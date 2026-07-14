"""Tests for Alembic migration 017 (idx_messages_chat_id_id index)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260715_017_add_messages_chat_id_id_index.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_017", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_messages_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE messages ("
            "id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "date DATETIME NOT NULL, "
            "PRIMARY KEY (id, chat_id)"
            ")"
        )
    )


def _indexes(conn):
    return {ix["name"] for ix in sa.inspect(conn).get_indexes("messages")}


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "017"
    assert migration.down_revision == "016"


def test_upgrade_creates_index_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)

        _run(conn, migration.upgrade)
        assert "idx_messages_chat_id_id" in _indexes(conn)

        # re-run must be a no-op (index already present)
        _run(conn, migration.upgrade)
        assert "idx_messages_chat_id_id" in _indexes(conn)


def test_upgrade_noop_when_index_already_exists():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_id_id ON messages (chat_id, id)"))

        _run(conn, migration.upgrade)  # must not raise
        assert "idx_messages_chat_id_id" in _indexes(conn)


def test_downgrade_drops_index_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        _run(conn, migration.upgrade)

        _run(conn, migration.downgrade)
        assert "idx_messages_chat_id_id" not in _indexes(conn)

        _run(conn, migration.downgrade)  # idempotent
        assert "idx_messages_chat_id_id" not in _indexes(conn)


def test_upgrade_noop_when_messages_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)  # no messages table → no-op, no raise
        assert "messages" not in sa.inspect(conn).get_table_names()
