"""Put ``account_id`` into the keys, so a second Telegram account cannot overwrite the first.

Until now every key in this schema assumed one account. A Telegram chat id,
message id, forum-topic id and dialog-filter id are only unique *within* an
account, so a decorative ``account_id`` column would not have been enough. Each
of these was built and run against scratch schemas before this migration was
written:

* ``message_versions`` UNIQUE(change_hash) silently DISCARDS the second
  account's entire edit history — the hash is over the message payload, which is
  identical for both accounts.
* ``chat_folders`` PK(id) destroys the first account's folder title and takes
  over all of its membership rows — Telegram numbers dialog filters per account
  and everyone's start at 2.
* ``messages`` PK(id, chat_id) drops the second account's copy of a message and
  makes it read the first account's ``is_outgoing``.

So the keys change:

===================== ============================= ==================================
table                 was                           becomes
===================== ============================= ==================================
chats                 PK (id)                       PK (account_id, id)
messages              PK (id, chat_id)              PK (account_id, chat_id, id)
media                 PK (id)                       PK (account_id, id)
sync_status           PK (chat_id)                  PK (account_id, chat_id)
forum_topics          PK (id, chat_id)              PK (account_id, chat_id, id)
chat_folders          PK (id)                       PK (account_id, id)
chat_folder_members   PK (folder_id, chat_id)       PK (account_id, folder_id, chat_id)
reactions             UNIQUE uq_reaction            + account_id
message_versions      UNIQUE(change_hash)           + account_id
===================== ============================= ==================================

``_message_version_hash``'s payload is deliberately NOT changed. It is a frozen
contract: re-encoding it would make every version already stored hash
differently and be re-inserted as a duplicate. The CONSTRAINT carries the
account instead.

``users`` and ``metadata`` stay global, and nothing on disk moves. Media stays
exactly where it is under ``<media>/<chat_id>/``.

Two new pieces of data ride along because they cannot be added later without a
second rebuild of the same tables:

* ``chats.ref`` — an opaque 22-character token minted once per chat, which the
  viewer will address chats by instead of putting a chat id in a URL.
* ``allowed_accounts`` and ``allowed_chat_refs`` on the four viewer-identity
  tables. They are NEW columns, never a reinterpretation of ``allowed_chat_ids``,
  because reinterpreting fails OPEN — an unconverted ``[123, 456]`` read as
  (account 123, chat 456) grants access rather than denying it. Every restricted
  row is converted here rather than left NULL, because NULL means "unrestricted".

One repair rides in front of all of it. Archives that lived through Telegram's
group→supergroup renumbering — or that arrived through a bulk copy which ran
with enforcement disabled, as data movers and restores routinely do — hold
messages, sync rows, topics or folder members whose ``chats`` row no longer
exists, and on PostgreSQL the 7.x constraint's ``convalidated`` flag still
reads true over them because nothing ever re-checked it. Recreating the
foreign keys below re-checks every row, so the first orphan used to abort the
whole upgrade. Every distinct chat id those tables still reference and
``chats`` no longer holds now gets a neutral placeholder row first — see
``_create_placeholder_chats`` — after which the formerly orphaned rows are
first-class and the viewer can serve them under their placeholder-titled chat.

Safety, in the order it matters:

1. **The first statement is DML.** Alembic sets ``transactional_ddl = False`` for
   SQLite and pysqlite opens no transaction until the first DML statement, so a
   ``CREATE TABLE`` issued before one is committed immediately and SURVIVES the
   rollback of everything after it. The next start then dies on "table already
   exists", forever. Writing ``alembic_version``'s row back to itself opens the
   transaction, changes nothing, and leaves nothing behind.
2. **Every rebuild is guarded and self-healing.** ``DROP TABLE IF EXISTS
   <t>__new022`` precedes every create, and every step asks the live schema
   whether its work is already done, so this is a complete no-op against a
   database ``create_all`` already provisioned in the 8.0 shape.
3. **Every SQLite rebuild declares its foreign keys, it never inherits them.**
   The failure this avoids was measured: a rebuild that carries the reflected
   constraint forward leaves ``media`` saying ``REFERENCES messages(id, chat_id)``
   while the primary key it names has become ``(account_id, chat_id, id)``, and
   ``PRAGMA foreign_key_check`` then reports
   ``foreign key mismatch - "media" referencing "messages"``. That check runs at
   the end of this migration, not only in its tests. The rebuild order is
   parent-first as well, which is what keeps it correct if foreign key
   enforcement is ever switched on; with enforcement off, as this project ships,
   the finished schema is the same either way — also measured.
4. **PostgreSQL drops the dependent foreign keys BY NAME first.** ``ALTER TABLE
   messages DROP CONSTRAINT messages_pkey`` fails while ``fk_media_message``
   depends on its index, and the obvious ``DROP ... CASCADE`` succeeds while
   permanently deleting that foreign key, announcing it with nothing but a
   NOTICE. No CASCADE appears in this file, all eight rebuilt constraints are
   recreated by name unconditionally, and all nine are asserted by name
   afterwards - so a lost one raises instead of being committed.
5. **Prechecks refuse rather than half-finish.** On SQLite the rebuild peaks at
   ~2.35x the database file — the WAL absorbs the whole rewrite inside one
   transaction — and a viewer holding the same file open is about to have its
   schema replaced underneath it. A third precheck makes sure SQLite is not
   enforcing foreign keys, because dropping a parent table would then delete its
   children first. All three run before anything is written.

Cost, measured on this migration: SQLite 400k messages 152 MiB -> 218 MiB in
2.2 s (peak 356 MiB), 1.6M messages 611 MiB -> 880 MiB in 11.9 s (peak
1442 MiB); PostgreSQL 1.6M messages 601 MiB -> 605 MiB in 5.5 s. Roughly 7 s and
1.8x growth per million messages on NVMe, and the ratio held across a 4x change
in size. On PostgreSQL the whole thing is one transaction holding ACCESS
EXCLUSIVE, so that time is startup downtime for the archive.

Revision ID: 022
Revises: 021
Create Date: 2026-08-15
"""

