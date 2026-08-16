"""Gates for Alembic migration 022 — the one that rewrites every table.

For most people running this project the database is the only copy of their
Telegram history, so the bar for this migration is not "it works on my machine":
every claim below is a check that has been shown to go RED when the guard it
covers is removed.

* **The crash matrix** interrupts the migration before every single statement it
  issues — all ~225 of them — and after each one asserts that the data is
  untouched, that ``alembic_version`` has NOT advanced, that no temporary table
  survived, and that replaying converges on the same schema and the same rows.
  It costs roughly 20 seconds, which is the right price for the only proof that
  a power cut mid-upgrade is survivable. Deleting the DML that opens the
  transaction turns it red at 211 of those points, with the ``accounts`` table
  surviving a rollback that was supposed to remove it.
* **Foreign keys** are asserted individually by name on both backends. On
  PostgreSQL a ``DROP ... CASCADE`` deletes one permanently and says so only in
  a NOTICE; on SQLite a rebuild that inherits its reflected constraints leaves
  ``media`` pointing at a key that no longer exists, which
  ``PRAGMA foreign_key_check`` reports as a mismatch.
* **The prechecks** must refuse loudly and change nothing — and, just as
  important, must NOT refuse when they are alone. The first version of the
  exclusive-access check used a second connection, which in WAL mode sees
  Alembic's own connection and would have refused every real upgrade there is.
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = REPO_ROOT / "alembic"
MIGRATION_PATH = ALEMBIC_DIR / "versions" / "20260815_022_multi_account_keys.py"
ENTRYPOINT_PATH = REPO_ROOT / "scripts" / "entrypoint.sh"

WHEN = datetime(2026, 8, 15, 12, 0, 0)
CHAT_IDS = (-1001234567890, -1009876543210, 55501)

# Tables the digest reads, so a comparison cannot silently skip one.
DIGESTED_TABLES = (
    "accounts",
    "app_settings",
    "chat_folder_members",
    "chat_folders",
    "chats",
    "forum_topics",
    "media",
    "message_versions",
    "messages",
    "metadata",
    "push_subscriptions",
    "reactions",
    "sync_status",
    "users",
    "viewer_accounts",
    "viewer_audit_log",
    "viewer_sessions",
    "viewer_tokens",
)


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_022", MIGRATION_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        raise RuntimeError("Unable to load migration 022")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade_to(url: str, target: str = "head") -> None:
    """Run the project's real Alembic environment against ``url``."""
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


# ---------------------------------------------------------------------------
# A small archive with a row in every table this migration touches
# ---------------------------------------------------------------------------


def seed(sync_url: str) -> None:
    engine = sa.create_engine(sync_url)
    try:
        inspector = sa.inspect(engine)
        with engine.begin() as conn:
            for table, rows in _seed_rows().items():
                columns = {column["name"] for column in inspector.get_columns(table)}
                for row in rows:
                    usable = {key: value for key, value in row.items() if key in columns}
                    names = ", ".join(f'"{key}"' for key in usable)
                    binds = ", ".join(f":{key}" for key in usable)
                    conn.execute(sa.text(f'INSERT INTO "{table}" ({names}) VALUES ({binds})'), usable)
    finally:
        engine.dispose()


