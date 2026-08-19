"""Tests for Alembic migration 025 (drop the redundant idx_messages_chat_id).

``idx_messages_chat_id`` (chat_id) is a strict prefix of both
``idx_messages_chat_id_id`` (chat_id, id) and ``idx_messages_chat_date_desc``
(chat_id, date DESC) - every access path that filters chat_id alone also
orders or bounds by id/date/is_pinned, so nothing plans against the narrower
index. These tests build a messages table with the redundant index present
and assert the migration drops it, leaves the other indexes alone, and that
downgrade puts it back (it is a real declared index up through 024; only 025
itself removes it from models.py).
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

from src.db.models import Message

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260819_025_drop_redundant_chat_id_index.py"
)

DROPPED = "idx_messages_chat_id"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_025", _MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load migration 025")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn: Connection, func: Callable[[], None]) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        func()


def _create_messages_table(conn: Connection, *, with_chat_id_index: bool) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE messages (account_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
            "id BIGINT NOT NULL, date DATETIME NOT NULL, is_pinned INTEGER)"
        )
    )
    # Unrelated / superset indexes, present in both shapes: the migration must leave them alone.
    conn.execute(sa.text("CREATE INDEX idx_messages_date ON messages (date)"))
    conn.execute(sa.text("CREATE INDEX idx_messages_chat_id_id ON messages (chat_id, id)"))
    conn.execute(sa.text("CREATE INDEX idx_messages_chat_date_desc ON messages (chat_id, date DESC)"))
    if with_chat_id_index:
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_id ON messages (chat_id)"))


def _indexes(conn: Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(conn).get_indexes("messages")}


class TestMigration025(unittest.TestCase):
    def test_revision_chain(self) -> None:
        migration = _load_migration()
        self.assertEqual(migration.revision, "025")
        self.assertEqual(migration.down_revision, "024")

    def test_model_no_longer_declares_the_index(self) -> None:
        """The other half of this change: models.py must agree with 025."""
        declared = {ix.name for ix in Message.__table__.indexes}
        self.assertNotIn(DROPPED, declared)

    def test_drops_the_redundant_index(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_chat_id_index=True)
            self.assertIn(DROPPED, _indexes(conn))

            _run(conn, migration.upgrade)

            self.assertNotIn(DROPPED, _indexes(conn))
            self.assertIn("idx_messages_date", _indexes(conn), "unrelated index must survive")
            self.assertIn("idx_messages_chat_id_id", _indexes(conn), "superset index must survive")
            self.assertIn("idx_messages_chat_date_desc", _indexes(conn), "superset index must survive")

    def test_a_database_already_missing_it_is_untouched(self) -> None:
        """Idempotent: some databases never had it drift-restored by 024."""
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_chat_id_index=False)
            self.assertNotIn(DROPPED, _indexes(conn))

            _run(conn, migration.upgrade)
            _run(conn, migration.upgrade)

            self.assertNotIn(DROPPED, _indexes(conn))

    def test_downgrade_recreates_it(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn, with_chat_id_index=True)
            _run(conn, migration.upgrade)
            self.assertNotIn(DROPPED, _indexes(conn))

            _run(conn, migration.downgrade)

            self.assertIn(DROPPED, _indexes(conn))
            recreated = next(ix for ix in sa.inspect(conn).get_indexes("messages") if ix["name"] == DROPPED)
            self.assertEqual(recreated["column_names"], ["chat_id"])

    def test_no_messages_table_is_survivable(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration.upgrade)
            _run(conn, migration.downgrade)


if __name__ == "__main__":
    unittest.main()