import json
import logging
import os
import secrets
import shutil
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# The account every pre-8.0.0 row is backfilled to.
DEFAULT_ACCOUNT_ID = 1
DEFAULT_ACCOUNT_LABEL = "default"

# Suffix for the temporary table a SQLite rebuild copies through. Unique to this
# revision so a leftover from any other migration can never be mistaken for ours.
TEMP_SUFFIX = "__new022"

# Peak transient size of the SQLite rebuild. Measured on this migration: a
# 400k-message archive went 152 MiB -> 218 MiB with a 356 MiB peak (2.34x) in
# 2.2 s, and a 1.6M-message one 611 MiB -> 880 MiB with a 1442 MiB peak (2.36x)
# in 11.9 s, both on NVMe. The excess is the write-ahead log, which cannot
# checkpoint inside a transaction, so the ratio is a property of the WAL rather
# than of the archive's size. 3x is that measurement with a margin.
SQLITE_DISK_HEADROOM = 3

# Tables that gain account_id. Order is irrelevant here; the SQLite rebuild
# order below is the one that matters.
ACCOUNT_ID_TABLES: tuple[str, ...] = (
    "chats",
    "messages",
    "media",
    "reactions",
    "message_versions",
    "sync_status",
    "forum_topics",
    "chat_folders",
    "chat_folder_members",
)

# Tables whose rows name a parent chat. A row in any of these whose chat id has
# no chats row is an orphan, and recreating the foreign keys below would refuse
# the whole upgrade over the first one it meets.
CHATS_REFERENCING_TABLES: tuple[str, ...] = ("messages", "sync_status", "forum_topics", "chat_folder_members")

# The marked-id convention this project stores (see telegram_backup's marked
# ids): supergroups and channels are -100 followed by the internal id, so they
# sit below -10^12; legacy basic groups are small negatives; users are positive.
SUPERGROUP_ID_CEILING = -(10**12)

# New primary keys, in column order. Leading with account_id makes it the
# partition key of every seek instead of a filter applied after one.
NEW_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "chats": ("account_id", "id"),
    "chat_folders": ("account_id", "id"),
    "messages": ("account_id", "chat_id", "id"),
    "sync_status": ("account_id", "chat_id"),
    "forum_topics": ("account_id", "chat_id", "id"),
    "chat_folder_members": ("account_id", "folder_id", "chat_id"),
    "media": ("account_id", "id"),
}

# Unique constraints that gain account_id, plus the new one on chats.ref.
NEW_UNIQUE_CONSTRAINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chats": ("uq_chats_ref", ("ref",)),
    "reactions": ("uq_reaction", ("account_id", "message_id", "chat_id", "emoji", "user_id")),
    "message_versions": ("uq_message_versions_change_hash", ("account_id", "change_hash")),
}

# Indexes this release adds. Repairs, not speculation: until 8.0.0 the messages
# primary key led with id and served resolve_message_chat_id /
# get_chat_id_for_message, which look a message up without naming a chat. The
# key now leads with account_id, so the account-qualified lookup needs its own
# index or it walks every message ever archived.
NEW_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("idx_messages_account_msgid", "messages", ("account_id", "id")),
)

# The nine foreign keys of this schema, in their 8.0.0 shape. Eight are rebuilt
# with account_id prepended to both sides; fk_reactions_user is unchanged
# because users stays global, and it is listed so it is asserted afterwards too.
# (name, table, columns, referred table, referred columns, ondelete)
FOREIGN_KEYS: tuple[tuple[str, str, tuple[str, ...], str, tuple[str, ...], str | None], ...] = (
    ("fk_messages_chat", "messages", ("account_id", "chat_id"), "chats", ("account_id", "id"), None),
    ("fk_sync_status_chat", "sync_status", ("account_id", "chat_id"), "chats", ("account_id", "id"), None),
    ("fk_forum_topics_chat", "forum_topics", ("account_id", "chat_id"), "chats", ("account_id", "id"), "CASCADE"),
    (
        "fk_folder_members_folder",
        "chat_folder_members",
        ("account_id", "folder_id"),
        "chat_folders",
        ("account_id", "id"),
        "CASCADE",
    ),
    (
        "fk_folder_members_chat",
        "chat_folder_members",
        ("account_id", "chat_id"),
        "chats",
        ("account_id", "id"),
        "CASCADE",
    ),
    (
        "fk_media_message",
        "media",
        ("account_id", "message_id", "chat_id"),
        "messages",
        ("account_id", "id", "chat_id"),
        "CASCADE",
    ),
    (
        "fk_reaction_message",
        "reactions",
        ("account_id", "message_id", "chat_id"),
        "messages",
        ("account_id", "id", "chat_id"),
        None,
    ),
    (
        "fk_message_versions_message",
        "message_versions",
        ("account_id", "message_id", "chat_id"),
        "messages",
        ("account_id", "id", "chat_id"),
        "CASCADE",
    ),
    ("fk_reactions_user", "reactions", ("user_id",), "users", ("id",), "SET NULL"),
)