def _seed_rows() -> dict[str, list[dict]]:
    messages = [
        {
            "id": n,
            "chat_id": chat_id,
            "sender_id": 7001,
            "sender_name": f"S{n}",
            "date": WHEN,
            "text": f"m{n}",
            "raw_data": json.dumps({"n": n}),
            "created_at": WHEN,
            "is_outgoing": n % 2,
            "is_pinned": 0,
            "is_deleted": 0,
        }
        for chat_id in CHAT_IDS
        for n in (1, 2, 3)
    ]
    return {
        "chats": [
            {
                "id": chat_id,
                "type": "supergroup",
                "title": f"c{index}",
                "username": f"u{index}",
                "is_forum": 1 if index == 0 else 0,
                "is_archived": 0,
                "last_synced_message_id": 3,
                "created_at": WHEN,
                "updated_at": WHEN,
            }
            for index, chat_id in enumerate(CHAT_IDS)
        ],
        "users": [{"id": 7001, "username": "someone", "is_bot": 0, "created_at": WHEN, "updated_at": WHEN}],
        "messages": messages,
        "media": [
            {
                "id": f"{CHAT_IDS[0]}_1_photo",
                "message_id": 1,
                "chat_id": CHAT_IDS[0],
                "type": "photo",
                "file_path": "/data/media/x/1_photo.jpg",
                "file_name": "1_photo.jpg",
                "file_size": 10,
                "content_hash": "hash",
                "downloaded": 1,
                "download_attempts": 0,
                "created_at": WHEN,
            },
            # message_id/chat_id NULL: a composite FK with a NULL column is not
            # enforced, so this row has to survive the rebuild too.
            {"id": "metadata_only", "type": "document", "downloaded": 0, "download_attempts": 2, "created_at": WHEN},
        ],
        "reactions": [
            {"message_id": 1, "chat_id": CHAT_IDS[0], "emoji": "+1", "user_id": 7001, "count": 2, "created_at": WHEN},
            {
                "message_id": 1,
                "chat_id": CHAT_IDS[0],
                "emoji": "heart",
                "user_id": None,
                "count": 1,
                "created_at": WHEN,
                "removed_at": WHEN,
            },
        ],
        "message_versions": [
            {
                "message_id": 1,
                "chat_id": CHAT_IDS[0],
                "text": "was",
                "date": WHEN,
                "change_hash": "hash-a",
                "captured_at": WHEN,
            },
        ],
        "sync_status": [
            {"chat_id": chat_id, "last_message_id": 3, "last_sync_date": WHEN, "message_count": 3}
            for chat_id in CHAT_IDS
        ],
        "forum_topics": [
            {
                "id": 1,
                "chat_id": CHAT_IDS[0],
                "title": "General",
                "is_closed": 0,
                "is_pinned": 1,
                "is_hidden": 0,
                "date": WHEN,
                "created_at": WHEN,
                "updated_at": WHEN,
            },
        ],
        "chat_folders": [
            {"id": 2, "title": "Work", "sort_order": 0, "created_at": WHEN, "updated_at": WHEN},
        ],
        "chat_folder_members": [{"folder_id": 2, "chat_id": CHAT_IDS[0]}],
        "metadata": [{"key": "owner_id", "value": "7001"}],
        "app_settings": [{"key": "backup_schedule", "value": "0 3 * * *", "updated_at": WHEN}],
        "viewer_accounts": [
            {
                "username": "restricted",
                "password_hash": "hash",
                "salt": "salt",
                "is_active": 1,
                "no_download": 0,
                "created_at": WHEN,
                "updated_at": WHEN,
                "allowed_chat_ids": json.dumps([CHAT_IDS[0], CHAT_IDS[2]]),
            },
            {
                "username": "unrestricted",
                "password_hash": "hash",
                "salt": "salt",
                "is_active": 1,
                "no_download": 0,
                "created_at": WHEN,
                "updated_at": WHEN,
                "allowed_chat_ids": None,
            },
            {
                "username": "corrupt",
                "password_hash": "hash",
                "salt": "salt",
                "is_active": 1,
                "no_download": 0,
                "created_at": WHEN,
                "updated_at": WHEN,
                "allowed_chat_ids": "{not json",
            },
        ],
        "viewer_tokens": [
            {
                "label": "share",
                "token_hash": "hash",
                "token_salt": "salt",
                "created_by": "master",
                "allowed_chat_ids": json.dumps([CHAT_IDS[1]]),
                "is_revoked": 0,
                "no_download": 1,
                "use_count": 0,
                "created_at": WHEN,
            },
        ],
        "viewer_sessions": [
            {
                "token": "session-restricted",
                "username": "restricted",
                "role": "viewer",
                "allowed_chat_ids": json.dumps([CHAT_IDS[0]]),
                "no_download": 0,
                "created_at": 1.0,
                "last_accessed": 2.0,
            },
            {
                "token": "session-master",
                "username": "master",
                "role": "master",
                "allowed_chat_ids": None,
                "no_download": 0,
                "created_at": 1.0,
                "last_accessed": 2.0,
            },
        ],
        "push_subscriptions": [
            {
                "endpoint": "https://push.invalid/1",
                "p256dh": "key",
                "auth": "auth",
                "chat_id": CHAT_IDS[0],
                "username": "restricted",
                "allowed_chat_ids": json.dumps([CHAT_IDS[0]]),
                "created_at": WHEN,
            },
        ],
        "viewer_audit_log": [
            {"username": "restricted", "role": "viewer", "action": "login", "created_at": WHEN},
        ],
    }


