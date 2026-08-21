"""Migration 026 - idx_media_gallery, guarded both directions.

The entrypoint stamping ladder tops out at 018 and relies on every later
migration being idempotent: a create_all() database already has this index
from the model declaration and must no-op here.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

spec = importlib.util.spec_from_file_location(
    "migration_026", _VERSIONS_DIR / "20260821_026_add_media_gallery_index.py"
)
migration_026 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration_026)


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _indexes(conn):
    return {ix["name"] for ix in sa.inspect(conn).get_indexes("media")}


def _create_media_table(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE media (id VARCHAR(255) NOT NULL PRIMARY KEY, chat_id BIGINT, "
            "message_id BIGINT, type VARCHAR(50), downloaded INTEGER DEFAULT 0)"
        )
    )


def test_revision_chain():
    assert migration_026.revision == "026"
    assert migration_026.down_revision == "025"


def test_upgrade_creates_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)

        _run(conn, migration_026.upgrade)
        assert "idx_media_gallery" in _indexes(conn)

        _run(conn, migration_026.upgrade)  # re-run must be a no-op
        assert "idx_media_gallery" in _indexes(conn)


def test_upgrade_noop_when_index_already_exists():
    """Simulates create_all(): the Media model already declares idx_media_gallery."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)
        conn.execute(sa.text("CREATE INDEX idx_media_gallery ON media (chat_id, downloaded, message_id, id)"))

        _run(conn, migration_026.upgrade)  # must not raise
        assert "idx_media_gallery" in _indexes(conn)


def test_upgrade_noop_when_media_table_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_026.upgrade)  # no media table -> no-op, no raise
        assert "media" not in sa.inspect(conn).get_table_names()


def test_downgrade_drops_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_media_table(conn)
        _run(conn, migration_026.upgrade)

        _run(conn, migration_026.downgrade)
        assert "idx_media_gallery" not in _indexes(conn)

        _run(conn, migration_026.downgrade)  # idempotent
        assert "idx_media_gallery" not in _indexes(conn)