# The eight that this migration drops and recreates. fk_reactions_user is
# untouched: users keeps PK(id), so nothing about that constraint changes.
REBUILT_FOREIGN_KEYS = tuple(spec for spec in FOREIGN_KEYS if spec[0] != "fk_reactions_user")

# Parent before child. Every rebuild below writes its own REFERENCES clause
# rather than inheriting the reflected one, so with foreign key enforcement off
# - as this project ships - the finished schema is the same in any order, and
# that was measured rather than assumed. The order is kept because it is the one
# that stays correct the day enforcement is switched on.
SQLITE_REBUILD_ORDER: tuple[str, ...] = (
    "chats",
    "chat_folders",
    "messages",
    "sync_status",
    "forum_topics",
    "chat_folder_members",
    "media",
    "reactions",
    "message_versions",
)

# Viewer identities. They are entitled to accounts and chats; they do not belong
# to one, so they get no account_id. Value is the primary-key column used to
# address a row during the entitlement conversion.
ENTITLEMENT_TABLES: dict[str, str] = {
    "viewer_accounts": "id",
    "viewer_sessions": "token",
    "viewer_tokens": "id",
    "push_subscriptions": "id",
}
ENTITLEMENT_COLUMNS: tuple[str, ...] = ("allowed_accounts", "allowed_chat_refs")

# secrets.token_urlsafe(16) is always exactly 22 characters over 128 bits. Kept
# in step with src/db/models.py:new_chat_ref; a migration must not import the
# ORM, which is free to move on without it.
CHAT_REF_BYTES = 16
CHAT_REF_LENGTH = 22


# ---------------------------------------------------------------------------
# Reading the live schema
# ---------------------------------------------------------------------------