# ---------------------------------------------------------------------------
# Reading a database back
# ---------------------------------------------------------------------------


def sqlite_digest(path: Path) -> dict:
    """Every row of every table, column by column, with refs canonicalised.

    A ref is random per run, so raw values would differ on every replay.
    Rewriting each one to the chat it names keeps the comparison strict: a grant
    pointing at the wrong chat, or a ref that never reached the entitlement
    columns, still shows up as a difference.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        live = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out: dict[str, list[dict]] = {}
        ref_to_chat: dict[str, str] = {}
        if "chats" in live and "ref" in {c[1] for c in conn.execute("PRAGMA table_info(chats)")}:
            ref_to_chat = {row[0]: str(row[1]) for row in conn.execute("SELECT ref, id FROM chats") if row[0]}

        def canonical(value):
            return f"<ref:{ref_to_chat.get(value, '?')}>" if value in ref_to_chat else value

        for table in DIGESTED_TABLES:
            if table not in live:
                continue
            columns = [column[1] for column in conn.execute(f"PRAGMA table_info({table})")]
            order = ", ".join(f'"{column}"' for column in columns)
            rows = []
            for record in conn.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}'):
                row = {column: (None if record[column] is None else str(record[column])) for column in columns}
                if table == "chats" and row.get("ref"):
                    row["ref"] = canonical(row["ref"])
                if row.get("allowed_chat_refs"):
                    try:
                        row["allowed_chat_refs"] = json.dumps(
                            [canonical(r) for r in json.loads(row["allowed_chat_refs"])]
                        )
                    except ValueError:  # pragma: no cover - the migration always writes valid JSON
                        pass
                rows.append(row)
            out[table] = rows
        return out
    finally:
        conn.close()


def orphan_tables(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if row[0].endswith("__new022")
        ]
    finally:
        conn.close()


def run_shipped_stamping_ladder(path: Path) -> str | None:
    """Execute the SQLite stamping block sliced out of the shipped entrypoint.

    The block lives inside a bash double-quoted ``python -c`` heredoc, so its
    embedded quotes are escaped; the slice and the de-escape are the same ones
    tests/test_entrypoint_schema_detection.py already uses. Running the real
    thing is the point: a reimplementation would only ever test itself.
    """
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    start = script.index("database_url = os.getenv('DATABASE_URL', '')")
    end = script.index("conn.close()", start) + len("conn.close()")
    block = script[start:end].replace('\\"', '"')

    namespace: dict = {"os": os, "sqlite3": sqlite3}
    saved_url, saved_path = os.environ.get("DATABASE_URL"), os.environ.get("DB_PATH")
    os.environ.pop("DATABASE_URL", None)
    os.environ["DB_PATH"] = str(path)
    try:
        exec(compile(block, str(ENTRYPOINT_PATH), "exec"), namespace)
    finally:
        if saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_url
        if saved_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = saved_path
    return namespace.get("stamp_version")


def chain_head() -> str:
    """The migration chain's live head, so reached-head assertions survive new revisions."""
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


def alembic_version(path: Path) -> str | None:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        conn.close()


def foreign_keys_by_name(sync_url: str) -> dict[str, tuple[str, tuple[str, ...], str]]:
    engine = sa.create_engine(sync_url)
    try:
        inspector = sa.inspect(engine)
        found = {}
        for table in inspector.get_table_names():
            for fk in inspector.get_foreign_keys(table):
                if fk.get("name"):
                    found[fk["name"]] = (table, tuple(fk["constrained_columns"]), fk["referred_table"])
        return found
    finally:
        engine.dispose()


