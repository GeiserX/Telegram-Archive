"""What the v8.0.0 keys buy, proved against real databases on both backends.

Every test here is a collision that a decorative ``account_id`` column would NOT
have prevented. They were built and run against scratch schemas before the
schema was changed, and each one describes silent, permanent loss of a second
account's data — not an error anyone would have seen.

The tests use a real engine on SQLite and on PostgreSQL. They talk to the tables
directly rather than through ``DatabaseAdapter`` on purpose: what is under test
is the shape of the schema, and the adapter's own account plumbing lands in the
change after this one.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import NO_POSTGRES_REASON, REAL_BACKENDS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.models import CHAT_REF_LENGTH, DEFAULT_ACCOUNT_ID, Base, new_chat_ref  # noqa: E402

WHEN = datetime(2026, 8, 15, 12, 0, 0)
CHAT_ID = -1001234567890
OTHER_ACCOUNT = 2


@pytest.fixture(params=REAL_BACKENDS)
def schema_engine(request, tmp_path, postgres_server_url, make_postgres_database):
    """A real, empty, ORM-built schema, once per backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        _, sync_url = make_postgres_database("telegram_archive_multiaccount")
    else:
        sync_url = f"sqlite:///{tmp_path / 'archive.db'}"
    engine = sa.create_engine(sync_url)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _insert(conn: sa.Connection, table: str, **values) -> None:
    columns = ", ".join(f'"{name}"' for name in values)
    binds = ", ".join(f":{name}" for name in values)
    conn.execute(sa.text(f'INSERT INTO "{table}" ({columns}) VALUES ({binds})'), values)


def _seed_two_accounts(conn: sa.Connection) -> None:
    """Two accounts that archive the same chat — the whole point of the release."""
    for account_id, label in ((DEFAULT_ACCOUNT_ID, "personal"), (OTHER_ACCOUNT, "work")):
        _insert(conn, "accounts", id=account_id, label=label)
        _insert(
            conn,
            "chats",
            account_id=account_id,
            id=CHAT_ID,
            ref=new_chat_ref(),
            type="supergroup",
            title=f"seen by {label}",
            is_forum=1,
            is_archived=0,
            last_synced_message_id=0,
            created_at=WHEN,
            updated_at=WHEN,
        )


def _rows(conn: sa.Connection, sql: str, **params) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sa.text(sql), params).fetchall()]


