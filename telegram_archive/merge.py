"""Merge one archive into another: ``telegram-archive merge``.

The source archive is only read. Each of its accounts is added to the target
under the next free account id, every row the account owns is copied under
that id, and the media files those rows point at are copied into the target's
media folder. Nothing already in the target is deleted or changed.

Two archives at the same head revision are required: the table list below is
the schema at head, and a source at another revision would be missing columns
or carry columns this code does not know.

Custom emoji (``custom_emoji`` and the files in ``media/_emoji``) are shared by
every account, so the rows and files the target lacks are added and the
target's own stay as they are. A row whose file does not come across arrives
pending, and the target's next backup fetches it.

What is not merged, on purpose: viewer accounts, viewer sessions, share
tokens, push subscriptions, the viewer audit log and app settings. Those
describe who may read the source install, not what it archived. Global
metadata (backup timestamps, cached statistics, the push keys) belongs to the
target install and stays as it is.

Privacy: every message this module raises or returns names tables, counts and
account row ids only, never a chat id, a path, a title or message content.
"""

import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, make_url

from .db.adapter import SUPERGROUP_ID_CEILING
from .db.models import (
    DEFAULT_ACCOUNT_ID,
    Account,
    AvatarHistory,
    Base,
    Chat,
    ChatFolder,
    ChatFolderMember,
    CustomEmoji,
    ForumTopic,
    Media,
    MediaTranscript,
    MediaVersion,
    Message,
    MessageSnapshot,
    MessageVersion,
    Metadata,
    Reaction,
    ReactionHistory,
    SyncStatus,
    User,
    account_metadata_key,
    new_chat_ref,
)
from .message_utils import CUSTOM_EMOJI_DIR, CUSTOM_EMOJI_FILE_RE, compute_file_hash, resolve_shared_file_path

BATCH_SIZE = 1000

# Account-scoped tables in the order the target's foreign keys need them.
# The flag is True when the table's own ``id`` is a surrogate the target must
# assign: those ids collide between two archives and nothing references them.
ACCOUNT_TABLES: tuple[tuple[sa.Table, bool], ...] = (
    (Chat.__table__, False),
    (ChatFolder.__table__, False),
    (Message.__table__, False),
    (ForumTopic.__table__, False),
    (SyncStatus.__table__, False),
    (ChatFolderMember.__table__, False),
    (Media.__table__, False),
    (MessageVersion.__table__, True),
    (MediaVersion.__table__, True),
    (MessageSnapshot.__table__, True),
    (Reaction.__table__, True),
    (ReactionHistory.__table__, True),
    (AvatarHistory.__table__, True),
)

# Every table with an ``account_id`` column that the merge copies.
ACCOUNT_SCOPED_TABLES: tuple[sa.Table, ...] = tuple(table for table, _ in ACCOUNT_TABLES) + (MediaTranscript.__table__,)

# Tables whose ``id`` the target assigns from its sequence during the copy.
SURROGATE_ID_TABLES: tuple[sa.Table, ...] = tuple(table for table, drop_id in ACCOUNT_TABLES if drop_id) + (
    MediaTranscript.__table__,
)

# How long a SQLite target waits for another writer before the merge gives up.
SQLITE_BUSY_TIMEOUT = 60

# Per-account metadata keys (``account_metadata_key``): account 1 uses the bare
# key, every other account the ``_account_<id>`` suffix. ``listener_active_since``
# and ``listener_heartbeat`` are left out: they describe a process of the source
# install, not the archive.
ACCOUNT_METADATA_KEY = re.compile(
    r"^(?P<base>followed_migrations|whitelist_unresolved_ids|reaction_resweep_cycle_done|import_progress"
    r"|message_failures_-?\d+)(?:_account_(?P<account>\d+))?$"
)

AVATAR_FOLDERS = ("users", "chats")
AVATAR_CHAT_ID = re.compile(r"^(-?\d+)[_.]")

# The date of a placeholder message: none of its own is known, and the start of
# the epoch keeps it out of the way at the very top of its chat.
PLACEHOLDER_MESSAGE_DATE = datetime(1970, 1, 1)


class MergeError(Exception):
    """The merge refused or stopped. The message names no chat, path or content."""


@dataclass
class MediaPlan:
    """What the media copy does, or would do in a dry run."""

    files: int = 0
    blobs: int = 0
    links: int = 0
    bytes: int = 0
    present: int = 0
    missing: int = 0
    avatars: int = 0
    avatars_present: int = 0
    avatars_kept: int = 0
    emoji: int = 0
    emoji_present: int = 0


@dataclass
class MergeReport:
    """The outcome: the account id map, row counts per table and the media plan."""

    dry_run: bool
    account_ids: dict[int, int]
    rows: dict[str, int]
    media: MediaPlan | None
    transcript_links_dropped: int = 0
    placeholders: dict[str, int] = field(default_factory=dict)


@dataclass
class MediaCopier:
    """State shared by one pass over the media files."""

    source_root: str
    target_root: str
    target_conn: Connection
    write: bool
    # The merged accounts' new ids. Their media rows are the ones being merged,
    # so they never vouch for a blob the target already had.
    new_account_ids: list[int] = field(default_factory=list)
    plan: MediaPlan = field(default_factory=MediaPlan)
    planned: set[str] = field(default_factory=set)
    blob_by_hash: dict[str, str] = field(default_factory=dict)

    @property
    def source_shared(self) -> str:
        return os.path.realpath(os.path.join(self.source_root, "_shared"))

    @property
    def target_shared(self) -> str:
        return os.path.join(self.target_root, "_shared")


# ---------------------------------------------------------------------------
# Database URLs and engines
# ---------------------------------------------------------------------------


def sync_database_url(value: str) -> str:
    """A synchronous SQLAlchemy URL for a SQLite path or a database URL."""
    if "://" not in value:
        return f"sqlite:///{os.path.abspath(value)}"
    url = make_url(value)
    backend = url.get_backend_name()
    if backend == "sqlite":
        return url.set(drivername="sqlite").render_as_string(hide_password=False)
    if backend in ("postgresql", "postgres"):
        return url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
    raise MergeError("only SQLite and PostgreSQL archives can be merged")