EXPECTED_FOREIGN_KEYS = {
    "fk_messages_chat": ("messages", ("account_id", "chat_id"), "chats"),
    "fk_sync_status_chat": ("sync_status", ("account_id", "chat_id"), "chats"),
    "fk_forum_topics_chat": ("forum_topics", ("account_id", "chat_id"), "chats"),
    "fk_folder_members_folder": ("chat_folder_members", ("account_id", "folder_id"), "chat_folders"),
    "fk_folder_members_chat": ("chat_folder_members", ("account_id", "chat_id"), "chats"),
    "fk_media_message": ("media", ("account_id", "message_id", "chat_id"), "messages"),
    "fk_reaction_message": ("reactions", ("account_id", "message_id", "chat_id"), "messages"),
    "fk_message_versions_message": ("message_versions", ("account_id", "message_id", "chat_id"), "messages"),
    "fk_reactions_user": ("reactions", ("user_id",), "users"),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pristine_021(tmp_path_factory) -> Path:
    """A seeded SQLite archive at revision 021, built once and copied per test."""
    path = tmp_path_factory.mktemp("pristine") / "archive.db"
    upgrade_to(f"sqlite+aiosqlite:///{path}", "021")
    seed(f"sqlite:///{path}")
    # WAL is what src/db/base.py leaves behind, and it is load-bearing for the
    # exclusive-access gate: in WAL mode every attached connection holds the
    # write-ahead index.
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def archive(pristine_021, tmp_path) -> Path:
    path = tmp_path / "archive.db"
    shutil.copy(pristine_021, path)
    return path


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


class TestRevision(unittest.TestCase):
    def test_it_chains_from_021(self) -> None:
        migration = load_migration()
        self.assertEqual(migration.revision, "022")
        self.assertEqual(migration.down_revision, "021")

    def test_downgrade_refuses_instead_of_pretending(self) -> None:
        """A plausible downgrade would be a lie once a second account exists:
        two rows differing only by account_id collapse onto one key, so it would
        have to choose whose copy to destroy."""
        migration = load_migration()
        with self.assertRaises(RuntimeError) as raised:
            migration.downgrade()
        self.assertIn("backup", str(raised.exception))


class TestCrashMatrix:
    """Interrupt before every statement; nothing may be lost, everything replays."""

    class Interrupted(RuntimeError):
        pass

    def _upgrade(self, path: Path, crash_at: int | None = None) -> int:
        seen = {"n": 0}

        def listener(conn, cursor, statement, parameters, context, executemany):
            seen["n"] += 1
            if crash_at is not None and seen["n"] == crash_at:
                raise self.Interrupted(f"simulated interruption before statement {crash_at}")

        sa.event.listen(sa.engine.Engine, "before_cursor_execute", listener)
        try:
            # Target 022 alone: the matrix's invariants (version never advances,
            # rows untouched, replay converges) bracket THIS migration's single
            # transaction; later chained revisions commit their own transactions
            # and carry their own suites.
            upgrade_to(f"sqlite+aiosqlite:///{path}", "022")
        finally:
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", listener)
        return seen["n"]

    def test_every_interruption_point_is_survivable(self, pristine_021, tmp_path):
        before = sqlite_digest(pristine_021)
        assert alembic_version(pristine_021) == "021"

        clean = tmp_path / "clean.db"
        shutil.copy(pristine_021, clean)
        total = self._upgrade(clean)
        expected = sqlite_digest(clean)
        assert total > 100, "the matrix is only meaningful if the migration really issues statements"

        work = tmp_path / "work.db"
        problems = []
        replays = 0
        for point in range(1, total + 1):
            for suffix in ("", "-wal", "-shm"):
                Path(f"{work}{suffix}").unlink(missing_ok=True)
            shutil.copy(pristine_021, work)
            try:
                self._upgrade(work, crash_at=point)
            except self.Interrupted:
                pass
            except Exception as exc:  # the migration's own refusals count as interruptions too
                if not isinstance(exc, RuntimeError):
                    problems.append(f"{point}: unexpected {type(exc).__name__}")

            if alembic_version(work) != "021":
                problems.append(f"{point}: alembic_version advanced")
            if orphan_tables(work):
                problems.append(f"{point}: left {orphan_tables(work)} behind")
            interrupted_state = sqlite_digest(work)
            unchanged = interrupted_state == before
            if not unchanged:
                problems.append(f"{point}: data changed")

            # Replay whenever the interrupted state is NOT the pristine one -
            # that is the only case where the replay is a different experiment -
            # and at a fixed sample regardless, so the converging half of the
            # matrix is exercised directly rather than only by implication.
            if not unchanged or point % 25 == 0 or point in (1, total):
                replays += 1
                self._upgrade(work)
                if alembic_version(work) != "022":
                    problems.append(f"{point}: replay did not reach 022")
                if sqlite_digest(work) != expected:
                    problems.append(f"{point}: replay did not converge")
            if problems:
                break  # one is enough; the whole point is that there are none
        assert not problems, f"crash matrix failed over {total} interruption points: {problems}"
        assert replays >= 5, "the converging half of the matrix has to actually run"


class TestForeignKeys:
    def test_all_nine_exist_by_name_on_sqlite(self, archive):
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        found = foreign_keys_by_name(f"sqlite:///{archive}")
        for name, expected in sorted(EXPECTED_FOREIGN_KEYS.items()):
            assert found.get(name) == expected, f"{name} is missing or reshaped: {found.get(name)}"

    def test_all_nine_exist_by_name_on_postgresql(self, require_postgres, make_postgres_database):
        """The one that a DROP ... CASCADE would delete with only a NOTICE."""
        async_url, sync_url = make_postgres_database("telegram_archive_migration_022")
        upgrade_to(async_url, "021")
        seed(sync_url)
        upgrade_to(async_url)
        found = foreign_keys_by_name(sync_url)
        for name, expected in sorted(EXPECTED_FOREIGN_KEYS.items()):
            assert found.get(name) == expected, f"{name} is missing or reshaped: {found.get(name)}"

    def test_foreign_key_check_is_clean_afterwards(self, archive):
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        conn = sqlite3.connect(str(archive))
        try:
            # A rebuild that inherited its constraints raises "foreign key
            # mismatch" here rather than returning rows, so both are checked.
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()


class TestPrechecks:
    def test_it_refuses_without_disk_headroom_and_changes_nothing(self, archive, monkeypatch):
        migration = load_migration()
        before = sqlite_digest(archive)

        class Full:
            total = 1 << 40
            used = total
            free = 1

        monkeypatch.setattr(migration.shutil, "disk_usage", lambda _path: Full())
        monkeypatch.setattr(shutil, "disk_usage", lambda _path: Full())
        with pytest.raises(RuntimeError) as raised:
            upgrade_to(f"sqlite+aiosqlite:///{archive}")
        message = str(raised.value)
        assert "MiB" in message
        assert str(archive) not in message, "a path may carry a chat id; report sizes only"
        assert sqlite_digest(archive) == before
        assert alembic_version(archive) == "021"

    @pytest.mark.parametrize("holder_sql", [None, "BEGIN", "BEGIN IMMEDIATE"])
    def test_it_refuses_while_another_connection_holds_the_archive(self, archive, holder_sql):
        before = sqlite_digest(archive)
        holder = sqlite3.connect(str(archive), isolation_level=None)
        holder.execute("SELECT COUNT(*) FROM chats").fetchall()
        if holder_sql:
            holder.execute(holder_sql)
            holder.execute("SELECT COUNT(*) FROM chats").fetchall()
        try:
            with pytest.raises(RuntimeError) as raised:
                upgrade_to(f"sqlite+aiosqlite:///{archive}")
            assert "Stop the viewer" in str(raised.value)
            assert sqlite_digest(archive) == before
            assert alembic_version(archive) == "021"
        finally:
            try:
                holder.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            holder.close()

    def test_it_does_not_refuse_when_it_is_alone_on_a_wal_archive(self, archive):
        """The check that broke the first version of this guard.

        A probe on a SECOND connection sees Alembic's own connection in WAL mode
        and refuses every real upgrade there is. Claiming the lock on the
        connection doing the work is what makes the answer mean "somebody else".
        """
        conn = sqlite3.connect(str(archive))
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.close()
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        assert alembic_version(archive) == chain_head()

    def test_it_does_not_refuse_a_first_ever_start(self, tmp_path):
        """A brand-new install has nothing to protect, and the viewer container
        legitimately comes up against the same empty file at the same time."""
        path = tmp_path / "firstboot.db"
        holder = sqlite3.connect(str(path), isolation_level=None)
        holder.execute("PRAGMA journal_mode=WAL")
        try:
            upgrade_to(f"sqlite+aiosqlite:///{path}")
        finally:
            holder.close()
        assert alembic_version(path) == chain_head()

    def test_it_turns_sqlite_foreign_key_enforcement_off_before_dropping_anything(self, tmp_path):
        """With enforcement on, DROP TABLE chats deletes its children first.

        SQLite performs an implicit DELETE FROM before dropping a table when
        foreign keys are enforced, which fires the ON DELETE CASCADE on
        forum_topics and chat_folder_members for real - out of the table that
        was already copied, so the rows are simply gone.
        """
        migration = load_migration()
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'x.db'}")
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                migration._precheck_foreign_keys_are_off(conn)
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
        finally:
            engine.dispose()

    def test_it_refuses_when_enforcement_cannot_be_turned_off(self, tmp_path):
        """A PRAGMA is ignored inside a transaction, so the read-back is the check."""
        migration = load_migration()
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'y.db'}")
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.exec_driver_sql("CREATE TABLE t (a INTEGER)")
                conn.exec_driver_sql("INSERT INTO t VALUES (1)")  # opens the transaction
                with pytest.raises(RuntimeError) as raised:
                    migration._precheck_foreign_keys_are_off(conn)
                assert "foreign keys" in str(raised.value)
        finally:
            engine.dispose()


