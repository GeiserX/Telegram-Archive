"""Migration 043: ``account_deleted_at`` on chats and users, added by inspector-guarded steps.

The upgrade from 042 keeps every row and reads them as never seen deleted. A
re-run, and a create_all() database that already has the columns, change
nothing; a second downgrade changes nothing either. Runs on SQLite and
PostgreSQL. The revision ids are read from the migration module, so
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

spec = importlib.util.spec_from_file_location("migration_043", _VERSIONS / "20261123_043_add_account_deleted_at.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

TABLES = ("chats", "users")


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def _has_column(conn) -> dict[str, bool]:
    inspector = sa.inspect(conn)
    return {table: "account_deleted_at" in {c["name"] for c in inspector.get_columns(table)} for table in TABLES}


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


def test_a_create_all_schema_already_has_the_columns_and_upgrade_is_a_no_op():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        assert _has_column(conn) == {"chats": True, "users": True}
        _run(conn, migration.upgrade)
        _run(conn, migration.upgrade)
        assert _has_column(conn) == {"chats": True, "users": True}


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_043")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_from_042_keeps_rows_and_both_directions_are_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert _has_column(conn) == {"chats": False, "users": False}
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, first_name, last_synced_message_id) "
                    "VALUES (1, 201, 'ref0043', 'private', 'Sample', 0)"
                )
            )
            conn.execute(sa.text("INSERT INTO users (id, first_name, is_bot) VALUES (201, 'Sample', 0)"))

        _build_alembic_schema(async_url, migration.revision)

        with engine.begin() as conn:
            assert _has_column(conn) == {"chats": True, "users": True}
            # Rows from before the column read as never seen deleted.
            assert conn.execute(sa.text("SELECT first_name, account_deleted_at FROM chats")).all() == [("Sample", None)]
            assert conn.execute(sa.text("SELECT first_name, account_deleted_at FROM users")).all() == [("Sample", None)]
            _run(conn, migration.upgrade)  # a re-run changes nothing
            assert _has_column(conn) == {"chats": True, "users": True}

        with engine.begin() as conn:
            _run(conn, migration.downgrade)
            assert _has_column(conn) == {"chats": False, "users": False}
            _run(conn, migration.downgrade)
            assert conn.execute(sa.text("SELECT first_name FROM chats")).all() == [("Sample",)]
            assert conn.execute(sa.text("SELECT first_name FROM users")).all() == [("Sample",)]
    finally:
        engine.dispose()