def target_database_url() -> str:
    """The target archive's URL, read from the same settings as every command."""
    from .db.base import DatabaseManager

    return sync_database_url(DatabaseManager().database_url)


def sqlite_file(url: str) -> str | None:
    """The absolute file path of a SQLite URL, or None for another backend."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return None
    return os.path.abspath(parsed.database or "")


def same_database(first: str, second: str) -> bool:
    """Whether two sync URLs name the same database."""
    first_file = sqlite_file(first)
    second_file = sqlite_file(second)
    if first_file is not None or second_file is not None:
        if first_file is None or second_file is None:
            return False
        return os.path.realpath(first_file) == os.path.realpath(second_file)
    a, b = make_url(first), make_url(second)
    return (a.host, a.port or 5432, a.database) == (b.host, b.port or 5432, b.database)


def sqlite_source_uri(path: str) -> str:
    """How to open a SQLite source so that nothing is written, not even beside it.

    Every archive runs in WAL mode, and a WAL database opened with ``mode=ro``
    still writes its ``-shm`` and ``-wal`` files next to it, and fails in a
    folder it cannot write. A stopped install leaves no ``-wal`` file, or an
    empty one, so the database file holds everything and is opened as
    immutable: no side files, no locks. A ``-wal`` file with content holds
    changes the database file lacks; reading them needs the side files, so
    such a source is read with ``mode=ro`` and its folder must be writable.
    """
    wal = f"{path}-wal"
    if not os.path.exists(wal) or os.path.getsize(wal) == 0:
        return f"sqlite:///file:{path}?mode=ro&immutable=1&uri=true"
    if not os.access(os.path.dirname(path), os.W_OK):
        raise MergeError(
            "the source database has changes still in its -wal file and its folder is read-only, so SQLite cannot "
            "read them. Start and stop the source install once, or copy the database with its -wal file to a "
            "writable folder"
        )
    return f"sqlite:///file:{path}?mode=ro&uri=true"


def open_engine(url: str, *, read_only: bool) -> Engine:
    """An engine for one side. The source side cannot write, by construction."""
    path = sqlite_file(url)
    if path is not None:
        if not os.path.isfile(path):
            side = "source" if read_only else "target"
            raise MergeError(f"the {side} database file does not exist")
        if read_only:
            return sa.create_engine(sqlite_source_uri(path), hide_parameters=True)
        return sa.create_engine(url, hide_parameters=True, connect_args={"timeout": SQLITE_BUSY_TIMEOUT})
    if read_only:
        return sa.create_engine(
            url, hide_parameters=True, connect_args={"options": "-c default_transaction_read_only=on"}
        )
    return sa.create_engine(url, hide_parameters=True)


# ---------------------------------------------------------------------------
# Preflight: every check runs before anything is written
# ---------------------------------------------------------------------------


def head_revision() -> str:
    """The newest migration this release ships."""
    from alembic.script import ScriptDirectory

    from .db.migrations import alembic_config

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def read_revision(conn: Connection) -> str | None:
    """The single revision stamped in ``alembic_version``, or None."""
    if not sa.inspect(conn).has_table("alembic_version"):
        return None
    rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    return rows[0] if len(rows) == 1 else None


def lock_sqlite_target(target: Connection) -> None:
    """Take a SQLite target's write lock before the checks, so they hold until the commit.

    A backup or listener still writing to the target is refused here, before
    anything is copied, instead of failing the copy much later.
    """
    if target.dialect.name != "sqlite":
        return
    try:
        target.exec_driver_sql("BEGIN IMMEDIATE")
    except sa.exc.OperationalError:
        raise MergeError("another process is writing to the target database. Stop the target install first") from None


def check_no_backup_running(target: Connection) -> None:
    """Refuse while the target's records say one of its backups is running."""
    running = target.execute(sa.select(Metadata.value).where(Metadata.key == "backup_in_progress")).scalar()
    if running != "1":
        return
    raise MergeError(
        "the target's records say a backup is running. Stop the target install first. If it is already stopped, "
        "the flag is left from a run that was cut off: start the target, let one backup finish, stop it and try "
        "again"
    )


def check_revisions(source: Connection, target: Connection, head: str) -> None:
    """Both archives must be at this release's head revision."""
    source_revision = read_revision(source)
    target_revision = read_revision(target)
    if source_revision == head and target_revision == head:
        return
    raise MergeError(
        f"both archives must be at schema revision {head}: the source is at {source_revision or 'none'} "
        f"and the target at {target_revision or 'none'}. Upgrade both installs to this release and start "
        "each once (or run 'telegram-archive migrate' against it), then try again"
    )


def select_source_accounts(source: Connection, selector: str | None) -> list[sa.Row]:
    """The source account rows to merge: all of them, or the one ``--account`` names."""
    rows = source.execute(sa.select(Account.id, Account.label, Account.telegram_user_id).order_by(Account.id)).all()
    if selector is None:
        selected = rows
    else:
        # A label wins over an account id, so a label made of digits can still be chosen.
        selected = [row for row in rows if row.label == selector]
        if not selected and selector.isdigit():
            selected = [row for row in rows if row.id == int(selector)]
    if not selected:
        raise MergeError("the source archive has no account matching --account" if selector else "no accounts")
    if selector is not None and len(selected) > 1:
        raise MergeError("more than one source account has that label; pass the account id instead")
    return selected


def check_accounts(source_accounts: list[sa.Row], target: Connection) -> None:
    """Refuse unclaimed accounts on either side and an account both archives hold."""
    for row in source_accounts:
        if row.telegram_user_id is None:
            raise MergeError(
                f"source account {row.id} has never logged in, so its rows could never be matched to a login. "
                "Start the source install once, then try again"
            )
    target_rows = target.execute(sa.select(Account.id, Account.telegram_user_id)).all()
    if any(row.telegram_user_id is None for row in target_rows):
        raise MergeError(
            "the target has an account that has never logged in. Start the target install once so every "
            "account claims its row, then try again"
        )
    target_owner = {row.telegram_user_id: row.id for row in target_rows}
    for row in source_accounts:
        if row.telegram_user_id in target_owner:
            raise MergeError(
                f"source account {row.id} is the same Telegram account as target account "
                f"{target_owner[row.telegram_user_id]}. Two archives of one account are not merged"
            )


