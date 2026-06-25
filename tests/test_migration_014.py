"""Tests for Alembic migration 014 (messages soft-delete columns).

The repo's CLAUDE.md requires every migration to be idempotent and to work on
the SQLite path. These tests drive the migration's own ``upgrade()`` /
``downgrade()`` (which use the global ``op`` proxy) against an in-memory SQLite
database via an Alembic ``Operations`` context.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260625_014_add_message_soft_delete.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_014", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _columns(conn):
    return {c["name"] for c in sa.inspect(conn).get_columns("messages")}


def test_revision_chain():
    """014 follows 013 with no branch label."""
    migration = _load_migration()
    assert migration.revision == "014"
    assert migration.down_revision == "013"


def test_upgrade_adds_columns_and_is_idempotent():
    """upgrade() adds both soft-delete columns and is safe to run twice."""
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE messages (id INTEGER, chat_id INTEGER, text TEXT)"))
        assert "is_deleted" not in _columns(conn)

        _run(conn, migration.upgrade)
        cols = _columns(conn)
        assert "is_deleted" in cols
        assert "deleted_at" in cols

        # Re-run must be a no-op (idempotent), not raise "duplicate column".
        _run(conn, migration.upgrade)
        assert _columns(conn) == cols


def test_downgrade_removes_columns_and_is_idempotent():
    """downgrade() drops both columns and is safe to run twice."""
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE messages (id INTEGER, chat_id INTEGER, text TEXT)"))
        _run(conn, migration.upgrade)
        assert {"is_deleted", "deleted_at"} <= _columns(conn)

        _run(conn, migration.downgrade)
        cols = _columns(conn)
        assert "is_deleted" not in cols
        assert "deleted_at" not in cols

        # Re-run must be a no-op (idempotent).
        _run(conn, migration.downgrade)
        assert _columns(conn) == cols


def test_upgrade_noop_when_columns_already_exist():
    """A create_all()-provisioned DB already has the columns; upgrade() is a no-op."""
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE messages "
                "(id INTEGER, chat_id INTEGER, text TEXT, "
                "is_deleted INTEGER NOT NULL DEFAULT 0, deleted_at DATETIME)"
            )
        )
        _run(conn, migration.upgrade)
        assert {"is_deleted", "deleted_at"} <= _columns(conn)
