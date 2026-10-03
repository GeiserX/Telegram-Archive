"""Migration 038: the ``message_snapshots`` table.

The upgrade runs from the revision before to head on SQLite and on PostgreSQL
with a poll message from before: the message and its ``raw_data`` stay as they
were, and no snapshot row is invented for it. A re-run, and a create_all()
database that already has the table, change nothing; the downgrade removes the
table and keeps the message.

The revision ids are read from the migration module, so renumbering the chain
at merge time changes only the migration file.
"""

import importlib.util
import json
import re
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
POLL = {"id": 5550001, "question": "Demo question?", "answers": [], "closed": False, "results": None}


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("migration_message_snapshots", "20261001_038_add_message_snapshots.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def test_revision_chain_points_at_an_existing_revision_and_does_not_fork():
    revisions = {}
    for path in _VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([^"]+)"', source, re.M)
        down = re.search(r'^down_revision: str \| None = "([^"]+)"', source, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    assert migration.down_revision in revisions
    # 038 was the head when it shipped; a later migration may follow it, two may not.
    assert list(revisions.values()).count(migration.revision) <= 1


def test_upgrade_without_the_tables_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        _run(conn, migration.downgrade)
        assert _tables(conn) == set()


def test_upgrade_adds_the_index_to_a_table_without_it():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE messages (account_id INTEGER, id INTEGER, chat_id INTEGER)"))
        conn.execute(
            sa.text(
                "CREATE TABLE message_snapshots (id INTEGER PRIMARY KEY, account_id INTEGER, chat_id INTEGER, "
                "message_id INTEGER, kind TEXT, payload TEXT, observed_at DATETIME, source TEXT)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO message_snapshots VALUES (1, 1, -1001, 5, 'poll', '{}', :d, 'listener')"),
            {"d": str(SENT)},
        )
        _run(conn, migration.upgrade)
        assert migration.INDEX_NAME in {i["name"] for i in sa.inspect(conn).get_indexes("message_snapshots")}
        assert conn.execute(sa.text("SELECT id, kind FROM message_snapshots")).all() == [(1, "poll")]


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_038")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def test_upgrade_keeps_a_poll_message_and_is_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)
    raw_data = json.dumps({"poll": POLL})

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert "message_snapshots" not in _tables(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0038', 'group', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO messages (account_id, id, chat_id, date, text, raw_data, is_outgoing, is_pinned, "
                    "is_deleted) VALUES (1, 5, -1001, :sent, '', :raw, 0, 0, 0)"
                ),
                {"sent": str(SENT), "raw": raw_data},
            )

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            assert "message_snapshots" in _tables(conn)
            assert conn.execute(sa.text("SELECT count(*) FROM message_snapshots")).scalar() == 0
            assert conn.execute(sa.text("SELECT raw_data FROM messages")).scalar() == raw_data
            # A re-run, or a create_all() database that already has the table, changes nothing.
            _run(conn, migration.upgrade)
            index_names = {i["name"] for i in sa.inspect(conn).get_indexes("message_snapshots")}
            assert migration.INDEX_NAME in index_names
            columns = {c["name"] for c in sa.inspect(conn).get_columns("message_snapshots")}
            assert columns == {"id", "account_id", "chat_id", "message_id", "kind", "payload", "observed_at", "source"}

        with engine.begin() as conn:
            _run(conn, migration.downgrade)
            assert "message_snapshots" not in _tables(conn)
            _run(conn, migration.downgrade)
            assert conn.execute(sa.text("SELECT raw_data FROM messages")).scalar() == raw_data
    finally:
        engine.dispose()