def plan_account_ids(source_accounts: list[sa.Row], target: Connection) -> dict[int, int]:
    """Source account id -> the next free target account ids, in source order.

    Free means free everywhere: an id no account row has and no account-scoped
    row carries either, so rows left behind by a removed account can never be
    mistaken for the merged account's.
    """
    highest = target.execute(sa.select(sa.func.max(Account.id))).scalar() or 0
    for table in ACCOUNT_SCOPED_TABLES:
        used = target.execute(sa.select(sa.func.max(table.c.account_id))).scalar() or 0
        highest = max(highest, used)
    return {row.id: highest + offset for offset, row in enumerate(source_accounts, start=1)}


def stream(conn: Connection, stmt: sa.Select) -> Iterator[list[dict[str, Any]]]:
    """Rows of ``stmt`` in batches of plain dicts, never the whole table at once."""
    result = conn.execution_options(yield_per=BATCH_SIZE).execute(stmt)
    for batch in result.mappings().partitions():
        yield [dict(row) for row in batch]


def check_chat_refs(source: Connection, target: Connection, account_ids: list[int]) -> None:
    """Chats keep their refs, so a ref the target already uses stops the merge."""
    stmt = sa.select(Chat.ref).where(Chat.account_id.in_(account_ids)).order_by(Chat.account_id, Chat.id)
    for batch in stream(source, stmt):
        refs = [row["ref"] for row in batch]
        clash = target.execute(sa.select(sa.func.count()).where(Chat.ref.in_(refs))).scalar()
        if clash:
            raise MergeError(
                f"{clash} chat ref(s) in the source already exist in the target. Refs are random, so one of "
                "these archives holds copied rows"
            )


def missing_parent(table: sa.Table, constraint: sa.ForeignKeyConstraint) -> sa.ColumnElement[bool]:
    """A row of ``table`` whose foreign key names a parent row the same database lacks."""
    pairs = [(element.parent, element.column) for element in constraint.elements]
    filled = sa.and_(*(child.is_not(None) for child, _ in pairs))
    parent = sa.exists().where(*(child == referred for child, referred in pairs))
    return sa.and_(filled, ~parent)


def count_orphans(source: Connection, target: Connection, account_ids: list[int]) -> dict[str, int]:
    """Rows of the merged accounts whose parent row neither archive has, per table.

    SQLite does not enforce foreign keys, so a SQLite archive can hold such
    rows. A parent in an account-scoped table must come from the source; a
    user can also be one the target already knows.
    """
    counts: dict[str, int] = {}
    for table, _ in ACCOUNT_TABLES:
        mine = table.c.account_id.in_(account_ids)
        orphans = 0
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table is User.__table__:
                orphans += count_unknown_users(source, target, table, constraint, mine)
                continue
            stmt = sa.select(sa.func.count()).select_from(table).where(mine, missing_parent(table, constraint))
            orphans += source.execute(stmt).scalar()
        if orphans:
            counts[table.name] = orphans
    return counts


def count_unknown_users(
    source: Connection,
    target: Connection,
    table: sa.Table,
    constraint: sa.ForeignKeyConstraint,
    mine: sa.ColumnElement[bool],
) -> int:
    """Rows of ``table`` naming a user neither the source nor the target has."""
    (column,) = constraint.columns
    stmt = (
        sa.select(column.label("user_id"), sa.func.count().label("rows"))
        .where(mine, missing_parent(table, constraint))
        .group_by(column)
        .order_by(column)
    )
    orphans = 0
    for batch in stream(source, stmt):
        known = set(target.execute(sa.select(User.id).where(User.id.in_([row["user_id"] for row in batch]))).scalars())
        orphans += sum(row["rows"] for row in batch if row["user_id"] not in known)
    return orphans


def check_orphans(source: Connection, target: Connection, account_ids: list[int]) -> None:
    """A PostgreSQL target refuses a row whose parent is missing, so find them before any write."""
    if target.dialect.name != "postgresql" or source.dialect.name != "sqlite":
        return
    counts = count_orphans(source, target, account_ids)
    if not counts:
        return
    listed = ", ".join(f"{name}: {count}" for name, count in counts.items())
    raise MergeError(
        f"the source has rows whose parent row it lacks ({listed}). SQLite keeps such rows and PostgreSQL "
        "refuses them. Pass --add-missing-parents to add an empty placeholder parent for each, so every row "
        "is merged"
    )


def known_users(target: Connection, keys: set[tuple]) -> set[int]:
    """The user ids among ``keys`` that the target already has, read in batches."""
    ids = sorted(user_id for (user_id,) in keys)
    known: set[int] = set()
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start : start + BATCH_SIZE]
        known.update(target.execute(sa.select(User.id).where(User.id.in_(chunk))).scalars())
    return known


def placeholder_chat_type(chat_id: int) -> str:
    """A chat type from the id alone, the rule migration 022 types its placeholder chats with."""
    if chat_id < SUPERGROUP_ID_CEILING:
        return "supergroup"
    return "group" if chat_id < 0 else "private"