class TestCollisionsTheOldKeysWouldHaveCaused:
    """Each of these lost data silently before account_id entered the key."""

    def test_both_accounts_keep_their_own_copy_of_the_same_message(self, schema_engine):
        """PK (id, chat_id) dropped the second copy and served the first instead.

        The damage was not the missing row: the second account then READ the
        first account's ``is_outgoing``, so a message it received was displayed
        as one it had sent.
        """
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            for account_id, outgoing, text in ((DEFAULT_ACCOUNT_ID, 1, "sent by me"), (OTHER_ACCOUNT, 0, "received")):
                _insert(
                    conn,
                    "messages",
                    account_id=account_id,
                    id=500,
                    chat_id=CHAT_ID,
                    date=WHEN,
                    text=text,
                    created_at=WHEN,
                    is_outgoing=outgoing,
                    is_pinned=0,
                    is_deleted=0,
                )
            assert _rows(
                conn,
                "SELECT account_id, is_outgoing, text FROM messages WHERE chat_id = :c ORDER BY account_id",
                c=CHAT_ID,
            ) == [(DEFAULT_ACCOUNT_ID, 1, "sent by me"), (OTHER_ACCOUNT, 0, "received")]

    def test_the_old_message_key_really_did_collide(self, schema_engine):
        """The other half of the proof: without the account it is one key.

        A test that only shows the new schema working cannot tell you the old
        one was broken. This one inserts the same (chat_id, id) twice into a
        table keyed the old way and watches it be rejected.
        """
        with schema_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE old_messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, PRIMARY KEY (id, chat_id))"
                )
            )
            _insert(conn, "old_messages", id=500, chat_id=CHAT_ID)
            with pytest.raises(sa.exc.IntegrityError):
                _insert(conn, "old_messages", id=500, chat_id=CHAT_ID)

    def test_both_accounts_keep_their_own_edit_history(self, schema_engine):
        """UNIQUE(change_hash) discarded the second account's history entirely.

        The hash is over the message payload, which is byte-identical for both
        accounts, so every edit either account recorded collapsed onto one row.
        """
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            for account_id in (DEFAULT_ACCOUNT_ID, OTHER_ACCOUNT):
                _insert(
                    conn,
                    "messages",
                    account_id=account_id,
                    id=500,
                    chat_id=CHAT_ID,
                    date=WHEN,
                    created_at=WHEN,
                    is_outgoing=0,
                    is_pinned=0,
                    is_deleted=0,
                )
                _insert(
                    conn,
                    "message_versions",
                    account_id=account_id,
                    message_id=500,
                    chat_id=CHAT_ID,
                    text="before the edit",
                    date=WHEN,
                    change_hash="the-same-hash-for-both-accounts",
                    captured_at=WHEN,
                )
            assert _rows(
                conn,
                "SELECT account_id FROM message_versions WHERE change_hash = :h ORDER BY account_id",
                h="the-same-hash-for-both-accounts",
            ) == [(DEFAULT_ACCOUNT_ID,), (OTHER_ACCOUNT,)]

    def test_the_version_hash_payload_is_still_the_frozen_contract(self):
        """account_id must NOT enter the hash — only the constraint.

        Re-encoding the payload would make every version already stored hash
        differently and be re-inserted as a duplicate of itself. This pins the
        digest so that cannot happen by accident.
        """
        from src.db.adapter import _message_version_hash

        digest = _message_version_hash(CHAT_ID, 500, "before the edit", WHEN)
        assert digest == "423d89b27bd1db2065c717a6c4559d43df148704718bb685837c50cdbf82e177"

    def test_both_accounts_keep_their_own_folder_and_its_members(self, schema_engine):
        """PK(id) destroyed the first account's folder title AND its members.

        Telegram numbers dialog filters per account and everybody's start at 2,
        so this collided for every user who added a second account, on their
        first sweep.
        """
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            for account_id, title in ((DEFAULT_ACCOUNT_ID, "Personal folder"), (OTHER_ACCOUNT, "Work folder")):
                _insert(
                    conn,
                    "chat_folders",
                    account_id=account_id,
                    id=2,
                    title=title,
                    sort_order=0,
                    created_at=WHEN,
                    updated_at=WHEN,
                )
                _insert(conn, "chat_folder_members", account_id=account_id, folder_id=2, chat_id=CHAT_ID)
            assert _rows(conn, "SELECT account_id, title FROM chat_folders ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID, "Personal folder"),
                (OTHER_ACCOUNT, "Work folder"),
            ]
            assert _rows(conn, "SELECT account_id FROM chat_folder_members ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID,),
                (OTHER_ACCOUNT,),
            ]

    def test_both_accounts_keep_their_own_media_row(self, schema_engine):
        """media.id is built as f"{chat_id}_{message_id}_{type}", not a file id.

        Two accounts archiving the same message therefore produce the identical
        string, and the old PK(id) kept exactly one of them.
        """
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            media_id = f"{CHAT_ID}_500_photo"
            for account_id, path in ((DEFAULT_ACCOUNT_ID, "/data/media/a.jpg"), (OTHER_ACCOUNT, "/data/media/b.jpg")):
                _insert(
                    conn,
                    "messages",
                    account_id=account_id,
                    id=500,
                    chat_id=CHAT_ID,
                    date=WHEN,
                    created_at=WHEN,
                    is_outgoing=0,
                    is_pinned=0,
                    is_deleted=0,
                )
                _insert(
                    conn,
                    "media",
                    account_id=account_id,
                    id=media_id,
                    message_id=500,
                    chat_id=CHAT_ID,
                    type="photo",
                    file_path=path,
                    downloaded=1,
                    download_attempts=0,
                    created_at=WHEN,
                )
            assert _rows(conn, "SELECT account_id, file_path FROM media ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID, "/data/media/a.jpg"),
                (OTHER_ACCOUNT, "/data/media/b.jpg"),
            ]

    def test_both_accounts_keep_their_own_reaction(self, schema_engine):
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            _insert(conn, "users", id=77, is_bot=0, created_at=WHEN, updated_at=WHEN)
            for account_id, count in ((DEFAULT_ACCOUNT_ID, 3), (OTHER_ACCOUNT, 9)):
                _insert(
                    conn,
                    "messages",
                    account_id=account_id,
                    id=500,
                    chat_id=CHAT_ID,
                    date=WHEN,
                    created_at=WHEN,
                    is_outgoing=0,
                    is_pinned=0,
                    is_deleted=0,
                )
                _insert(
                    conn,
                    "reactions",
                    account_id=account_id,
                    message_id=500,
                    chat_id=CHAT_ID,
                    emoji="+1",
                    user_id=77,
                    count=count,
                    created_at=WHEN,
                )
            assert _rows(conn, "SELECT account_id, count FROM reactions ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID, 3),
                (OTHER_ACCOUNT, 9),
            ]

    def test_both_accounts_keep_their_own_sync_counter(self, schema_engine):
        """update_sync_status ACCUMULATES message_count on conflict.

        Two accounts collapsing onto PK(chat_id) did not overwrite that counter,
        they summed it, and the archive reported a message count that never
        existed.
        """
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            for account_id, count in ((DEFAULT_ACCOUNT_ID, 120), (OTHER_ACCOUNT, 7)):
                _insert(
                    conn,
                    "sync_status",
                    account_id=account_id,
                    chat_id=CHAT_ID,
                    last_message_id=count,
                    last_sync_date=WHEN,
                    message_count=count,
                )
            assert _rows(conn, "SELECT account_id, message_count FROM sync_status ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID, 120),
                (OTHER_ACCOUNT, 7),
            ]

    def test_both_accounts_keep_their_own_forum_topics(self, schema_engine):
        with schema_engine.begin() as conn:
            _seed_two_accounts(conn)
            for account_id, title in ((DEFAULT_ACCOUNT_ID, "General"), (OTHER_ACCOUNT, "Generale")):
                _insert(
                    conn,
                    "forum_topics",
                    account_id=account_id,
                    id=1,
                    chat_id=CHAT_ID,
                    title=title,
                    is_closed=0,
                    is_pinned=0,
                    is_hidden=0,
                    created_at=WHEN,
                    updated_at=WHEN,
                )
            assert _rows(conn, "SELECT account_id, title FROM forum_topics ORDER BY account_id") == [
                (DEFAULT_ACCOUNT_ID, "General"),
                (OTHER_ACCOUNT, "Generale"),
            ]


