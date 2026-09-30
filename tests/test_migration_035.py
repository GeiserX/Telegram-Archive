"""Migration 035: ``message_versions.entities`` and ``message_versions.source``.

Both columns are nullable and guarded by the inspector. The upgrade path runs
from 033, the release before the edits work, through 034 to head on SQLite and
on PostgreSQL, with a version row from before: it keeps its text and reads back
with no formatting and no source, never a guess.
"""

import asyncio
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import NO_POSTGRES_REASON
from test_schema_parity import _build_alembic_schema

from telegram_archive.db.adapter import DatabaseAdapter
from telegram_archive.db.base import DatabaseManager

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"
_COLUMNS = {"entities", "source"}
SENT = datetime(2026, 1, 1, 9, 0, 0)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_035 = _load("migration_035", "20260930_035_add_message_version_entities_source.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("message_versions")}


def test_revision_chain():
    assert (migration_035.revision, migration_035.down_revision) == ("035", "034")


def test_upgrade_without_the_table_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_035.upgrade)
        _run(conn, migration_035.downgrade)
        assert "message_versions" not in sa.inspect(conn).get_table_names()


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_035")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


async def _read_versions(async_url: str) -> list[dict]:
    manager = DatabaseManager(async_url)
    await manager.init()
    try:
        return await DatabaseAdapter(manager).get_message_versions(-1001, 5, account_id=1)
    finally:
        await manager.close()


def test_upgrade_from_033_keeps_old_versions_and_is_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, "033")

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert not _COLUMNS & _columns(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) VALUES (1, -1001, 'ref0035', 'group', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO messages (account_id, id, chat_id, date, text, edit_date, is_outgoing, is_pinned, "
                    "is_deleted) VALUES (1, 5, -1001, :sent, 'Meet at ten', :edited, 0, 0, 0)"
                ),
                {"sent": str(SENT), "edited": str(datetime(2026, 1, 1, 9, 5, 0))},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO message_versions (account_id, message_id, chat_id, text, date, change_hash, "
                    "captured_at) VALUES (1, 5, -1001, 'Meet at nine', :sent, :hash, :sent)"
                ),
                {"sent": str(SENT), "hash": "0" * 64},
            )

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            assert _columns(conn) >= _COLUMNS
            rows = conn.execute(sa.text("SELECT text, entities, source FROM message_versions")).all()
            assert [tuple(row) for row in rows] == [("Meet at nine", None, None)]
            # A re-run, or a create_all() database that already has the columns, changes nothing.
            _run(conn, migration_035.upgrade)
            assert _columns(conn) >= _COLUMNS
    finally:
        engine.dispose()

    versions = asyncio.run(_read_versions(async_url))
    assert [(v["text"], v["entities"], v["source"], v["captured_at"]) for v in versions] == [
        ("Meet at nine", None, None, SENT)
    ]

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            _run(conn, migration_035.downgrade)
            assert not _COLUMNS & _columns(conn)
            _run(conn, migration_035.downgrade)
            assert conn.execute(sa.text("SELECT text FROM message_versions")).all() == [("Meet at nine",)]
    finally:
        engine.dispose()