def find_missing_parents(source: Connection, target: Connection, account_ids: list[int]) -> dict[str, list[tuple]]:
    """The parent rows the merged accounts' rows point at and neither archive has.

    Keyed by the parent table, each value holds the parent's key columns in the
    order of that table's foreign-key columns: ``(account_id, id)`` for chats
    and folders, ``(account_id, id, chat_id)`` for messages, ``(id,)`` for
    users. A placeholder message needs a chat too, so its chat is added when
    the source lacks it.
    """
    missing: dict[str, set[tuple]] = {}
    for table, _ in ACCOUNT_TABLES:
        mine = table.c.account_id.in_(account_ids)
        for constraint in table.foreign_key_constraints:
            children = [element.parent for element in constraint.elements]
            stmt = sa.select(*children).where(mine, missing_parent(table, constraint)).distinct()
            keys = {tuple(row.values()) for batch in stream(source, stmt) for row in batch}
            if constraint.referred_table is User.__table__:
                known = known_users(target, keys)
                keys = {key for key in keys if key[0] not in known}
            if keys:
                missing.setdefault(constraint.referred_table.name, set()).update(keys)
    message_chats = {(account, chat) for account, _, chat in missing.get(Message.__tablename__, ())}
    if message_chats:
        wanted = sorted(message_chats)
        present = set()
        for start in range(0, len(wanted), BATCH_SIZE):
            chunk = wanted[start : start + BATCH_SIZE]
            stmt = sa.select(Chat.account_id, Chat.id).where(sa.tuple_(Chat.account_id, Chat.id).in_(chunk))
            present.update(tuple(row) for row in source.execute(stmt))
        lacking = message_chats - present
        if lacking:
            missing.setdefault(Chat.__tablename__, set()).update(lacking)
    return {name: sorted(keys) for name, keys in missing.items()}


def placeholder_rows(name: str, keys: list[tuple], account_map: dict[int, int]) -> list[dict[str, Any]]:
    """Empty parent rows for ``keys``, under the new account ids. No value comes from message content."""
    if name == User.__tablename__:
        return [{"id": user_id} for (user_id,) in keys]
    if name == Chat.__tablename__:
        return [
            {
                "account_id": account_map[account],
                "id": chat_id,
                "ref": new_chat_ref(),
                "type": placeholder_chat_type(chat_id),
                "title": "",
            }
            for account, chat_id in keys
        ]
    if name == ChatFolder.__tablename__:
        return [{"account_id": account_map[account], "id": folder_id, "title": ""} for account, folder_id in keys]
    if name == Message.__tablename__:
        return [
            {"account_id": account_map[account], "id": message_id, "chat_id": chat_id, "date": PLACEHOLDER_MESSAGE_DATE}
            for account, message_id, chat_id in keys
        ]
    raise MergeError(f"no placeholder is defined for a missing {name} row")


def remap_metadata_key(key: str, account_map: dict[int, int]) -> str | None:
    """The target key for a per-account source key, or None when it is not merged."""
    match = ACCOUNT_METADATA_KEY.match(key)
    if match is None:
        return None
    account = int(match["account"]) if match["account"] else DEFAULT_ACCOUNT_ID
    if account not in account_map:
        return None
    return account_metadata_key(match["base"], account_map[account])


def account_metadata_rows(source: Connection, account_map: dict[int, int]) -> list[dict[str, Any]]:
    """The source's per-account metadata rows, re-keyed for the target."""
    rows = []
    for key, value in source.execute(sa.select(Metadata.key, Metadata.value)).all():
        new_key = remap_metadata_key(key, account_map)
        if new_key is not None:
            rows.append({"key": new_key, "value": value})
    return rows


def check_metadata_keys(target: Connection, rows: list[dict[str, Any]]) -> None:
    """A re-keyed metadata row must not replace one the target already has."""
    keys = [row["key"] for row in rows]
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = keys[start : start + BATCH_SIZE]
        clash = target.execute(sa.select(sa.func.count()).where(Metadata.key.in_(chunk))).scalar()
        if clash:
            raise MergeError(f"{clash} per-account metadata key(s) for the new account ids already exist in the target")


def referenced_user_ids(source: Connection, account_ids: list[int]) -> set[int]:
    """Every user id the given accounts' rows point at: senders, reactions and chats.

    A private chat's id is the other party's user id, and the viewer looks a
    chat up in the users table by that id.
    """
    queries = (
        sa.select(Message.sender_id).where(Message.account_id.in_(account_ids)).distinct(),
        sa.select(Reaction.user_id).where(Reaction.account_id.in_(account_ids)).distinct(),
        sa.select(Chat.id).where(Chat.account_id.in_(account_ids)).distinct(),
    )
    ids: set[int] = set()
    for stmt in queries:
        for batch in stream(source, stmt):
            ids.update(value for row in batch for value in row.values() if value is not None)
    return ids


def missing_users(source: Connection, target: Connection, wanted: set[int] | None) -> Iterator[list[dict[str, Any]]]:
    """Batches of source users the target does not know. The target's rows win.

    ``wanted`` limits the copy to those user ids; None copies every source user.
    """
    stmt = sa.select(*User.__table__.c).order_by(User.id)
    for batch in stream(source, stmt):
        if wanted is not None:
            batch = [row for row in batch if row["id"] in wanted]
            if not batch:
                continue
        ids = [row["id"] for row in batch]
        known = set(target.execute(sa.select(User.id).where(User.id.in_(ids))).scalars())
        fresh = [row for row in batch if row["id"] not in known]
        if fresh:
            yield fresh


def missing_custom_emoji(source: Connection, target: Connection) -> Iterator[list[dict[str, Any]]]:
    """Batches of source custom emoji rows the target does not have. The target's rows win."""
    stmt = sa.select(*CustomEmoji.__table__.c).order_by(CustomEmoji.document_id)
    for batch in stream(source, stmt):
        ids = [row["document_id"] for row in batch]
        known = set(
            target.execute(sa.select(CustomEmoji.document_id).where(CustomEmoji.document_id.in_(ids))).scalars()
        )
        fresh = [row for row in batch if row["document_id"] not in known]
        if fresh:
            yield fresh


def custom_emoji_source_file(source_media: str, name: str) -> str | None:
    """The path of a custom emoji file the merge may copy from the source, or None.

    A real, non-empty file under the source's own ``_emoji`` folder with a name
    the fetcher writes. A symlink is refused, and so is a path that resolves
    outside the source media folder: the copy would otherwise publish whatever
    a link in an untrusted source points at, through the emoji route.
    """
    if CUSTOM_EMOJI_FILE_RE.match(name) is None:
        return None
    path = os.path.join(source_media, CUSTOM_EMOJI_DIR, name)
    if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    root = os.path.realpath(source_media)
    if os.path.commonpath([root, os.path.realpath(path)]) != root:
        return None
    return path


