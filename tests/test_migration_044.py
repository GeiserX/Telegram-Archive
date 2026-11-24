"""Migration 044: the rotating full pass state on sync_status, added by inspector-guarded steps.

The upgrade from 043 keeps every row and reads each chat as never having
finished a pass. A re-run, and a create_all() database that already has the
columns, change nothing; a second downgrade changes nothing either. Runs on
SQLite and PostgreSQL. The revision ids are read from the migration module, so
renumbering the chain at merge time changes only the migration file.
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

from telegram_archive.db.models import Base

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"

spec = importlib.util.spec_from_file_location("migration_044", _VERSIONS / "20261124_044_add_full_sync_rotation.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

COLUMNS = ("full_sync_cursor", "full_sync_completed_at")


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def _has_columns(conn) -> dict[str, bool]:
    names = {c["name"] for c in sa.inspect(conn).get_columns("sync_status")}
    return {column: column in names for column in COLUMNS}


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


def test_it_no_ops_when_the_table_is_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
        assert sa.inspect(conn).get_table_names() == []


def test_a_create_all_schema_already_has_the_columns_and_upgrade_is_a_no_op():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        assert _has_columns(conn) == dict.fromkeys(COLUMNS, True)
        _run(conn, migration.upgrade)
        _run(conn, migration.upgrade)
        assert _has_columns(conn) == dict.fromkeys(COLUMNS, True)


def test_a_half_applied_upgrade_adds_only_the_missing_column():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE sync_status (account_id INTEGER, chat_id BIGINT, full_sync_cursor BIGINT)"))
        _run(conn, migration.upgrade)
        assert _has_columns(conn) == dict.fromkeys(COLUMNS, True)


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_044")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_from_043_keeps_rows_and_both_directions_are_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _has_columns(conn) == dict.fromkeys(COLUMNS, False)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, first_name, last_synced_message_id) "
                    "VALUES (1, 201, 'ref0044', 'private', 'Sample', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO sync_status (account_id, chat_id, last_message_id, message_count) "
                    "VALUES (1, 201, 77, 5)"
                )
            )

        _build_alembic_schema(async_url, migration.revision)

        with engine.begin() as conn:
            assert _has_columns(conn) == dict.fromkeys(COLUMNS, True)
            # A row from before the columns reads as never having finished a pass.
            assert conn.execute(
                sa.text(
                    "SELECT last_message_id, message_count, full_sync_cursor, full_sync_completed_at FROM sync_status"
                )
            ).all() == [(77, 5, None, None)]
            _run(conn, migration.upgrade)  # a re-run changes nothing
            assert _has_columns(conn) == dict.fromkeys(COLUMNS, True)

        with engine.begin() as conn:
            _run(conn, migration.downgrade)
            assert _has_columns(conn) == dict.fromkeys(COLUMNS, False)
            _run(conn, migration.downgrade)
            assert conn.execute(sa.text("SELECT last_message_id, message_count FROM sync_status")).all() == [(77, 5)]
    finally:
        engine.dispose()
