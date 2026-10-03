"""Migration 040: the ``custom_emoji`` table and its seed from stored reactions.

The upgrade runs from the revision before to head on SQLite and on PostgreSQL
with reactions from before: every custom emoji stored as a reaction, in
``reactions`` or only in ``reaction_history``, gets one pending row dated when
the archive first saw it. A value that is not ``custom_`` and digits adds
nothing. A re-run adds nothing, every other table keeps its rows byte for
byte, and the downgrade removes only the new table.

The revision ids are read from the migration module, so renumbering the chain
at merge time changes only the migration file.
"""

import hashlib
import importlib.util
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
EARLY = datetime(2026, 1, 1, 9, 5, 0)
LATER = datetime(2026, 1, 2, 9, 5, 0)
# Above 2**53, as real document ids are.
BIG = 5000000000000000001
OTHER = 5000000000000000002
HISTORY_ONLY = 5000000000000000003


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("migration_custom_emoji", "20261004_040_add_custom_emoji.py")


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
    assert list(revisions.values()).count(migration.down_revision) == 1
    assert list(revisions.values()).count(migration.revision) <= 1


def test_upgrade_on_an_empty_database_creates_the_table_and_seeds_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        assert _tables(conn) == {"custom_emoji"}
        assert conn.execute(sa.text("SELECT COUNT(*) FROM custom_emoji")).scalar() == 0
        _run(conn, migration.downgrade)
        assert _tables(conn) == set()


@pytest.fixture(params=("sqlite", "postgresql"))
def database_urls(request, tmp_path, postgres_server_url, make_postgres_database) -> tuple[str, str]:
    """(async url, sync url) of an empty database on each backend."""
    if request.param == "postgresql":
        if not postgres_server_url:
            pytest.skip(NO_POSTGRES_REASON)
        return make_postgres_database("telegram_archive_migration_040")
    path = tmp_path / "archive.db"
    return f"sqlite+aiosqlite:///{path}", f"sqlite:///{path}"


def _fingerprint(conn) -> dict[str, tuple[int, str]]:
    """(row count, checksum of every row) of each table but the new one.

    SQLite's full-text index keeps its own tables, which it rewrites on its own
    when an upgrade opens the database; the indexed rows are in ``messages``.
    """
    prints = {}
    for table in sorted(_tables(conn) - {"custom_emoji", "alembic_version"}):
        if "_fts" in table:
            continue
        rows = conn.execute(sa.text(f'SELECT * FROM "{table}"')).all()
        digest = hashlib.sha256(repr(sorted(repr(tuple(row)) for row in rows)).encode()).hexdigest()
        prints[table] = (len(rows), digest)
    return prints


def _emoji(conn) -> list[tuple]:
    rows = conn.execute(
        sa.text(
            "SELECT document_id, downloaded, attempts, skip_reason, file_name, first_seen FROM custom_emoji "
            "ORDER BY document_id"
        )
    ).all()
    return [
        (
            row.document_id,
            row.downloaded,
            row.attempts,
            row.skip_reason,
            row.file_name,
            row.first_seen if isinstance(row.first_seen, datetime) else datetime.fromisoformat(row.first_seen),
        )
        for row in rows
    ]


def test_upgrade_seeds_from_reactions_and_history_and_changes_nothing_else(database_urls):
    """Synchronous on purpose: Alembic's env.py runs its own event loop."""
    async_url, sync_url = database_urls
    _build_alembic_schema(async_url, migration.down_revision)

    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            assert "custom_emoji" not in _tables(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO chats (account_id, id, ref, type, last_synced_message_id) "
                    "VALUES (1, -1001, 'ref0040', 'group', 0)"
                )
            )
            for message_id in (5, 6):
                conn.execute(
                    sa.text(
                        "INSERT INTO messages (account_id, id, chat_id, date, text, is_outgoing, is_pinned, is_deleted) "
                        "VALUES (1, :id, -1001, :sent, 'Look at this', 0, 0, 0)"
                    ).bindparams(sa.bindparam("sent", type_=sa.DateTime())),
                    {"id": message_id, "sent": SENT},
                )
            reactions = [
                (5, f"custom_{BIG}", LATER),
                (6, f"custom_{BIG}", EARLY),  # the same emoji, seen earlier on another message
                (5, f"custom_{OTHER}", LATER),
                (5, "👍", EARLY),
                (6, "custom_abc", EARLY),  # not an id
                (6, "custom_", EARLY),
                (6, "paid", EARLY),
            ]
            for message_id, emoji, created_at in reactions:
                conn.execute(
                    sa.text(
                        "INSERT INTO reactions (account_id, message_id, chat_id, emoji, count, created_at) "
                        "VALUES (1, :m, -1001, :e, 1, :c)"
                    ).bindparams(sa.bindparam("c", type_=sa.DateTime())),
                    {"m": message_id, "e": emoji, "c": created_at},
                )
            # An emoji only the history still names (its reactions row went with
            # a legacy delete), and one the history saw before its reactions row.
            for emoji, observed_at in ((f"custom_{HISTORY_ONLY}", LATER), (f"custom_{OTHER}", EARLY)):
                conn.execute(
                    sa.text(
                        "INSERT INTO reaction_history (account_id, chat_id, message_id, emoji, count, observed_at, source) "
                        "VALUES (1, -1001, 6, :e, 1, :t, 'listener')"
                    ).bindparams(sa.bindparam("t", type_=sa.DateTime())),
                    {"e": emoji, "t": observed_at},
                )
            before = _fingerprint(conn)

        _build_alembic_schema(async_url, "head")

        expected = [
            (BIG, 0, 0, None, None, EARLY),
            (OTHER, 0, 0, None, None, EARLY),
            (HISTORY_ONLY, 0, 0, None, None, LATER),
        ]
        with engine.begin() as conn:
            assert _emoji(conn) == expected
            assert _fingerprint(conn) == before
            # A re-run, or a create_all() database that already has the rows, adds nothing.
            _run(conn, migration.upgrade)
            assert _emoji(conn) == expected

        with engine.begin() as conn:
            # A row the backup filled meanwhile is never touched by a re-run.
            conn.execute(
                sa.text(f"UPDATE custom_emoji SET downloaded = 1, file_name = '{BIG}.webp' WHERE document_id = {BIG}")
            )
            _run(conn, migration.upgrade)
            assert _emoji(conn)[0][:5] == (BIG, 1, 0, None, f"{BIG}.webp")

        with engine.begin() as conn:
            _run(conn, migration.downgrade)
            assert "custom_emoji" not in _tables(conn)
            _run(conn, migration.downgrade)
            assert _fingerprint(conn) == before
    finally:
        engine.dispose()