def custom_emoji_file_comes(row: dict[str, Any], source_media: str | None) -> bool:
    """Whether a downloaded row's file is in the source media folder, so the media copy brings it."""
    name = row.get("file_name")
    if not row.get("downloaded") or source_media is None or not isinstance(name, str):
        return False
    return custom_emoji_source_file(source_media, name) is not None


def copy_custom_emoji(source: Connection, target: Connection, source_media: str | None) -> int:
    """Add the custom emoji rows the target lacks; one whose file does not come across arrives pending."""
    copied = 0
    for batch in missing_custom_emoji(source, target):
        for row in batch:
            if row["downloaded"] and not custom_emoji_file_comes(row, source_media):
                row["downloaded"] = 0
                row["download_date"] = None
                row["attempts"] = 0
        target.execute(sa.insert(CustomEmoji), batch)
        copied += len(batch)
    return copied


def count_source_rows(
    source: Connection,
    target: Connection,
    account_ids: list[int],
    metadata_rows: list[dict[str, Any]],
    wanted_users: set[int] | None,
) -> dict[str, int]:
    """The row counts the merge will add, per table."""
    users = sum(len(batch) for batch in missing_users(source, target, wanted_users))
    counts = {"accounts": len(account_ids), "users": users}
    counts[CustomEmoji.__tablename__] = sum(len(batch) for batch in missing_custom_emoji(source, target))
    for table, _ in ACCOUNT_TABLES:
        counts[table.name] = count_account_rows(source, table, account_ids)
    counts[MediaTranscript.__tablename__] = count_account_rows(source, MediaTranscript.__table__, account_ids)
    counts[Metadata.__tablename__] = len(metadata_rows)
    return counts


def count_account_rows(conn: Connection, table: sa.Table, account_ids: list[int]) -> int:
    """Rows of ``table`` that belong to the given accounts."""
    return conn.execute(
        sa.select(sa.func.count()).select_from(table).where(table.c.account_id.in_(account_ids))
    ).scalar()


# ---------------------------------------------------------------------------
# Media files
# ---------------------------------------------------------------------------


def media_relative_path(file_path: str | None) -> str | None:
    """A stored ``media.file_path`` as a path relative to the media folder, or None.

    The same rule the viewer applies: a relative path is kept, and an absolute
    one is cut after its last ``/media/``, because the media folder is always
    ``<BACKUP_PATH>/media``.
    """
    if not file_path:
        return None
    path = file_path.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        cut = path.rfind("/media/")
        if cut < 0:
            return None
        path = path[cut + len("/media/") :]
    if not path or path.startswith("/") or ".." in path.split("/"):
        return None
    return path


def rebase_media_row(row: dict[str, Any]) -> dict[str, Any]:
    """Point a copied media row at its file relative to the target's media folder."""
    relative = media_relative_path(row.get("file_path"))
    if relative is not None:
        row["file_path"] = relative
    return row


def is_inside(path: str, root: str) -> bool:
    """Whether ``path`` lies under the directory ``root`` (both resolved)."""
    return path == root or path.startswith(root + os.sep)


def same_content(existing: str, expected_hash: str) -> bool:
    """Whether the target file at ``existing`` holds bytes with ``expected_hash``."""
    if not os.path.isfile(existing):
        return False
    return compute_file_hash(existing) == expected_hash


def copy_new_file(source: str, destination: str) -> None:
    """Copy a file to a name that must not exist yet. Never replaces a file."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    partial = f"{destination}.merge-{os.getpid()}.part"
    try:
        shutil.copy2(source, partial)
    except OSError:
        # Only the partial file this call created; never an archive file.
        if os.path.lexists(partial):
            os.remove(partial)
        raise
    # The target install is stopped, so nothing else creates this name between
    # the check and the rename.
    if os.path.lexists(destination):
        os.remove(partial)
        raise MergeError("a media file appeared in the target while the merge was copying it")
    os.replace(partial, destination)


def target_blob_for(copier: MediaCopier, content_hash: str) -> str | None:
    """A blob the target already had for ``content_hash``, found through its own media rows.

    Only rows that were in the target before the merge count, and a file is
    reused only when its bytes have that hash: a name can resolve to an older
    flat ``_shared`` file that holds something else.
    """
    cached = copier.blob_by_hash.get(content_hash)
    if cached is not None:
        return cached
    names = copier.target_conn.execute(
        sa.select(Media.file_name)
        .where(Media.content_hash == content_hash, Media.account_id.not_in(copier.new_account_ids))
        .distinct()
        .order_by(Media.file_name)
    ).scalars()
    for name in names:
        if not name:
            continue
        found = resolve_shared_file_path(copier.target_shared, name, content_hash)
        if found and same_content(found, content_hash):
            copier.blob_by_hash[content_hash] = found
            return found
    return None


def place_blob(copier: MediaCopier, blob: str, content_hash: str) -> str:
    """The target blob for a source ``_shared`` blob, copying it when the target lacks it."""
    existing = target_blob_for(copier, content_hash)
    if existing is not None:
        return existing
    destination = os.path.join(copier.target_shared, os.path.relpath(blob, copier.source_shared))
    if destination in copier.planned:
        return destination
    if os.path.lexists(destination):
        if not same_content(destination, content_hash):
            raise MergeError("a shared media file in the target has the same name as a different source file")
        copier.blob_by_hash[content_hash] = destination
        return destination
    if copier.write:
        copy_new_file(blob, destination)
    else:
        copier.planned.add(destination)
    # A later source blob with the same bytes under another name reuses this
    # one, in the dry run exactly as in the real run.
    copier.blob_by_hash[content_hash] = destination
    copier.plan.blobs += 1
    copier.plan.bytes += os.path.getsize(blob)
    return destination


def place_link(copier: MediaCopier, relative: str, blob: str, content_hash: str | None) -> None:
    """Recreate a chat-folder symlink into ``_shared``, the way the backup writes it."""
    content_hash = content_hash or compute_file_hash(blob)
    target_blob = place_blob(copier, blob, content_hash)
    destination = os.path.join(copier.target_root, relative)
    if destination in copier.planned:
        copier.plan.present += 1
        return
    if os.path.lexists(destination):
        if not same_content(destination, content_hash):
            raise MergeError("a media file in the target has the same name as a different source file")
        copier.plan.present += 1
        return
    if copier.write:
        chat_dir = os.path.dirname(destination)
        os.makedirs(chat_dir, exist_ok=True)
        os.symlink(os.path.relpath(target_blob, chat_dir), destination)
    else:
        copier.planned.add(destination)
    copier.plan.links += 1


def place_file(copier: MediaCopier, relative: str, source_file: str, content_hash: str | None) -> None:
    """Copy a plain chat-folder file unless the target already holds the same bytes."""
    destination = os.path.join(copier.target_root, relative)
    if destination in copier.planned:
        copier.plan.present += 1
        return
    if os.path.lexists(destination):
        if not same_content(destination, content_hash or compute_file_hash(source_file)):
            raise MergeError("a media file in the target has the same name as a different source file")
        copier.plan.present += 1
        return
    if copier.write:
        copy_new_file(source_file, destination)
    else:
        copier.planned.add(destination)
    copier.plan.files += 1
    copier.plan.bytes += os.path.getsize(source_file)


def place_media_row(copier: MediaCopier, row: dict[str, Any]) -> None:
    """Bring one media row's file across, as a symlink into ``_shared`` or as a file."""
    relative = media_relative_path(row["file_path"])
    source_entry = os.path.join(copier.source_root, relative) if relative else None
    if source_entry is None or not os.path.lexists(source_entry):
        if row["downloaded"]:
            copier.plan.missing += 1
        return
    resolved = os.path.realpath(source_entry)
    if not os.path.isfile(resolved):
        copier.plan.missing += 1
        return
    if os.path.islink(source_entry) and is_inside(resolved, copier.source_shared):
        place_link(copier, relative, resolved, row["content_hash"])
        return
    place_file(copier, relative, resolved, row["content_hash"])


