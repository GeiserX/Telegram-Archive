"""Tests for Alembic migration 015 (message versions table)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260626_015_add_message_versions.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_015", _MIGRATION_PATH)
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
            "text TEXT, "
            "edit_date DATETIME, "
            "PRIMARY KEY (id, chat_id)"
            ")"
        )
    )


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "015"
    assert migration.down_revision == "014"


def test_upgrade_creates_table_indexes_and_constraints_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)

        _run(conn, migration.upgrade)
        inspector = sa.inspect(conn)
        assert "message_versions" in inspector.get_table_names()

        columns = {c["name"] for c in inspector.get_columns("message_versions")}
        assert {
            "id",
            "message_id",
            "chat_id",
            "text",
            "date",
            "change_hash",
            "captured_at",
        } <= columns

        indexes = {idx["name"] for idx in inspector.get_indexes("message_versions")}
        assert "idx_message_versions_message_captured" in indexes
        assert "idx_message_versions_message_date" in indexes

        uniques = {uc["name"] for uc in inspector.get_unique_constraints("message_versions")}
        assert "uq_message_versions_change_hash" in uniques

        # Re-run must be a no-op.
        _run(conn, migration.upgrade)
        assert "message_versions" in sa.inspect(conn).get_table_names()


def test_downgrade_drops_table_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        _run(conn, migration.upgrade)

        _run(conn, migration.downgrade)
        assert "message_versions" not in sa.inspect(conn).get_table_names()

        _run(conn, migration.downgrade)
        assert "message_versions" not in sa.inspect(conn).get_table_names()


def test_upgrade_noop_when_table_already_exists():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)
        conn.execute(
            sa.text(
                "CREATE TABLE message_versions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "message_id BIGINT NOT NULL, "
                "chat_id BIGINT NOT NULL, "
                "text TEXT, "
                "date DATETIME NOT NULL, "
                "change_hash VARCHAR(64) NOT NULL UNIQUE, "
                "captured_at DATETIME NOT NULL"
                ")"
            )
        )

        _run(conn, migration.upgrade)
        inspector = sa.inspect(conn)
        assert "message_versions" in inspector.get_table_names()

        # The partial state (table pre-created WITHOUT the named indexes, e.g. a
        # half-applied migration) is the riskiest real-world case: upgrade() must
        # add the missing indexes instead of just tolerating the table.
        indexes = {idx["name"] for idx in inspector.get_indexes("message_versions")}
        assert "idx_message_versions_message_date" in indexes
        assert "idx_message_versions_message_captured" in indexes
