"""Migration 042: the six timestamp indexes ``export --changed-since`` selects on.

Guarded both directions: a create_all() database already has the indexes from
the model declarations and must no-op, and a re-run or a second downgrade
changes nothing. The upgrade from 041 to head runs on SQLite and PostgreSQL.
The revision ids are read from the migration module, so renumbering the chain
at merge time changes only the migration file.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import NO_POSTGRES_REASON
from test_schema_parity import _build_alembic_schema

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"

spec = importlib.util.spec_from_file_location("migration_042", _VERSIONS / "20261028_042_add_changed_since_indexes.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

EXPECTED = {
    ("messages", "idx_messages_created_at"),
    ("messages", "idx_messages_deleted_at"),
    ("message_versions", "idx_message_versions_captured_at"),
    ("media_versions", "ix_media_versions_captured_at"),
    ("message_snapshots", "ix_message_snapshots_observed_at"),
    ("reaction_history", "ix_reaction_history_observed_at"),
}


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _ours(conn) -> set[tuple[str, str]]:
    inspector = sa.inspect(conn)
    found = set()
    for table in {table for table, _ in EXPECTED} & set(inspector.get_table_names()):
        found |= {(table, index["name"]) for index in inspector.get_indexes(table)}
    return found & EXPECTED


def test_revision_chain_points_at_an_existing_revision_and_does_not_fork():
    revisions = {}
    for path in _VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([^"]+)"', source, re.M)
        down = re.search(r'^down_revision: str \| None = "([^"]+)"', source, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    assert migration.down_revision in revisions
    assert list(revisions.values()).count(migration.down_revision) == 1
    assert list(revisions.values()).count(migration.revision) <= 1


def test_it_no_ops_when_the_tables_are_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
        assert sa.inspect(conn).get_table_names() == []


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_042")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_adds_the_indexes_and_both_directions_are_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            assert _ours(conn) == set()

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            assert _ours(conn) == EXPECTED
            # A re-run, or a create_all() database that already has them, adds nothing.
            _run(conn, migration.upgrade)
            assert _ours(conn) == EXPECTED
        with engine.begin() as conn:
            _run(conn, migration.downgrade)
            assert _ours(conn) == set()
            _run(conn, migration.downgrade)
            assert _ours(conn) == set()
    finally:
        engine.dispose()