def place_avatars(copier: MediaCopier, owner_ids: set[int]) -> None:
    """Copy the avatar files of the merged accounts' chats and senders that the target lacks.

    An avatar file is named after its photo id and never rewritten, so a name
    the target already has is the same picture. Older names without a photo id
    can differ; the target's file stays and the source's is counted as kept out.
    """
    for folder in AVATAR_FOLDERS:
        source_dir = os.path.join(copier.source_root, "avatars", folder)
        if not os.path.isdir(source_dir):
            continue
        for name in sorted(os.listdir(source_dir)):
            match = AVATAR_CHAT_ID.match(name)
            source_file = os.path.join(source_dir, name)
            if match is None or int(match[1]) not in owner_ids or not os.path.isfile(source_file):
                continue
            place_avatar(copier, source_file, os.path.join(copier.target_root, "avatars", folder, name))


def place_avatar(copier: MediaCopier, source_file: str, destination: str) -> None:
    """Copy one avatar file, or count it as present or kept out."""
    if destination in copier.planned or os.path.lexists(destination):
        if destination in copier.planned or same_content(destination, compute_file_hash(source_file)):
            copier.plan.avatars_present += 1
        else:
            copier.plan.avatars_kept += 1
        return
    if copier.write:
        copy_new_file(source_file, destination)
    else:
        copier.planned.add(destination)
    copier.plan.avatars += 1
    copier.plan.bytes += os.path.getsize(source_file)


def place_custom_emoji(copier: MediaCopier) -> None:
    """Copy the custom emoji files the target lacks. A name the target has is the same file."""
    source_dir = os.path.join(copier.source_root, CUSTOM_EMOJI_DIR)
    if not os.path.isdir(source_dir):
        return
    for name in sorted(os.listdir(source_dir)):
        source_file = custom_emoji_source_file(copier.source_root, name)
        if source_file is None:
            continue
        destination = os.path.join(copier.target_root, CUSTOM_EMOJI_DIR, name)
        if destination in copier.planned or os.path.lexists(destination):
            copier.plan.emoji_present += 1
            continue
        if copier.write:
            copy_new_file(source_file, destination)
        else:
            copier.planned.add(destination)
        copier.plan.emoji += 1
        copier.plan.bytes += os.path.getsize(source_file)


def copy_media_files(copier: MediaCopier, source: Connection, account_ids: list[int]) -> MediaPlan:
    """One pass over the merged accounts' media rows, their earlier media, and avatars."""
    stmt = (
        sa.select(Media.file_path, Media.content_hash, Media.downloaded)
        .where(Media.account_id.in_(account_ids), Media.file_path.is_not(None))
        .order_by(Media.account_id, Media.id)
    )
    for batch in stream(source, stmt):
        for row in batch:
            place_media_row(copier, row)
    # The files an edit replaced, kept in media_versions (036).
    version_stmt = (
        sa.select(MediaVersion.file_path, MediaVersion.content_hash, MediaVersion.downloaded)
        .where(MediaVersion.account_id.in_(account_ids), MediaVersion.file_path.is_not(None))
        .order_by(MediaVersion.account_id, MediaVersion.id)
    )
    for batch in stream(source, version_stmt):
        for row in batch:
            place_media_row(copier, row)
    place_avatars(copier, avatar_owner_ids(source, account_ids))
    place_custom_emoji(copier)
    return copier.plan


def avatar_owner_ids(source: Connection, account_ids: list[int]) -> set[int]:
    """Ids whose avatar files belong to the merged accounts: their chats and their senders."""
    owners = set(source.execute(sa.select(Chat.id).where(Chat.account_id.in_(account_ids))).scalars())
    senders = sa.select(Message.sender_id).where(Message.account_id.in_(account_ids)).distinct()
    owners.update(sender for sender in source.execute(senders).scalars() if sender is not None)
    return owners


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def advance_sequence(target: Connection, table: str, floor: int) -> None:
    """Move a PostgreSQL ``id`` sequence so its next value is above ``floor``. Never moves it back.

    ``table`` is always a module constant, never input.
    """
    sequence = target.execute(sa.text(f"SELECT pg_get_serial_sequence('{table}', 'id')")).scalar()
    if sequence is None:
        return
    last_value, is_called = target.execute(sa.text(f"SELECT last_value, is_called FROM {sequence}")).one()
    next_value = last_value + 1 if is_called else last_value
    if next_value <= floor:
        target.execute(
            sa.text("SELECT setval(CAST(:sequence AS regclass), :floor)"), {"sequence": sequence, "floor": floor}
        )