class TestResult:
    def test_every_row_lands_on_account_one(self, archive):
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        conn = sqlite3.connect(str(archive))
        try:
            for table in (
                "chats",
                "messages",
                "media",
                "reactions",
                "message_versions",
                "sync_status",
                "forum_topics",
                "chat_folders",
                "chat_folder_members",
            ):
                total, on_one = conn.execute(
                    f"SELECT COUNT(*), SUM(CASE WHEN account_id = 1 THEN 1 ELSE 0 END) FROM {table}"
                ).fetchone()
                assert total and total == on_one, f"{table}: {total} rows, {on_one} on account 1"
            assert conn.execute("SELECT id, label, telegram_user_id FROM accounts").fetchall() == [(1, "default", None)]
        finally:
            conn.close()

    def test_every_chat_gets_its_own_ref(self, archive):
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        conn = sqlite3.connect(str(archive))
        try:
            total, distinct, shortest, longest = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT ref), MIN(LENGTH(ref)), MAX(LENGTH(ref)) FROM chats"
            ).fetchone()
            assert total == len(CHAT_IDS)
            assert distinct == total
            assert (shortest, longest) == (22, 22)
        finally:
            conn.close()

    def test_a_restricted_grant_is_converted_and_an_unrestricted_one_is_left_alone(self, archive):
        """Leaving a restricted row NULL would fail OPEN: NULL means everything."""
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        conn = sqlite3.connect(str(archive))
        try:
            refs = {row[0] for row in conn.execute("SELECT ref FROM chats")}
            rows = dict(
                (row[0], row[1:])
                for row in conn.execute(
                    "SELECT username, allowed_chat_ids, allowed_accounts, allowed_chat_refs FROM viewer_accounts"
                )
            )
            _, accounts, chat_refs = rows["restricted"]
            assert json.loads(accounts) == [1]
            assert set(json.loads(chat_refs)) <= refs
            assert len(json.loads(chat_refs)) == 2

            assert rows["unrestricted"][1] is None and rows["unrestricted"][2] is None

            # An unreadable payload converts to an empty grant, not to "everything".
            assert rows["corrupt"][1] == "[]" and rows["corrupt"][2] == "[]"

            for table in ("viewer_sessions", "viewer_tokens", "push_subscriptions"):
                for old, accounts, chat_refs in conn.execute(
                    f"SELECT allowed_chat_ids, allowed_accounts, allowed_chat_refs FROM {table}"
                ):
                    if old is None:
                        assert accounts is None and chat_refs is None
                    else:
                        assert accounts is not None and chat_refs is not None
        finally:
            conn.close()

    def test_no_row_is_deleted(self, archive):
        before = sqlite_digest(archive)
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        after = sqlite_digest(archive)
        for table, rows in before.items():
            assert len(after[table]) == len(rows), f"{table} lost or gained rows"

    def test_running_it_again_changes_nothing(self, archive):
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        once = sqlite_digest(archive)
        upgrade_to(f"sqlite+aiosqlite:///{archive}")
        assert sqlite_digest(archive) == once

    def test_it_is_a_no_op_against_a_create_all_provisioned_80_schema(self, tmp_path):
        """The shape most installs actually have: the viewer built the schema
        from ORM metadata, and the stamping ladder then sends it through here.

        The ladder is the one the container really runs, extracted verbatim from
        scripts/entrypoint.sh rather than reimplemented, because a
        reimplementation of it would test itself.
        """
        from src.db.models import Base

        path = tmp_path / "createall.db"
        engine = sa.create_engine(f"sqlite:///{path}")
        try:
            Base.metadata.create_all(engine)
            inspector = sa.inspect(engine)
            before = {
                table: (
                    sorted(inspector.get_pk_constraint(table).get("constrained_columns") or []),
                    sorted(
                        (fk.get("name") or "?", tuple(fk["constrained_columns"]))
                        for fk in inspector.get_foreign_keys(table)
                    ),
                    sorted(
                        (u.get("name") or "?", tuple(sorted(u["column_names"])))
                        for u in inspector.get_unique_constraints(table)
                    ),
                    sorted(index["name"] for index in inspector.get_indexes(table)),
                )
                for table in inspector.get_table_names()
            }
        finally:
            engine.dispose()

        assert run_shipped_stamping_ladder(path) == "018"
        upgrade_to(f"sqlite+aiosqlite:///{path}")

        engine = sa.create_engine(f"sqlite:///{path}")
        try:
            inspector = sa.inspect(engine)
            after = {
                table: (
                    sorted(inspector.get_pk_constraint(table).get("constrained_columns") or []),
                    sorted(
                        (fk.get("name") or "?", tuple(fk["constrained_columns"]))
                        for fk in inspector.get_foreign_keys(table)
                    ),
                    sorted(
                        (u.get("name") or "?", tuple(sorted(u["column_names"])))
                        for u in inspector.get_unique_constraints(table)
                    ),
                    sorted(index["name"] for index in inspector.get_indexes(table)),
                )
                for table in inspector.get_table_names()
                if table != "alembic_version"
            }
        finally:
            engine.dispose()
        assert after == before


