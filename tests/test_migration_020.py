"""Tests for Alembic migration 020 (messages.sender_name)."""

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
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260727_020_add_message_sender_name.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_020", _MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load migration 020")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn: Connection, func: Callable[[], None]) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        func()


def _create_messages_table(conn: Connection) -> None:
    conn.execute(sa.text("CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL)"))


def _columns(conn: Connection) -> set[str]:
    return {column["name"] for column in sa.inspect(conn).get_columns("messages")}


class TestMigration020(unittest.TestCase):
    def test_revision_chain(self) -> None:
        migration = _load_migration()
        self.assertEqual(migration.revision, "020")
        self.assertEqual(migration.down_revision, "019")

    def test_upgrade_and_downgrade_are_idempotent(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _create_messages_table(conn)

            _run(conn, migration.upgrade)
            _run(conn, migration.upgrade)
            self.assertIn("sender_name", _columns(conn))

            _run(conn, migration.downgrade)
            _run(conn, migration.downgrade)
            self.assertNotIn("sender_name", _columns(conn))

    def test_migration_noops_when_messages_table_absent(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration.upgrade)
            _run(conn, migration.downgrade)
            self.assertNotIn("messages", sa.inspect(conn).get_table_names())