def resync_surrogate_sequences(target: Connection) -> None:
    """Move each sequence the copy draws ids from past the ids its table already holds.

    A restore or a move to PostgreSQL copies rows with their ids and can leave
    a sequence behind its table; the first copied row would then collide.
    """
    if target.dialect.name != "postgresql":
        return
    for table in SURROGATE_ID_TABLES:
        highest = target.execute(sa.select(sa.func.max(table.c.id))).scalar() or 0
        advance_sequence(target, table.name, highest)


def insert_accounts(target: Connection, source_accounts: list[sa.Row], account_map: dict[int, int]) -> None:
    """Add the merged accounts under their new ids."""
    for row in source_accounts:
        target.execute(
            sa.insert(Account).values(id=account_map[row.id], label=row.label, telegram_user_id=row.telegram_user_id)
        )
    if target.dialect.name == "postgresql":
        # An explicit id does not advance the sequence; the next account the
        # target logs in would otherwise be handed one of these ids, or one
        # that rows of a removed account still carry.
        advance_sequence(target, Account.__tablename__, max(account_map.values()))


def copy_users(source: Connection, target: Connection, wanted: set[int] | None) -> int:
    """Add the source's users the target does not know yet."""
    copied = 0
    for batch in missing_users(source, target, wanted):
        target.execute(sa.insert(User), batch)
        copied += len(batch)
    return copied


