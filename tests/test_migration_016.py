"""Tests for Alembic migration 016 (media.download_attempts column)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260714_016_add_media_download_attempts.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_016", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_media_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE media ("
            "id VARCHAR(255) NOT NULL PRIMARY KEY, "
            "message_id BIGINT, "
            "chat_id BIGINT, "
            "downloaded INTEGER DEFAULT 0"
            ")"
        )
    )


def _columns(conn):
    return {c["name"] for c in sa.inspect(conn).get_columns("media")}


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "016"
    assert migration.down_revision == "015"


def test_upgrade_adds_column_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)

        _run(conn, migration.upgrade)
        assert "download_attempts" in _columns(conn)

        # existing rows get the server_default 0
        conn.execute(sa.text("INSERT INTO media (id) VALUES ('m1')"))
        val = conn.execute(sa.text("SELECT download_attempts FROM media WHERE id='m1'")).scalar()
        assert val == 0

        # re-run must be a no-op (column already present)
        _run(conn, migration.upgrade)
        assert "download_attempts" in _columns(conn)


def test_upgrade_noop_when_column_already_exists():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)
        conn.execute(sa.text("ALTER TABLE media ADD COLUMN download_attempts INTEGER NOT NULL DEFAULT 0"))

        _run(conn, migration.upgrade)  # must not raise
        assert "download_attempts" in _columns(conn)


def test_downgrade_drops_column_and_is_idempotent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)
        _run(conn, migration.upgrade)

        _run(conn, migration.downgrade)
        assert "download_attempts" not in _columns(conn)

        _run(conn, migration.downgrade)  # idempotent
        assert "download_attempts" not in _columns(conn)


def test_upgrade_noop_when_media_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)  # no media table → no-op, no raise
        assert "media" not in sa.inspect(conn).get_table_names()