class TestEntrypointLadder(unittest.TestCase):
    """022 adds no rung, in either block, and that is the decision.

    The ladder caps at 018 because migration 019 is data-only and has no
    detectable artifact: a schema built from current ORM metadata must still be
    stamped at 018 so 019 runs. 022's artifacts — account_id, chats.ref, the
    accounts table — are exactly what such a schema already has, so a '022' rung
    could only push the stamp PAST 019. 022 reads the live schema and does
    nothing where the target shape is already in place, which is what makes
    stamping at 018 correct rather than merely tolerable.
    """

    def setUp(self) -> None:
        self.source = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    def test_neither_block_stamps_past_018(self) -> None:
        for version in ("019", "020", "021", "022"):
            self.assertNotIn(f"stamp_version = '{version}'", self.source)
        self.assertEqual(self.source.count("stamp_version = '018'"), 2, "one rung per block")

    def test_both_blocks_explain_why_022_adds_no_rung(self) -> None:
        self.assertEqual(self.source.count("022 adds no rung"), 2)

    def test_a_create_all_80_schema_is_stamped_at_018(self) -> None:
        """Run the SHIPPED ladder, extracted verbatim, over an 8.0 schema."""
        import tempfile

        from src.db.models import Base

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.db"
            engine = sa.create_engine(f"sqlite:///{path}")
            try:
                Base.metadata.create_all(engine)
            finally:
                engine.dispose()
            self.assertEqual(run_shipped_stamping_ladder(path), "018")