def copy_account_table(
    source: Connection,
    target: Connection,
    table: sa.Table,
    account_map: dict[int, int],
    drop_id: bool,
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    """Copy one account-scoped table in batches, with ``account_id`` remapped."""
    columns = [column for column in table.c if not (drop_id and column.name == "id")]
    stmt = sa.select(*columns).where(table.c.account_id.in_(list(account_map))).order_by(*table.primary_key.columns)
    copied = 0
    for batch in stream(source, stmt):
        for row in batch:
            row["account_id"] = account_map[row["account_id"]]
            if transform is not None:
                transform(row)
        target.execute(sa.insert(table), batch)
        copied += len(batch)
    return copied


def copy_transcripts(source: Connection, target: Connection, account_map: dict[int, int]) -> tuple[int, int]:
    """Copy transcripts row by row, remapping ``copied_from_id`` to the new ids.

    Returns (rows copied, provenance links dropped). A link is dropped only
    when a transcript was copied from a row of a source account left out with
    ``--account``: that row is not in the target to point at.
    """
    table = MediaTranscript.__table__
    stmt = sa.select(*table.c).where(table.c.account_id.in_(list(account_map))).order_by(table.c.id)
    new_ids: dict[int, int] = {}
    copied = 0
    dropped = 0
    for batch in stream(source, stmt):
        for row in batch:
            old_id = row.pop("id")
            row["account_id"] = account_map[row["account_id"]]
            if row["copied_from_id"] is not None:
                row["copied_from_id"] = new_ids.get(row["copied_from_id"])
                dropped += row["copied_from_id"] is None
            new_ids[old_id] = target.execute(sa.insert(table).values(row).returning(table.c.id)).scalar_one()
            copied += 1
    return copied, dropped


def insert_placeholders(
    target: Connection, name: str, missing: dict[str, list[tuple]], account_map: dict[int, int]
) -> None:
    """Add the placeholder parents of one table, right after its own rows."""
    keys = missing.get(name)
    if keys:
        table = Base.metadata.tables[name]
        rows = placeholder_rows(name, keys, account_map)
        for start in range(0, len(rows), BATCH_SIZE):
            target.execute(sa.insert(table), rows[start : start + BATCH_SIZE])


def copy_rows(
    source: Connection,
    target: Connection,
    source_accounts: list[sa.Row],
    account_map: dict[int, int],
    metadata_rows: list[dict[str, Any]],
    wanted_users: set[int] | None = None,
    missing: dict[str, list[tuple]] | None = None,
    source_media: str | None = None,
) -> tuple[dict[str, int], int]:
    """Copy every merged row, parents before children. Returns counts and dropped links.

    ``missing`` holds the placeholder parents to add; each table's go in right
    after that table's copied rows, before the rows that point at them.
    """
    missing = missing or {}
    step = Account.__tablename__
    try:
        resync_surrogate_sequences(target)
        insert_accounts(target, source_accounts, account_map)
        copied = {step: len(source_accounts)}
        step = User.__tablename__
        copied[step] = copy_users(source, target, wanted_users)
        insert_placeholders(target, step, missing, account_map)
        step = CustomEmoji.__tablename__
        copied[step] = copy_custom_emoji(source, target, source_media)
        for table, drop_id in ACCOUNT_TABLES:
            step = table.name
            transform = rebase_media_row if table in (Media.__table__, MediaVersion.__table__) else None
            copied[step] = copy_account_table(source, target, table, account_map, drop_id, transform)
            insert_placeholders(target, step, missing, account_map)
        step = MediaTranscript.__tablename__
        copied[step], dropped = copy_transcripts(source, target, account_map)
        step = Metadata.__tablename__
        if metadata_rows:
            target.execute(sa.insert(Metadata), metadata_rows)
        copied[step] = len(metadata_rows)
    except sa.exc.IntegrityError as error:
        # The type name only: the driver's message quotes the clashing key.
        raise MergeError(
            f"the target database rejected a {step} row ({type(error.orig).__name__}): it collides with a row "
            "the target already has. Nothing was written to the target database"
        ) from None
    return copied, dropped


def verify_counts(
    target: Connection,
    account_ids: list[int],
    expected: dict[str, int],
    copied: dict[str, int],
    placeholders: dict[str, int] | None = None,
) -> None:
    """Recount what landed in the target and compare it with the source's counts."""
    placeholders = placeholders or {}
    landed = dict(copied)
    for table, _ in ACCOUNT_TABLES:
        landed[table.name] = count_account_rows(target, table, account_ids) - placeholders.get(table.name, 0)
    landed[MediaTranscript.__tablename__] = count_account_rows(target, MediaTranscript.__table__, account_ids)
    wrong = sorted(name for name, count in expected.items() if landed.get(name) != count)
    if wrong:
        raise MergeError(f"row counts in the target do not match the source for: {', '.join(wrong)}")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def default_source_media(source_url: str) -> str | None:
    """``media`` beside a SQLite source database, when that folder exists."""
    path = sqlite_file(source_url)
    if path is None:
        return None
    candidate = os.path.join(os.path.dirname(path), "media")
    return candidate if os.path.isdir(candidate) else None


def merge_archives(
    *,
    source: str,
    target_url: str,
    target_media: str,
    source_media: str | None = None,
    dry_run: bool = False,
    account: str | None = None,
    add_missing_parents: bool = False,
) -> MergeReport:
    """Merge the source archive into the target. Every check runs before any write.

    Order: database checks, then the media check pass (it refuses on a name
    the target already uses for different bytes), then, unless ``dry_run``,
    the row copy in one target transaction, a recount, the media copy, and
    the commit. Anything that fails before the commit rolls every row back.
    Files already copied by then stay (they are new names, never replacements),
    and a second run finds them and counts them as already there.
    """
    source_url = sync_database_url(source)
    target_url = sync_database_url(target_url)
    if same_database(source_url, target_url):
        raise MergeError("the source and the target are the same database")
    if source_media is None:
        source_media = default_source_media(source_url)
    elif not os.path.isdir(source_media):
        raise MergeError("the --source-media folder does not exist")

    head = head_revision()
    source_engine = open_engine(source_url, read_only=True)
    try:
        target_engine = open_engine(target_url, read_only=False)
    except MergeError:
        source_engine.dispose()
        raise
    try:
        with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
            return run_merge(
                source_conn, target_conn, head, source_media, target_media, dry_run, account, add_missing_parents
            )
    finally:
        source_engine.dispose()
        target_engine.dispose()


def run_merge(
    source: Connection,
    target: Connection,
    head: str,
    source_media: str | None,
    target_media: str,
    dry_run: bool,
    account: str | None,
    add_missing_parents: bool = False,
) -> MergeReport:
    """The merge on two open connections. Commits the target only at the very end."""
    lock_sqlite_target(target)
    check_revisions(source, target, head)
    check_no_backup_running(target)
    source_accounts = select_source_accounts(source, account)
    check_accounts(source_accounts, target)
    account_map = plan_account_ids(source_accounts, target)
    source_ids = list(account_map)
    new_ids = list(account_map.values())
    check_chat_refs(source, target, source_ids)
    metadata_rows = account_metadata_rows(source, account_map)
    check_metadata_keys(target, metadata_rows)
    missing: dict[str, list[tuple]] = {}
    if add_missing_parents:
        missing = find_missing_parents(source, target, source_ids)
    else:
        check_orphans(source, target, source_ids)
    placeholders = {name: len(keys) for name, keys in missing.items()}
    # With --account, only the users the merged accounts point at; the others
    # belong to the accounts left out.
    wanted_users = referenced_user_ids(source, source_ids) if account is not None else None
    expected = count_source_rows(source, target, source_ids, metadata_rows, wanted_users)

    media_plan = None
    if source_media is not None:
        copier = MediaCopier(source_media, target_media, target, write=False, new_account_ids=new_ids)
        media_plan = copy_media_files(copier, source, source_ids)
    if dry_run:
        target.rollback()
        return MergeReport(
            dry_run=True, account_ids=account_map, rows=expected, media=media_plan, placeholders=placeholders
        )

    copied, dropped = copy_rows(
        source, target, source_accounts, account_map, metadata_rows, wanted_users, missing, source_media
    )
    verify_counts(target, new_ids, expected, copied, placeholders)
    if source_media is not None:
        copier = MediaCopier(source_media, target_media, target, write=True, new_account_ids=new_ids)
        media_plan = copy_media_files(copier, source, source_ids)
    target.commit()
    return MergeReport(
        dry_run=False,
        account_ids=account_map,
        rows=copied,
        media=media_plan,
        transcript_links_dropped=dropped,
        placeholders=placeholders,
    )


def format_report(report: MergeReport) -> list[str]:
    """The lines the command prints: account ids, counts and sizes only."""
    heading = "[DRY RUN] Merge plan, nothing written:" if report.dry_run else "Merge complete:"
    lines = [heading]
    for source_id, target_id in report.account_ids.items():
        lines.append(f"  Source account {source_id} -> target account {target_id}")
    lines.append("  Rows per table:")
    for name, count in report.rows.items():
        lines.append(f"    {name}: {count}")
    if report.placeholders:
        lines.append("  Placeholder parent rows added:")
        for name, count in report.placeholders.items():
            lines.append(f"    {name}: {count}")
    if report.transcript_links_dropped:
        lines.append(f"  Transcript copy links left empty (source row not merged): {report.transcript_links_dropped}")
    media = report.media
    if media is None:
        lines.append("  Media files: not copied (no source media folder; pass --source-media)")
        return lines
    size_mb = media.bytes / (1024 * 1024)
    lines.extend(
        [
            f"  Media files copied: {media.files}",
            f"  Shared files copied: {media.blobs}",
            f"  Links created: {media.links}",
            f"  Already in the target: {media.present}",
            f"  Missing in the source folder: {media.missing}",
            f"  Avatar files copied: {media.avatars} (already there: {media.avatars_present}, "
            f"target's own kept: {media.avatars_kept})",
            f"  Custom emoji files copied: {media.emoji} (already there: {media.emoji_present})",
            f"  Size: {size_mb:.1f} MB",
        ]
    )
    return lines