class TestTheQuietOne:
    """detect_message_gaps is raw SQL that no type checker will ever object to.

    ``LAG(id) OVER (ORDER BY id) ... WHERE chat_id = ?`` reads every account's
    copy of a chat as one id sequence. Two accounts' sequences interleave inside
    that window, so the gap list it returns is not merely unscoped — it is
    WRONG: real gaps disappear because the other account's ids fill them in.

    The schema cannot fix this on its own; the query has to name the account.
    Both spellings are run here so the difference is a measurement rather than a
    warning, and so the corrected SQL is written down where the change that
    needs it will find it.
    """

    GAP_SQL = """
        SELECT gap_start, gap_end, gap_size FROM (
            SELECT
                LAG(id) OVER (ORDER BY id) AS gap_start,
                id AS gap_end,
                id - LAG(id) OVER (ORDER BY id) AS gap_size
            FROM messages
            WHERE chat_id = :chat_id {account_filter}
        ) gaps
        WHERE gap_size > :threshold
        ORDER BY gap_start
    """

    def _seed_interleaved(self, conn: sa.Connection) -> None:
        _seed_two_accounts(conn)
        # Account 1 archived 100 and 400: a gap of 300 it still has to fill.
        # Account 2 happens to hold 200 and 300, right inside that gap.
        for account_id, ids in ((DEFAULT_ACCOUNT_ID, (100, 400)), (OTHER_ACCOUNT, (200, 300))):
            for message_id in ids:
                _insert(
                    conn,
                    "messages",
                    account_id=account_id,
                    id=message_id,
                    chat_id=CHAT_ID,
                    date=WHEN,
                    created_at=WHEN,
                    is_outgoing=0,
                    is_pinned=0,
                    is_deleted=0,
                )

    def test_the_account_blind_window_hides_a_real_gap(self, schema_engine):
        with schema_engine.begin() as conn:
            self._seed_interleaved(conn)
            gaps = _rows(
                conn,
                self.GAP_SQL.format(account_filter=""),
                chat_id=CHAT_ID,
                threshold=150,
            )
            # 100 -> 200 -> 300 -> 400: every step is 100, so nothing exceeds the
            # threshold and account 1's 300-wide hole is reported as no gap at all.
            assert gaps == []

    def test_naming_the_account_finds_it(self, schema_engine):
        with schema_engine.begin() as conn:
            self._seed_interleaved(conn)
            gaps = _rows(
                conn,
                self.GAP_SQL.format(account_filter="AND account_id = :account_id"),
                chat_id=CHAT_ID,
                threshold=150,
                account_id=DEFAULT_ACCOUNT_ID,
            )
            assert gaps == [(100, 400, 300)]


