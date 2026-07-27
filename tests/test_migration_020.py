"""Tests for Alembic migration 020 (messages.sender_name)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260727_020_add_message_sender_name.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_020", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        func()


def _create_messages_table(conn):
    conn.execute(sa.text("CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL)"))


def _columns(conn):
    return {column["name"] for column in sa.inspect(conn).get_columns("messages")}


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "020"
    assert migration.down_revision == "019"


def test_upgrade_and_downgrade_are_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)

        _run(conn, migration.upgrade)
        _run(conn, migration.upgrade)
        assert "sender_name" in _columns(conn)

        _run(conn, migration.downgrade)
        _run(conn, migration.downgrade)
        assert "sender_name" not in _columns(conn)


def test_migration_noops_when_messages_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
        assert "messages" not in sa.inspect(conn).get_table_names()
