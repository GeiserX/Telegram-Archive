"""Tests for Alembic migration 018 (reactions.removed_at + chat-first index)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260718_018_add_reaction_removed_at.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_018", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_reactions_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE reactions ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "message_id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "emoji VARCHAR(50) NOT NULL, "
            "user_id BIGINT, "
            "count INTEGER DEFAULT 1, "
            "created_at DATETIME"
            ")"
        )
    )


def _columns(conn):
    return {c["name"] for c in sa.inspect(conn).get_columns("reactions")}


def _indexes(conn):
    return {ix["name"] for ix in sa.inspect(conn).get_indexes("reactions")}


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "018"
    assert migration.down_revision == "017"


def test_upgrade_adds_column_and_index_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_reactions_table(conn)

        _run(conn, migration.upgrade)
        assert "removed_at" in _columns(conn)
        assert "idx_reactions_chat_message" in _indexes(conn)

        # existing rows have removed_at NULL (active)
        conn.execute(sa.text("INSERT INTO reactions (id, message_id, chat_id, emoji) VALUES (1, 1, -100, '👍')"))
        val = conn.execute(sa.text("SELECT removed_at FROM reactions WHERE id=1")).scalar()
        assert val is None

        # re-run must be a no-op (both artifacts already present)
        _run(conn, migration.upgrade)
        assert "removed_at" in _columns(conn)
        assert "idx_reactions_chat_message" in _indexes(conn)


def test_upgrade_noop_when_artifacts_already_exist():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_reactions_table(conn)
        conn.execute(sa.text("ALTER TABLE reactions ADD COLUMN removed_at DATETIME"))
        conn.execute(sa.text("CREATE INDEX idx_reactions_chat_message ON reactions (chat_id, message_id)"))

        _run(conn, migration.upgrade)  # must not raise
        assert "removed_at" in _columns(conn)
        assert "idx_reactions_chat_message" in _indexes(conn)


def test_downgrade_drops_both_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_reactions_table(conn)
        _run(conn, migration.upgrade)

        _run(conn, migration.downgrade)
        assert "removed_at" not in _columns(conn)
        assert "idx_reactions_chat_message" not in _indexes(conn)

        _run(conn, migration.downgrade)  # idempotent
        assert "removed_at" not in _columns(conn)


def test_upgrade_noop_when_reactions_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)  # no reactions table → no-op, no raise
        assert "reactions" not in sa.inspect(conn).get_table_names()