class TestChatRef:
    def test_a_ref_is_minted_on_insert_without_anyone_asking(self, schema_engine):
        """The ORM's own default does it, so an existing writer needs no change."""
        from src.db.models import Chat

        with schema_engine.begin() as conn:
            _insert(conn, "accounts", id=DEFAULT_ACCOUNT_ID, label="personal")
            conn.execute(
                sa.insert(Chat).values(id=CHAT_ID, type="supergroup", title="minted", created_at=WHEN, updated_at=WHEN)
            )
            (ref,) = conn.execute(sa.text("SELECT ref FROM chats")).first()
        assert len(ref) == CHAT_REF_LENGTH
        assert set(ref) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def test_an_update_never_re_rolls_it(self, schema_engine):
        """A ref that changed on every backup cycle would break every open tab.

        An upsert's ON CONFLICT DO UPDATE branch must not touch the column, and
        the Python-side default gives exactly that for free: it applies to the
        INSERT and to nothing else.
        """
        from src.db.models import Chat

        with schema_engine.begin() as conn:
            _insert(conn, "accounts", id=DEFAULT_ACCOUNT_ID, label="personal")
            conn.execute(
                sa.insert(Chat).values(id=CHAT_ID, type="supergroup", title="first", created_at=WHEN, updated_at=WHEN)
            )
            (before,) = conn.execute(sa.text("SELECT ref FROM chats")).first()
            conn.execute(sa.update(Chat).values(title="renamed"))
            (after,) = conn.execute(sa.text("SELECT ref FROM chats")).first()
        assert before == after

    def test_two_chats_cannot_share_a_ref(self, schema_engine):
        with schema_engine.begin() as conn:
            _insert(conn, "accounts", id=DEFAULT_ACCOUNT_ID, label="personal")
            shared = new_chat_ref()
            _insert(
                conn,
                "chats",
                account_id=DEFAULT_ACCOUNT_ID,
                id=1,
                ref=shared,
                type="private",
                is_forum=0,
                is_archived=0,
                last_synced_message_id=0,
                created_at=WHEN,
                updated_at=WHEN,
            )
            with pytest.raises(sa.exc.IntegrityError):
                _insert(
                    conn,
                    "chats",
                    account_id=DEFAULT_ACCOUNT_ID,
                    id=2,
                    ref=shared,
                    type="private",
                    is_forum=0,
                    is_archived=0,
                    last_synced_message_id=0,
                    created_at=WHEN,
                    updated_at=WHEN,
                )

    def test_refs_do_not_repeat(self):
        assert len({new_chat_ref() for _ in range(2000)}) == 2000


class TestEntitlementColumnsAreNew:
    """A reinterpreted allowed_chat_ids fails OPEN; a new column cannot.

    An unconverted 7.x payload ``[123, 456]`` read as (account 123, chat 456)
    GRANTS access instead of denying it. These columns exist so an unconverted
    row is unmistakably unconverted.
    """

    @pytest.mark.parametrize("table", ["viewer_accounts", "viewer_sessions", "viewer_tokens", "push_subscriptions"])
    def test_both_columns_exist_beside_the_old_one(self, schema_engine, table):
        columns = {column["name"]: column for column in sa.inspect(schema_engine).get_columns(table)}
        assert "allowed_chat_ids" in columns, "the 7.x column must survive, not be renamed"
        for name in ("allowed_accounts", "allowed_chat_refs"):
            assert name in columns
            assert columns[name]["nullable"] is True


