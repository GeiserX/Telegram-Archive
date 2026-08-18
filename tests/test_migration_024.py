"""Tests for Alembic migration 024 (recreate declared indexes a database lacks).

The fault this migration repairs was real: a production archive was missing
``idx_messages_chat_date_desc`` and ``idx_messages_chat_pinned`` while
declaring both in models.py, and listing chats consequently read the whole
archive. These tests build that exact shape — a messages table with the two
indexes absent — and assert the migration puts them back, leaves a healthy
database untouched, and keeps the DESC ordering the query depends on.
"""

import importlib.util
import unittest
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260818_024_restore_missing_declared_indexes.py"
)

RESTORED = {"idx_messages_chat_date_desc", "idx_messages_chat_pinned"}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_024", _MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load migration 024")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn: Connection, func: Callable[[], None]) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        func()


def _create_messages_table(conn: Connection, *, with_indexes: bool) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE messages (account_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
            "id BIGINT NOT NULL, date DATETIME NOT NULL, is_pinned INTEGER)"
        )
    )
    # An unrelated index, present in both shapes: the migration must leave it alone.
    conn.execute(sa.text("CREATE INDEX idx_messages_date ON messages (date)"))
    if with_indexes:
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_date_desc ON messages (chat_id, date DESC)"))
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_pinned ON messages (chat_id, is_pinned)"))


def _indexes(conn: Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(conn).get_indexes("messages")}


def _index_ddl(conn: Connection) -> dict[str, str]:
    """Every index's CREATE statement verbatim — identity, not just presence."""
    rows = conn.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'messages' AND sql IS NOT NULL"
        )
    ).fetchall()
    return {row[0]: row[1] for row in rows}


class TestMigration024(unittest.TestCase):
    def test_revision_chain(self) -> None:
        migration = _load_migration()
        self.assertEqual(migration.revision, "024")
        self.assertEqual(migration.down_revision, "023")

    def test_recreates_the_indexes_a_database_is_missing(self) -> None:
        """The production shape: declared indexes absent, everything else intact."""
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_indexes=False)
            self.assertEqual(_indexes(conn) & RESTORED, set(), "fixture should start without them")

            _run(conn, migration.upgrade)

            self.assertEqual(_indexes(conn) & RESTORED, RESTORED)
            self.assertIn("idx_messages_date", _indexes(conn), "unrelated index must survive")

    def test_the_restored_date_index_keeps_its_desc_ordering(self) -> None:
        """DESC is the whole point: the query reads ORDER BY date DESC."""
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_indexes=False)
            _run(conn, migration.upgrade)

            ddl = conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_messages_chat_date_desc'")
            ).scalar()
            columns_clause = ddl[ddl.index("(") :].upper()
            self.assertIn("DESC", columns_clause)

    def test_a_healthy_database_is_untouched_and_rerunning_is_a_no_op(self) -> None:
        """Untouched means the definitions too, not just the names.

        Comparing names alone would pass a migration that dropped each index
        and built it again — which on a real archive is minutes of rewriting
        for no change, and on SQLite silently loses the DESC that reflection
        cannot see.
        """
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_indexes=True)
            before = _index_ddl(conn)

            _run(conn, migration.upgrade)
            _run(conn, migration.upgrade)

            self.assertEqual(_index_ddl(conn), before)

    def test_downgrade_keeps_the_indexes(self) -> None:
        """Dropping them on the way down would reintroduce the fault."""
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_indexes=False)
            _run(conn, migration.upgrade)

            _run(conn, migration.downgrade)

            self.assertEqual(_indexes(conn) & RESTORED, RESTORED)

    def test_no_messages_table_is_survivable(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration.upgrade)


if __name__ == "__main__":
    unittest.main()
