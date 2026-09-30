"""Migration 034: ``messages.edit_hide``, added by an inspector-guarded step."""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import NO_POSTGRES_REASON
from test_schema_parity import _build_alembic_schema

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_034 = _load("migration_034", "20260930_034_add_message_edit_hide.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("messages")}


def test_revision_chain():
    assert (migration_034.revision, migration_034.down_revision) == ("034", "033")


def test_upgrade_adds_the_column_keeps_rows_and_is_idempotent_and_downgrade_drops_it():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE messages (id INTEGER, chat_id INTEGER, text TEXT, edit_date DATETIME)"))
        conn.execute(sa.text("INSERT INTO messages VALUES (1, 7, 'kept', '2026-01-01 09:05:00')"))
        _run(conn, migration_034.upgrade)
        assert "edit_hide" in _columns(conn)
        # Rows from before the column read as unknown, never as hidden.
        assert conn.execute(sa.text("SELECT text, edit_hide FROM messages")).all() == [("kept", None)]
        _run(conn, migration_034.upgrade)  # a re-run, or a create_all() database, changes nothing
        assert "edit_hide" in _columns(conn)
        _run(conn, migration_034.downgrade)
        assert "edit_hide" not in _columns(conn)
        _run(conn, migration_034.downgrade)
        assert conn.execute(sa.text("SELECT text FROM messages")).all() == [("kept",)]


def test_upgrade_without_the_table_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_034.upgrade)
        assert "messages" not in sa.inspect(conn).get_table_names()


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_034")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_from_033_keeps_rows_and_downgrade_drops_the_column(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, "033")

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert "edit_hide" not in _columns(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0034', 'group', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO messages "
                    "(account_id, id, chat_id, date, edit_date, text, is_outgoing, is_pinned, is_deleted) "
                    "VALUES (1, 5, -1001, :sent, :edited, 'kept', 0, 0, 0)"
                ),
                {"sent": str(datetime(2026, 1, 1, 9)), "edited": str(datetime(2026, 1, 1, 9, 5))},
            )

        _build_alembic_schema(async_url, "034")

        with engine.begin() as conn:
            assert "edit_hide" in _columns(conn)
            assert [tuple(row) for row in conn.execute(sa.text("SELECT text, edit_hide FROM messages"))] == [
                ("kept", None)
            ]
            _run(conn, migration_034.upgrade)  # a re-run changes nothing
            assert "edit_hide" in _columns(conn)

        with engine.begin() as conn:
            _run(conn, migration_034.downgrade)
            assert "edit_hide" not in _columns(conn)
            _run(conn, migration_034.downgrade)
            assert [tuple(row) for row in conn.execute(sa.text("SELECT text FROM messages"))] == [("kept",)]
    finally:
        engine.dispose()