class TestWhatDidNotChange:
    def test_users_stay_global(self, schema_engine):
        """Per-account users turns the folder-resolution outerjoin into duplicates.

        get_chats_for_folder_resolution joins User on ``User.id == Chat.id``. With
        users keyed per account and two accounts holding the same person, that
        join returns a row per account and the folder counts silently double.
        """
        assert sa.inspect(schema_engine).get_pk_constraint("users")["constrained_columns"] == ["id"]

    def test_metadata_is_not_split(self, schema_engine):
        assert sa.inspect(schema_engine).get_pk_constraint("metadata")["constrained_columns"] == ["key"]

    def test_media_gained_no_new_column(self, schema_engine):
        """The media URL scheme needs no opaque id: media.id is already unique
        per account, and chats.ref is what takes chat ids out of the URL."""
        columns = {column["name"] for column in sa.inspect(schema_engine).get_columns("media")}
        assert "uid" not in columns and "url_key" not in columns

    def test_a_writer_that_names_no_account_still_lands_somewhere_real(self, schema_engine):
        """Every account_id carries a server-side default of 1.

        This is what lets the single-account writers keep working against the
        new keys while the account is threaded through them.
        """
        with schema_engine.begin() as conn:
            _insert(conn, "accounts", id=DEFAULT_ACCOUNT_ID, label="personal")
            _insert(
                conn,
                "chats",
                id=CHAT_ID,
                ref=new_chat_ref(),
                type="private",
                is_forum=0,
                is_archived=0,
                last_synced_message_id=0,
                created_at=WHEN,
                updated_at=WHEN,
            )
            _insert(
                conn,
                "messages",
                id=1,
                chat_id=CHAT_ID,
                date=WHEN,
                created_at=WHEN,
                is_outgoing=0,
                is_pinned=0,
                is_deleted=0,
            )
            assert _rows(conn, "SELECT account_id FROM messages") == [(DEFAULT_ACCOUNT_ID,)]

    def test_the_accounts_table_has_exactly_three_columns(self, schema_engine):
        """No status, no soft delete, no session name. Nobody has asked for them."""
        columns = {column["name"] for column in sa.inspect(schema_engine).get_columns("accounts")}
        assert columns == {"id", "label", "telegram_user_id"}

    def test_the_account_id_is_a_surrogate_not_a_telegram_user_id(self, schema_engine):
        """A Telegram user id is an identifier this project treats as PII, and it
        would otherwise be copied into every row of every table and every index."""
        with schema_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO accounts (label, telegram_user_id) VALUES ('personal', NULL)"))
            assert _rows(conn, "SELECT id, telegram_user_id FROM accounts") == [(1, None)]


def test_no_account_or_chat_identifier_is_logged_by_the_migration():
    """PII rule: counts and type names only, never an id, a ref or a payload."""
    source = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260815_022_multi_account_keys.py"
    ).read_text(encoding="utf-8")
    logged = [line for line in source.splitlines() if "logger." in line or "raise RuntimeError" in line]
    assert logged, "the guard is worthless if it matches nothing"
    # A log line may name a count of refs; it may never interpolate one, or a
    # chat id, a grant payload, a Telegram user id, or a filesystem path.
    forbidden = (
        "chat_id",
        "chat_ids",
        "allowed_",
        "telegram_user_id",
        "payload",
        "path",
        "%s",
        "{ref",
        "row[",
        "value",
    )
    for line in logged:
        for needle in forbidden:
            assert needle not in line, f"a log or error line may carry {needle}: {line.strip()}"


def test_the_mappers_configure_without_a_relationship_warning():
    """account_id sits in two composite foreign keys on chat_folder_members.

    SQLAlchemy warns when two relationships would both persist the same column,
    and a warning nobody reads is how an ORM-level overwrite gets shipped. The
    membership row's `chat` is viewonly, which says out loud that only `folder`
    writes it.
    """
    import subprocess

    # In a subprocess: configure_mappers() only warns the first time, so doing
    # this in-process would be a check that cannot fail.
    script = (
        "import warnings, sqlalchemy.orm as orm;"
        "warnings.simplefilter('error', orm.exc.sa_exc.SAWarning);"
        "import src.db.models;"
        "orm.configure_mappers()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]

    # And the reason it is quiet, stated directly: exactly one relationship on
    # the membership row may persist its columns.
    from src.db.models import ChatFolderMember

    writers = sorted(rel.key for rel in sa.inspect(ChatFolderMember).relationships if not rel.viewonly)
    assert writers == ["folder"]


def test_the_migration_never_uses_cascade():
    """DROP CONSTRAINT ... CASCADE succeeds while deleting a foreign key, with
    only a NOTICE to say so. Measured on a real server; it must never appear."""
    source = (
        Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260815_022_multi_account_keys.py"
    ).read_text(encoding="utf-8")
    executed = [
        line
        for line in source.splitlines()
        if "op.execute(" in line or "conn.execute(" in line or "exec_driver_sql(" in line
    ]
    assert executed, "the guard is worthless if it matches nothing"
    assert not any("CASCADE" in line.upper() for line in executed)
    assert "op.drop_constraint(" in source, "constraints must be dropped explicitly, by name"
