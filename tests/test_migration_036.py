"""Migration 036: ``media.telegram_file_id`` and the ``media_versions`` table.

The upgrade runs from 035 to head on SQLite and on PostgreSQL with a media row
from before: it keeps every value and reads back with no file id (unknown,
never a guess). A re-run, and a create_all() database that already has both,
change nothing; the downgrade removes both and keeps the media row.
"""

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
SENT = datetime(2026, 1, 1, 9, 0, 0)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_036 = _load("migration_036", "20260930_036_add_media_versions.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _media_columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("media")}


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def test_revision_chain():
    assert (migration_036.revision, migration_036.down_revision) == ("036", "035")


def test_upgrade_without_the_tables_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_036.upgrade)
        _run(conn, migration_036.downgrade)
        assert _tables(conn) == set()


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_036")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_from_035_keeps_media_and_is_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, "035")

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert "telegram_file_id" not in _media_columns(conn)
            assert "media_versions" not in _tables(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0036', 'group', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned, is_deleted) "
                    "VALUES (1, 5, -1001, :sent, 'Look at this', 0, 0, 0)"
                ),
                {"sent": str(SENT)},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO media (account_id, id, message_id, chat_id, type, file_name, downloaded) "
                    "VALUES (1, '-1001_5_photo', 5, -1001, 'photo', '111.jpg', 1)"
                )
            )

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            assert "telegram_file_id" in _media_columns(conn)
            assert "media_versions" in _tables(conn)
            rows = conn.execute(sa.text("SELECT id, file_name, telegram_file_id FROM media")).all()
            assert [tuple(row) for row in rows] == [("-1001_5_photo", "111.jpg", None)]
            # A re-run, or a create_all() database that already has both, changes nothing.
            _run(conn, migration_036.upgrade)
            assert "telegram_file_id" in _media_columns(conn)
            index_names = {i["name"] for i in sa.inspect(conn).get_indexes("media_versions")}
            assert "ix_media_versions_message" in index_names

        with engine.begin() as conn:
            _run(conn, migration_036.downgrade)
            assert "telegram_file_id" not in _media_columns(conn)
            assert "media_versions" not in _tables(conn)
            _run(conn, migration_036.downgrade)
            assert conn.execute(sa.text("SELECT id FROM media")).all() == [("-1001_5_photo",)]
    finally:
        engine.dispose()