def _tables(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _pk_columns(inspector: sa.Inspector, table: str) -> list[str]:
    if table not in inspector.get_table_names():
        return []
    return list(inspector.get_pk_constraint(table).get("constrained_columns") or [])


def _unique_columns(inspector: sa.Inspector, table: str, name: str) -> list[str] | None:
    """Columns of the named unique constraint, or None when it does not exist."""
    if table not in inspector.get_table_names():
        return None
    for constraint in inspector.get_unique_constraints(table):
        if constraint.get("name") == name:
            return list(constraint.get("column_names") or [])
    return None


def _live_foreign_keys(inspector: sa.Inspector, table: str) -> list[dict]:
    if table not in inspector.get_table_names():
        return []
    return list(inspector.get_foreign_keys(table))


def _reshape_pending(inspector: sa.Inspector) -> bool:
    """True while any key in this schema still lacks its account dimension."""
    for table, columns in NEW_PRIMARY_KEYS.items():
        if table in inspector.get_table_names() and set(_pk_columns(inspector, table)) != set(columns):
            return True
    for table, (name, columns) in NEW_UNIQUE_CONSTRAINTS.items():
        live = _unique_columns(inspector, table, name)
        if table in inspector.get_table_names() and (live is None or set(live) != set(columns)):
            return True
    for name, table, columns, referred, _referred_columns, _ondelete in REBUILT_FOREIGN_KEYS:
        if table not in inspector.get_table_names():
            continue
        matching = [
            fk
            for fk in _live_foreign_keys(inspector, table)
            if fk.get("referred_table") == referred and set(fk.get("constrained_columns") or ()) == set(columns)
        ]
        if not any(fk.get("name") == name for fk in matching):
            return True
    return False


# ---------------------------------------------------------------------------
# Prechecks. These run before anything is written and leave the database exactly
# as they found it. Their failure is the intended outcome, not an accident.
# ---------------------------------------------------------------------------


def _sqlite_main_database_path(conn: sa.Connection) -> str | None:
    for _seq, name, path in conn.exec_driver_sql("PRAGMA database_list").fetchall():
        if name == "main" and path:
            return str(path)
    return None


def _sqlite_footprint(path: str) -> int:
    """Bytes the archive currently occupies, main file plus its WAL."""
    total = 0
    for candidate in (path, f"{path}-wal"):
        try:
            total += os.path.getsize(candidate)
        except OSError:
            continue
    return total


def _precheck_disk_space(path: str) -> None:
    """Refuse to start a rebuild that cannot fit on the disk.

    Inside one transaction SQLite cannot checkpoint, so the WAL absorbs the
    entire rewrite: peak on-disk measured 2.34x at 400k messages and 2.36x at
    1.6M. Running out of space mid-rebuild is safe (the transaction rolls back)
    but the container will not start until space is freed, so failing here with
    both numbers is strictly better than failing there with neither.

    Sizes only, never the path: paths in this project carry chat ids.
    """
    footprint = _sqlite_footprint(path)
    if footprint <= 0:
        return
    required = footprint * SQLITE_DISK_HEADROOM
    free = shutil.disk_usage(os.path.dirname(path) or ".").free
    if free >= required:
        return
    raise RuntimeError(
        "Migration 022 needs about "
        f"{required / (1024 * 1024):.1f} MiB free to rebuild this archive "
        f"({SQLITE_DISK_HEADROOM}x its current {footprint / (1024 * 1024):.1f} MiB, because SQLite's "
        f"write-ahead log holds the whole rewrite), and only {free / (1024 * 1024):.1f} MiB is free. "
        "Free some space and start the container again. Nothing has been changed."
    )


def _precheck_foreign_keys_are_off(conn: sa.Connection) -> None:
    """Make sure SQLite is not enforcing foreign keys before any table is dropped.

    This is the difference between a rebuild and a disaster. With
    ``PRAGMA foreign_keys = ON``, ``DROP TABLE chats`` performs an implicit
    ``DELETE FROM chats`` first, which fires the ON DELETE CASCADE on
    forum_topics and chat_folder_members and deletes their rows for real - and
    they are not coming back from the ``_new`` table, because they were deleted
    out of the table that was already copied. It is off by default and
    ``alembic/env.py`` sets no pragmas, so today this is a no-op; it is here
    because a single line added to env.py in some future release would otherwise
    turn this migration into silent data loss.

    A PRAGMA is ignored inside a transaction, which is exactly why this runs in
    the prechecks, before the statement that opens one.
    """
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(
            "Migration 022 rebuilds tables that other tables reference and cannot run while SQLite is "
            "enforcing foreign keys, because dropping a parent table would delete its children first. "
            "Nothing has been changed."
        )


def _claim_exclusive_access(conn: sa.Connection) -> bool:
    """Ask SQLite for exclusive use of the archive for the whole rebuild.

    Not a poll - a claim. Every table in the database is about to be replaced,
    and a viewer holding the same file open comes up serving a schema that is
    being swapped underneath it, while ``create_all``'s own failure handler
    downgrades "database is locked" to a warning so it never notices.
    ``locking_mode=EXCLUSIVE`` refuses to take the lock at all while another
    connection is attached, and then keeps that connection out for the duration
    instead of leaving a gap between the check and the work.

    The obvious version of this check - the same pragma on a SECOND connection -
    is wrong, and measurably so: in WAL mode every attached connection holds the
    write-ahead index, so a probe connection sees Alembic's OWN connection and
    refuses every real upgrade there is. Asking on the connection doing the work
    is what makes the answer mean "somebody else".

    Only applied to an archive that holds data. A database with no chats and no
    messages has nothing to protect, and refusing there would break a first-ever
    start, where the viewer container legitimately comes up at the same time.
    The lock is actually taken by the first write, which is why the message for
    it lives with the statement that opens the transaction.
    """
    if not _sqlite_has_rows(conn):
        return False
    conn.exec_driver_sql("PRAGMA locking_mode=EXCLUSIVE")
    return True


BUSY_MESSAGE = (
    "Migration 022 rebuilds every table in this archive and another process is holding the database "
    "open, so it will not start. Stop the viewer container (and any other process using the archive), "
    "then start this one again. Nothing has been changed."
)


def _sqlite_has_rows(conn: sa.Connection) -> bool:
    inspector = sa.inspect(conn)
    present = _tables(inspector)
    for table in ("chats", "messages"):
        if table in present and conn.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            return True
    return False


def _run_prechecks(conn: sa.Connection) -> bool:
    """Returns True when exclusive access was claimed and must be released.

    PostgreSQL has neither check, and that is a decision, not an omission. Free
    space cannot be read from SQL - the data directory belongs to the server,
    not to this client - and the PostgreSQL path adds a column as catalog
    metadata rather than rewriting the table, so it grows the database by
    single-digit percent instead of tripling a file. Concurrent connections are
    safe there too: the whole migration is one transaction holding ACCESS
    EXCLUSIVE, other sessions block instead of racing, ``env.py`` already takes a
    transaction-scoped advisory lock against a second migrator, and
    ``create_all`` never runs against PostgreSQL. Refusing on a connection count
    would fail every real upgrade, because the viewer container connects the
    moment it starts.
    """
    if conn.dialect.name != "sqlite":
        return False
    _precheck_foreign_keys_are_off(conn)
    path = _sqlite_main_database_path(conn)
    # An in-memory database has no file to measure and no other process that
    # could hold it; the exclusive claim below is still correct there.
    if path and path != ":memory:":
        _precheck_disk_space(path)
    return _claim_exclusive_access(conn)


# ---------------------------------------------------------------------------
# Steps shared by both backends
# ---------------------------------------------------------------------------


def _open_write_transaction(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Emit the DML that has to come before any DDL on SQLite.

    See point 1 of the module docstring. Rewriting ``alembic_version``'s row with
    its own value is a real DML statement, so pysqlite opens its implicit
    transaction here and every ``CREATE TABLE`` below is inside it - and it
    leaves nothing behind if this migration rolls back.

    It is also where the exclusive lock claimed above is actually taken, so a
    database another process is holding open reports itself here, before a
    single byte has been written.
    """
    if "alembic_version" not in _tables(inspector):
        return
    try:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = version_num"))
    except sa.exc.OperationalError as exc:
        if conn.dialect.name == "sqlite" and "locked" in str(exc).lower():
            raise RuntimeError(BUSY_MESSAGE) from exc
        raise


def _create_accounts_table(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Create the registry, then seed the account every existing row belongs to.

    The two halves are guarded separately on purpose. A viewer running 8.0.0
    ``create_all`` against a 7.x SQLite file creates ``accounts`` and nothing
    else, so a guard that skipped the INSERT because the table already existed
    would leave the archive with no account at all.
    """
    if "accounts" not in _tables(inspector):
        op.create_table(
            "accounts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("label", sa.String(length=255), nullable=True),
            sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    seeded = conn.execute(sa.text("SELECT 1 FROM accounts WHERE id = :id"), {"id": DEFAULT_ACCOUNT_ID}).first()
    if not seeded:
        conn.execute(
            sa.text("INSERT INTO accounts (id, label, telegram_user_id) VALUES (:id, :label, NULL)"),
            {"id": DEFAULT_ACCOUNT_ID, "label": DEFAULT_ACCOUNT_LABEL},
        )

    if conn.dialect.name == "postgresql":
        # The seed writes id explicitly, which does not advance the SERIAL
        # sequence - the next account added would collide on id 1.
        conn.execute(
            sa.text(
                "SELECT setval(pg_get_serial_sequence('accounts', 'id'), (SELECT COALESCE(MAX(id), 1) FROM accounts))"
            )
        )


def _create_placeholder_chats(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Give every orphaned row a parent chat before anything writes to its table.

    Archives that lived through Telegram's group→supergroup renumbering — or a
    bulk copy that ran with enforcement disabled, which is how the SQLite→
    PostgreSQL path moves data — hold messages, sync rows, topics and folder
    members whose ``chats`` row is long gone. PostgreSQL's ``convalidated``
    flag still reads true for the 7.x constraint over such rows, because a
    trigger-bypassing copy never re-checks it; recreating ``fk_messages_chat``
    below re-checks every row and used to abort the whole upgrade on the first
    orphan. Refusing was the wrong answer: the rows are the user's history, and
    a rollback keeps them hostage on 7.x forever.

    So, before ref minting and before any statement writes to a table that
    references ``chats``, every distinct chat id that ``messages``,
    ``sync_status``, ``forum_topics`` or ``chat_folder_members`` still points
    at and ``chats`` no longer holds gets a placeholder row. Type comes from
    the id pattern alone (supergroup below -10^12, group for other negatives,
    private otherwise), the title is empty, the cursors are zero and the
    timestamps are the database's now — never a value derived from message
    content. Running right after ``_create_accounts_table`` is what lets the
    stubs ride the same machinery as every real chat: ``_add_account_id_columns``
    backfills them to account 1, ``_mint_chat_refs`` mints their ref, and the
    recreated foreign keys cover them — after which the formerly orphaned rows
    are first-class and the viewer serves them under a placeholder-titled chat.

    One INSERT..SELECT, identical on both backends, idempotent by its
    NOT EXISTS. Only columns the live pre-rebuild table actually has are named,
    with values every 020-era NOT NULL column accepts; everything else keeps
    its default or NULL.
    """
    present = _tables(inspector)
    if "chats" not in present:
        return
    sources = [table for table in CHATS_REFERENCING_TABLES if table in present]
    if not sources:
        return

    placeholder = {
        "id": "missing.chat_id",
        "type": (
            f"CASE WHEN missing.chat_id < {SUPERGROUP_ID_CEILING} THEN 'supergroup' "
            "WHEN missing.chat_id < 0 THEN 'group' ELSE 'private' END"
        ),
        "title": "''",
        "is_forum": "0",
        "is_archived": "0",
        "last_synced_message_id": "0",
        "created_at": "CURRENT_TIMESTAMP",
        "updated_at": "CURRENT_TIMESTAMP",
    }
    live = _column_names(inspector, "chats")
    columns = [column for column in placeholder if column in live]
    referenced = " UNION ".join(f'SELECT chat_id FROM "{table}"' for table in sources)
    names = ", ".join(f'"{column}"' for column in columns)
    values = ", ".join(placeholder[column] for column in columns)
    created = conn.execute(
        sa.text(
            f"INSERT INTO chats ({names}) "
            f"SELECT {values} FROM ({referenced}) AS missing "
            "WHERE missing.chat_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM chats WHERE chats.id = missing.chat_id)"
        )
    ).rowcount
    if created:
        # Count only - a chat id is PII in this project's logs.
        logger.info("022: created %d placeholder chat row(s) for rows whose chat no longer exists", created)


def _add_account_id_columns(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Add the column itself, backfilled to account 1 by its own default.

    ``NOT NULL DEFAULT '1'`` is catalog-only on PostgreSQL 11+ and on SQLite
    (0.3 ms on 2M rows, measured), so no table is rewritten here. The default
    stays: it is what lets a writer that does not yet name an account keep
    working against this schema.
    """
    present = _tables(inspector)
    for table in ACCOUNT_ID_TABLES:
        if table not in present or "account_id" in _column_names(inspector, table):
            continue
        op.add_column(
            table,
            sa.Column("account_id", sa.Integer(), nullable=False, server_default=str(DEFAULT_ACCOUNT_ID)),
        )


def _mint_chat_refs(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Add ``chats.ref`` and give every chat one.

    Added nullable and filled here; the NOT NULL and the unique constraint are
    applied by each backend's reshape, which is rebuilding the table anyway on
    SQLite. Values are generated in Python so both backends and the ORM's own
    default produce byte-identical tokens - SQLite has no base64 function, and
    two spellings of "opaque token" would be one too many.
    """
    if "chats" not in _tables(inspector):
        return
    if "ref" not in _column_names(inspector, "chats"):
        op.add_column("chats", sa.Column("ref", sa.String(length=CHAT_REF_LENGTH), nullable=True))

    rows = conn.execute(sa.text("SELECT account_id, id FROM chats WHERE ref IS NULL")).fetchall()
    if not rows:
        return
    taken = {ref for (ref,) in conn.execute(sa.text("SELECT ref FROM chats WHERE ref IS NOT NULL")).fetchall() if ref}
    updates = []
    for account_id, chat_id in rows:
        ref = secrets.token_urlsafe(CHAT_REF_BYTES)
        while ref in taken:  # pragma: no cover - 128 bits; the loop is the proof, not the path
            ref = secrets.token_urlsafe(CHAT_REF_BYTES)
        taken.add(ref)
        updates.append({"ref": ref, "account_id": account_id, "chat_id": chat_id})
    conn.execute(sa.text("UPDATE chats SET ref = :ref WHERE account_id = :account_id AND id = :chat_id"), updates)
    logger.info("022: minted %d chat ref(s)", len(updates))


def _add_entitlement_columns(conn: sa.Connection, inspector: sa.Inspector) -> None:
    present = _tables(inspector)
    for table in ENTITLEMENT_TABLES:
        if table not in present:
            continue
        existing = _column_names(inspector, table)
        for column in ENTITLEMENT_COLUMNS:
            if column not in existing:
                op.add_column(table, sa.Column(column, sa.Text(), nullable=True))


def _convert_entitlements(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Restate every restricted viewer's grant in the new columns.

    Leaving them NULL would be the one failure this whole design exists to
    prevent: NULL means "no restriction", so a viewer restricted to two chats in
    7.x would come back entitled to everything. A row whose payload cannot be
    read at all is converted to an empty grant, which denies rather than grants.

    Unrestricted rows (allowed_chat_ids IS NULL) keep NULL in both new columns,
    which is the same "everything" they already meant.
    """
    present = _tables(inspector)
    if "chats" not in present:
        return
    ref_by_chat_id = {
        chat_id: ref
        for chat_id, ref in conn.execute(
            sa.text("SELECT id, ref FROM chats WHERE account_id = :account"), {"account": DEFAULT_ACCOUNT_ID}
        ).fetchall()
    }
    converted = 0
    unreadable = 0
    for table, key_column in ENTITLEMENT_TABLES.items():
        if table not in present:
            continue
        columns = _column_names(inspector, table)
        if not {"allowed_chat_ids", *ENTITLEMENT_COLUMNS}.issubset(columns):
            continue
        rows = conn.execute(
            sa.text(
                f"SELECT {key_column}, allowed_chat_ids FROM {table} "
                "WHERE allowed_chat_ids IS NOT NULL "
                "AND allowed_accounts IS NULL AND allowed_chat_refs IS NULL"
            )
        ).fetchall()
        for key, payload in rows:
            try:
                chat_ids = json.loads(payload)
            except TypeError, ValueError:
                chat_ids = None
            if not isinstance(chat_ids, list):
                accounts_json, refs_json = "[]", "[]"
                unreadable += 1
            else:
                refs = []
                for entry in chat_ids:
                    try:
                        chat_id = int(entry)
                    except TypeError, ValueError:
                        continue
                    ref = ref_by_chat_id.get(chat_id)
                    if ref:
                        refs.append(ref)
                accounts_json, refs_json = json.dumps([DEFAULT_ACCOUNT_ID]), json.dumps(refs)
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET allowed_accounts = :accounts, allowed_chat_refs = :refs "
                    f"WHERE {key_column} = :key"
                ),
                {"accounts": accounts_json, "refs": refs_json, "key": key},
            )
            converted += 1
    if converted:
        # Counts only - a grant payload is a list of chat ids.
        logger.info(
            "022: converted %d restricted viewer grant(s) to the account-qualified columns "
            "(%d had an unreadable payload and were converted to an empty grant)",
            converted,
            unreadable,
        )


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _drop_rebuilt_foreign_keys(inspector: sa.Inspector) -> None:
    """Drop, by their live names, exactly the eight constraints being replaced.

    Discovery is by shape (this table -> that table), not by name, because 7.x
    left five of them to PostgreSQL to name (``messages_chat_id_fkey`` and
    friends). Dropping them explicitly is the whole point: the shortcut,
    ``ALTER TABLE messages DROP CONSTRAINT messages_pkey CASCADE``, succeeds and
    deletes ``fk_media_message`` permanently, reporting it only as a NOTICE.
    """
    for name, table, columns, referred, _referred_columns, _ondelete in REBUILT_FOREIGN_KEYS:
        for fk in _live_foreign_keys(inspector, table):
            if fk.get("referred_table") != referred:
                continue
            if set(fk.get("constrained_columns") or ()) == set(columns) and fk.get("name") == name:
                continue  # already in its 8.0.0 shape
            if fk.get("name"):
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def _upgrade_postgresql(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)

    # chats.ref was added nullable so it could be filled; tighten it now.
    if "ref" in _column_names(inspector, "chats"):
        chats_columns = {column["name"]: column for column in inspector.get_columns("chats")}
        if chats_columns["ref"]["nullable"]:
            op.alter_column("chats", "ref", existing_type=sa.String(length=CHAT_REF_LENGTH), nullable=False)

    _drop_rebuilt_foreign_keys(inspector)
    inspector = sa.inspect(conn)

    for table, columns in NEW_PRIMARY_KEYS.items():
        if table not in _tables(inspector) or set(_pk_columns(inspector, table)) == set(columns):
            continue
        constraint = inspector.get_pk_constraint(table)
        if constraint.get("name"):
            op.drop_constraint(constraint["name"], table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, list(columns))

    for table, (name, columns) in NEW_UNIQUE_CONSTRAINTS.items():
        if table not in _tables(inspector):
            continue
        live = _unique_columns(inspector, table, name)
        if live is not None and set(live) == set(columns):
            continue
        if live is not None:
            op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, list(columns))

    inspector = sa.inspect(conn)
    for name, table, columns, referred, referred_columns, ondelete in REBUILT_FOREIGN_KEYS:
        if table not in _tables(inspector):
            continue
        if any(fk.get("name") == name for fk in _live_foreign_keys(inspector, table)):
            continue
        op.create_foreign_key(name, table, referred, list(columns), list(referred_columns), ondelete=ondelete)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def _sqlite_index_ddl(conn: sa.Connection, table: str) -> list[str]:
    """The CREATE INDEX statements of ``table``, verbatim.

    Replaying the stored DDL rather than reflecting and re-declaring is what
    keeps ``idx_messages_chat_date_desc``'s DESC ordering: SQLite reflection
    reports an index's columns but not their sort order, and the parity gate
    compares column names only, so a reflected rebuild would silently downgrade
    that index to ASC and nothing would report it. Rows with a NULL sql are the
    implicit indexes behind UNIQUE constraints; those come back with the table.
    """
    rows = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :t AND sql IS NOT NULL"),
        {"t": table},
    ).fetchall()
    return [row[0] for row in rows]


def _fk_stub_metadata(meta: sa.MetaData, table: str) -> None:
    """Give the rebuilt table's foreign keys something to resolve against.

    Only the referenced column NAMES reach the emitted DDL, so a stub is enough
    and is deterministic - reflecting the real parent would drag its own
    constraints into this MetaData for no gain.
    """
    for _name, source, _columns, referred, referred_columns, _ondelete in FOREIGN_KEYS:
        if source != table or referred in meta.tables:
            continue
        sa.Table(referred, meta, *[sa.Column(column, sa.Integer(), primary_key=True) for column in referred_columns])


def _rebuild_sqlite_table(conn: sa.Connection, table: str) -> None:
    """Copy ``table`` through a new one carrying the 8.0.0 keys.

    Columns are taken from the live schema, so their types, nullability and
    defaults come from the database itself and cannot drift from what earlier
    migrations built. Only the keys and constraints are declared here.
    """
    inspector = sa.inspect(conn)
    columns = inspector.get_columns(table)
    index_ddl = _sqlite_index_ddl(conn, table)
    temp = f"{table}{TEMP_SUFFIX}"

    meta = sa.MetaData()
    _fk_stub_metadata(meta, table)

    elements: list = []
    for column in columns:
        kwargs: dict = {"nullable": column["nullable"]}
        if column.get("default") is not None:
            kwargs["server_default"] = sa.text(str(column["default"]))
        if table == "chats" and column["name"] == "ref":
            kwargs["nullable"] = False  # filled by _mint_chat_refs above
        elements.append(sa.Column(column["name"], column["type"], **kwargs))

    primary_key = NEW_PRIMARY_KEYS.get(table) or tuple(_pk_columns(inspector, table))
    elements.append(sa.PrimaryKeyConstraint(*primary_key))

    if table in NEW_UNIQUE_CONSTRAINTS:
        name, unique_columns = NEW_UNIQUE_CONSTRAINTS[table]
        elements.append(sa.UniqueConstraint(*unique_columns, name=name))

    for name, source, fk_columns, referred, referred_columns, ondelete in FOREIGN_KEYS:
        if source != table:
            continue
        elements.append(
            sa.ForeignKeyConstraint(
                list(fk_columns),
                [f"{referred}.{column}" for column in referred_columns],
                name=name,
                ondelete=ondelete,
            )
        )

    # Unreachable with the statement order this migration actually has - the
    # transaction is open well before the first rebuild, so a crash rolls this
    # table away with everything else, and the crash matrix confirms no run at
    # any of its 200-plus interruption points leaves one behind. It is kept as
    # one line of insurance against a future reordering, because the failure it
    # would prevent is a permanent "table already exists" startup loop.
    conn.execute(sa.text(f'DROP TABLE IF EXISTS "{temp}"'))
    sa.Table(temp, meta, *elements).create(conn)

    column_list = ", ".join(f'"{column["name"]}"' for column in columns)
    conn.execute(sa.text(f'INSERT INTO "{temp}" ({column_list}) SELECT {column_list} FROM "{table}"'))
    conn.execute(sa.text(f'DROP TABLE "{table}"'))
    conn.execute(sa.text(f'ALTER TABLE "{temp}" RENAME TO "{table}"'))
    for statement in index_ddl:
        conn.execute(sa.text(statement))


def _sqlite_foreign_key_violations(conn: sa.Connection) -> int | None:
    """Rows failing a foreign key, or None when the check itself cannot run."""
    try:
        return len(conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall())
    except sa.exc.DatabaseError:
        return None


def _upgrade_sqlite(conn: sa.Connection) -> None:
    inspector = sa.inspect(conn)
    present = _tables(inspector)

    # Establish the baseline before touching anything: an archive that already
    # has orphan rows must not be blocked from upgrading by them, but a
    # violation this rebuild introduces has to be fatal.
    violations_before = _sqlite_foreign_key_violations(conn)

    # Renaming over a table that other tables reference makes modern SQLite
    # re-validate and rewrite those references. The legacy behaviour is what
    # migration 005 and 021 already use for this table set.
    conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        for table in SQLITE_REBUILD_ORDER:
            if table not in present:
                continue
            inspector = sa.inspect(conn)
            wanted_pk = NEW_PRIMARY_KEYS.get(table)
            if wanted_pk and set(_pk_columns(inspector, table)) != set(wanted_pk):
                _rebuild_sqlite_table(conn, table)
                continue
            if table in NEW_UNIQUE_CONSTRAINTS:
                name, columns = NEW_UNIQUE_CONSTRAINTS[table]
                live = _unique_columns(inspector, table, name)
                if live is None or set(live) != set(columns):
                    _rebuild_sqlite_table(conn, table)
                    continue
            if any(
                spec[1] == table and not any(fk.get("name") == spec[0] for fk in _live_foreign_keys(inspector, table))
                for spec in REBUILT_FOREIGN_KEYS
            ):
                _rebuild_sqlite_table(conn, table)
    finally:
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")

    violations_after = _sqlite_foreign_key_violations(conn)
    if violations_after is None:
        # The pragma raises "foreign key mismatch" when a REFERENCES clause names
        # a key that no longer exists - exactly what a wrong rebuild order
        # produces, and it must not be allowed to commit.
        raise RuntimeError(
            "Migration 022: PRAGMA foreign_key_check could not complete after the rebuild, which means a "
            "foreign key now references a key that does not exist. Nothing has been committed."
        )
    if violations_before is not None and violations_after > violations_before:
        raise RuntimeError(
            "Migration 022: the rebuild introduced "
            f"{violations_after - violations_before} foreign key violation(s) "
            f"({violations_before} existed before it). Nothing has been committed."
        )
    if violations_after:
        logger.info(
            "022: %d pre-existing foreign key violation(s) carried over unchanged; the rebuild added none",
            violations_after,
        )


# ---------------------------------------------------------------------------
# Result assertion
# ---------------------------------------------------------------------------


def _create_new_indexes(conn: sa.Connection, inspector: sa.Inspector) -> None:
    """Add this release's new indexes, after any rebuild has replayed the old ones."""
    present = _tables(inspector)
    for name, table, columns in NEW_INDEXES:
        if table not in present:
            continue
        if name in {index["name"] for index in inspector.get_indexes(table)}:
            continue
        op.create_index(name, table, list(columns))


def _assert_foreign_keys(conn: sa.Connection) -> None:
    """Every one of the nine constraints must exist, by name, with its columns.

    This is the guard against the failure that is otherwise invisible: on
    PostgreSQL a CASCADE drop deletes a foreign key with only a NOTICE, and on
    SQLite a rebuild can quietly lose one. Both would leave an archive whose
    referential integrity is gone with nothing having reported an error.
    """
    inspector = sa.inspect(conn)
    present = _tables(inspector)
    missing = []
    for name, table, columns, referred, _referred_columns, _ondelete in FOREIGN_KEYS:
        if table not in present:
            continue
        if not any(
            fk.get("name") == name
            and fk.get("referred_table") == referred
            and list(fk.get("constrained_columns") or ()) == list(columns)
            for fk in _live_foreign_keys(inspector, table)
        ):
            missing.append(name)
    if missing:
        raise RuntimeError(
            f"Migration 022 did not leave these foreign keys in place: {', '.join(sorted(missing))}. "
            "Nothing has been committed."
        )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "chats" not in _tables(inspector):
        # Nothing to key by account yet. A database this early is either being
        # built from base by this same run or is not an archive at all.
        return

    # A schema create_all provisioned from current ORM metadata reaches this
    # migration already in the 8.0.0 shape and skips straight past the rebuild.
    if _reshape_pending(inspector) or "accounts" not in _tables(inspector):
        exclusive = _run_prechecks(conn)
        try:
            _open_write_transaction(conn, inspector)
            _create_accounts_table(conn, sa.inspect(conn))
            _create_placeholder_chats(conn, sa.inspect(conn))
            _add_account_id_columns(conn, sa.inspect(conn))
            _mint_chat_refs(conn, sa.inspect(conn))
            _add_entitlement_columns(conn, sa.inspect(conn))
            _convert_entitlements(conn, sa.inspect(conn))

            if conn.dialect.name == "postgresql":
                _upgrade_postgresql(conn)
            else:
                _upgrade_sqlite(conn)
        finally:
            if exclusive:
                # Hands the archive back at the end of this transaction, so the
                # viewer can attach again without waiting for the process to
                # exit. A raise here is not swallowed - only the mode is reset.
                conn.exec_driver_sql("PRAGMA locking_mode=NORMAL")

    _create_new_indexes(conn, sa.inspect(conn))
    _assert_foreign_keys(conn)


def downgrade() -> None:
    """Deliberately not implemented, because a plausible one would be a lie.

    Reversing this is a second full rebuild of the same nine tables, and once a
    second account exists it cannot be done at all: two rows that differ only by
    account_id collapse onto one key, so a downgrade would have to choose which
    account's copy of every shared message, folder and edit history to destroy.

    The rollback that does work needs no code. This migration is one transaction
    on both backends, so an interrupted upgrade leaves a byte-for-byte intact
    7.x database that the previous image still runs. After a successful upgrade
    the way back is the backup taken before it - which the release notes ask for
    explicitly, and which is the only honest answer here.
    """
    raise RuntimeError(
        "Migration 022 cannot be downgraded: the pre-8.0.0 keys cannot identify a row once more than "
        "one account exists. Restore the backup taken before the upgrade instead."
    )
