"""Migration 037: the ``reaction_history`` table and its baseline.

The upgrade runs from 036 to head on SQLite and on PostgreSQL with reactions
from before: it keeps every ``reactions`` row as it was and seeds one baseline
history row per emoji, plus a count 0 row for an emoji taken back. A re-run,
and a create_all() database that already has the table, add nothing; the
downgrade removes the table and keeps the reactions.
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
FIRST_SEEN = datetime(2026, 1, 1, 9, 5, 0)
GONE = datetime(2026, 1, 1, 10, 0, 0)
GONE_LATER = datetime(2026, 1, 1, 11, 0, 0)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_037 = _load("migration_037", "20261001_037_add_reaction_history.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def test_revision_chain():
    assert (migration_037.revision, migration_037.down_revision) == ("037", "036")


def test_upgrade_without_the_tables_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_037.upgrade)
        _run(conn, migration_037.downgrade)
        assert _tables(conn) == set()


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_037")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


# (message, emoji, count, user_id, created_at, removed_at)
REACTIONS = [
    (5, "👍", 3, None, FIRST_SEEN, None),  # live
    (5, "😮", 2, None, FIRST_SEEN, GONE),  # taken back
    (6, "🔥", 1, 11, FIRST_SEEN, None),  # legacy per-user rows, one live
    (6, "🔥", 1, 12, SENT, GONE),  # and one tombstoned: the live sum counts
    (6, "🎉", 0, 11, FIRST_SEEN, GONE),  # legacy rows, every one tombstoned,
    (6, "🎉", 2, 12, FIRST_SEEN, GONE_LATER),  # one with count 0, read as one
    (6, "custom_5000000001", 0, None, FIRST_SEEN, GONE),  # a tombstone with count 0
]

SEEDED = [
    (5, "👍", 3, None, FIRST_SEEN),
    (5, "😮", 2, None, FIRST_SEEN),
    (5, "😮", 0, 2, GONE),
    (6, "custom_5000000001", 1, None, FIRST_SEEN),
    (6, "custom_5000000001", 0, 1, GONE),
    (6, "🎉", 3, None, FIRST_SEEN),
    (6, "🎉", 0, 3, GONE_LATER),
    (6, "🔥", 1, None, SENT),
]


def _history(conn) -> list[tuple]:
    rows = conn.execute(
        sa.text(
            "SELECT message_id, emoji, count, previous_count, observed_at, source FROM reaction_history "
            "ORDER BY message_id, emoji, observed_at, id"
        )
    ).all()
    assert {row.source for row in rows} <= {"baseline"}
    return [
        (
            row.message_id,
            row.emoji,
            row.count,
            row.previous_count,
            row.observed_at if isinstance(row.observed_at, datetime) else datetime.fromisoformat(row.observed_at),
        )
        for row in rows
    ]


def _sorted(rows: list[tuple]) -> list[tuple]:
    return sorted(rows, key=lambda row: (row[0], row[1], row[4], row[2] == 0))


def test_upgrade_from_036_seeds_the_baseline_and_is_idempotent(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, "036")

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert "reaction_history" not in _tables(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0037', 'group', 0)"
                )
            )
            for user_id in (11, 12):
                conn.execute(
                    sa.text("INSERT INTO users (id, first_name, is_bot) VALUES (:id, 'Fake User', 0)"), {"id": user_id}
                )
            for message_id in (5, 6):
                conn.execute(
                    sa.text(
                        "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned, is_deleted) "
                        "VALUES (1, :id, -1001, :sent, 'Look at this', 0, 0, 0)"
                    ).bindparams(sa.bindparam("sent", type_=sa.DateTime())),
                    {"id": message_id, "sent": SENT},
                )
            for message_id, emoji, n, user_id, created_at, removed_at in REACTIONS:
                conn.execute(
                    sa.text(
                        "INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, count, created_at, removed_at) "
                        "VALUES (1, :m, -1001, :e, :u, :n, :c, :r)"
                    ).bindparams(
                        sa.bindparam("c", type_=sa.DateTime()),
                        sa.bindparam("r", type_=sa.DateTime()),
                    ),
                    {"m": message_id, "e": emoji, "u": user_id, "n": n, "c": created_at, "r": removed_at},
                )
            reactions_before = conn.execute(sa.text("SELECT * FROM reactions ORDER BY id")).all()

        _build_alembic_schema(async_url, "head")

        with engine.begin() as conn:
            assert "reaction_history" in _tables(conn)
            assert _sorted(_history(conn)) == _sorted(SEEDED)
            assert conn.execute(sa.text("SELECT * FROM reactions ORDER BY id")).all() == reactions_before
            index_names = {i["name"] for i in sa.inspect(conn).get_indexes("reaction_history")}
            assert {"ix_reaction_history_message", "ix_reaction_history_taken_back"} <= index_names
            # A re-run, or a create_all() database that already has the table and
            # its rows, adds nothing.
            _run(conn, migration_037.upgrade)
            assert _sorted(_history(conn)) == _sorted(SEEDED)

        with engine.begin() as conn:
            _run(conn, migration_037.downgrade)
            assert "reaction_history" not in _tables(conn)
            _run(conn, migration_037.downgrade)
            assert conn.execute(sa.text("SELECT * FROM reactions ORDER BY id")).all() == reactions_before
    finally:
        engine.dispose()


def test_a_message_with_history_keeps_it_and_gets_no_baseline(database_urls):
    """An emoji whose history already began (a create_all() database the new
    code wrote to) is left alone; another emoji of the same message is seeded."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, "head")
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0037', 'group', 0)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned, is_deleted) "
                    "VALUES (1, 5, -1001, :sent, 'Look at this', 0, 0, 0)"
                ).bindparams(sa.bindparam("sent", type_=sa.DateTime())),
                {"sent": SENT},
            )
            for emoji, n in (("👍", 4), ("🔥", 1)):
                conn.execute(
                    sa.text(
                        "INSERT INTO reactions (account_id, message_id, chat_id, emoji, count, created_at) "
                        "VALUES (1, 5, -1001, :e, :n, :c)"
                    ).bindparams(sa.bindparam("c", type_=sa.DateTime())),
                    {"e": emoji, "n": n, "c": FIRST_SEEN},
                )
            conn.execute(
                sa.text(
                    "INSERT INTO reaction_history (account_id, chat_id, message_id, emoji, count, observed_at, source) "
                    "VALUES (1, -1001, 5, '👍', 4, :t, 'listener')"
                ).bindparams(sa.bindparam("t", type_=sa.DateTime())),
                {"t": GONE},
            )
            _run(conn, migration_037.upgrade)
            rows = conn.execute(sa.text("SELECT emoji, count, source FROM reaction_history ORDER BY emoji, id")).all()
            assert [tuple(row) for row in rows] == [("👍", 4, "listener"), ("🔥", 1, "baseline")]
    finally:
        engine.dispose()
