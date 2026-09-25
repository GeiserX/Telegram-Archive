"""Migration 032 - the append-only ``media_transcripts`` table and its search objects.

Follows the conventions of 031: inspector guards make a create_all() database
and a re-run both no-ops, the stamping ladder stays at 018, and ``downgrade``
drops the table together with its FTS objects. The FTS5 rebuild that indexes
existing rows follows 028: it runs only when this pass created the FTS table.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.db.fts import SQLITE_TRANSCRIPT_FTS_TABLE, SQLITE_TRANSCRIPT_TRIGGER_NAMES
from src.db.models import MediaTranscript

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260925_032_add_media_transcripts.py"
)
_spec = importlib.util.spec_from_file_location("migration_032", _MIGRATION_PATH)
migration_032 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_032)

TABLE = "media_transcripts"
EXPECTED_COLUMNS = {
    "id",
    "account_id",
    "media_id",
    "content_hash",
    "idempotency_key",
    "source",
    "engine_name",
    "engine_version",
    "preset",
    "models",
    "language",
    "language_confidence",
    "text",
    "words",
    "segments",
    "confidence",
    "duration_s",
    "job_id",
    "status",
    "error",
    "requested_at",
    "completed_at",
    "created_at",
}
EXPECTED_INDEXES = {
    "uq_media_transcripts_account_media_job": (["account_id", "media_id", "job_id"], True),
    "uq_media_transcripts_open": (["account_id", "media_id"], True),
    "ix_media_transcripts_account_media": (["account_id", "media_id"], False),
    "ix_media_transcripts_idempotency_key": (["idempotency_key"], False),
    "ix_media_transcripts_status": (["status"], False),
}


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _sqlite_objects(conn, kind: str) -> set[str]:
    rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type = :kind"), {"kind": kind})
    return {row[0] for row in rows}


def _indexes(conn) -> dict[str, tuple[list[str], bool]]:
    return {i["name"]: (list(i["column_names"]), bool(i["unique"])) for i in sa.inspect(conn).get_indexes(TABLE)}


def _insert(conn, media_id: str, status: str, text: str | None = None) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO media_transcripts (account_id, media_id, status, text, requested_at, created_at) "
            "VALUES (1, :media_id, :status, :text, '2026-01-02 03:04:05', '2026-01-02 03:04:05')"
        ),
        {"media_id": media_id, "status": status, "text": text},
    )


class TestMigration032:
    def test_revision_chain(self):
        assert migration_032.revision == "032"
        assert migration_032.down_revision == "031"

    def test_upgrade_creates_the_table_indexes_and_search_objects_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_032.upgrade)
            inspector = sa.inspect(conn)
            assert TABLE in inspector.get_table_names()
            columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
            assert set(columns) == EXPECTED_COLUMNS
            assert columns["status"]["nullable"] is False
            assert columns["job_id"]["nullable"] is True
            assert columns["completed_at"]["nullable"] is True
            assert _indexes(conn) == EXPECTED_INDEXES
            assert SQLITE_TRANSCRIPT_FTS_TABLE in _sqlite_objects(conn, "table")
            assert set(SQLITE_TRANSCRIPT_TRIGGER_NAMES) <= _sqlite_objects(conn, "trigger")

            _run(conn, migration_032.upgrade)  # re-run must be a no-op
            assert _indexes(conn) == EXPECTED_INDEXES
            assert set(SQLITE_TRANSCRIPT_TRIGGER_NAMES) <= _sqlite_objects(conn, "trigger")

    def test_partial_unique_index_allows_one_open_row_per_media(self):
        """The partial index is real: two open rows for one media are refused,
        while any number of closed rows and one open row coexist."""
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_032.upgrade)
            _insert(conn, "m1", "done")
            _insert(conn, "m1", "failed")
            _insert(conn, "m1", "queued")
            try:
                _insert(conn, "m1", "running")
            except sa.exc.IntegrityError:
                pass
            else:
                raise AssertionError("a second open row for the same media was accepted")

    def test_upgrade_noop_on_create_all_shape(self):
        """create_all() already built the table; a half-applied one gets its missing index."""
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            MediaTranscript.__table__.create(conn)
            conn.execute(sa.text("DROP INDEX ix_media_transcripts_status"))
            _run(conn, migration_032.upgrade)
            assert _indexes(conn) == EXPECTED_INDEXES
            assert SQLITE_TRANSCRIPT_FTS_TABLE in _sqlite_objects(conn, "table")
            _run(conn, migration_032.upgrade)
            assert _indexes(conn) == EXPECTED_INDEXES

    def test_existing_rows_are_indexed_once_and_new_rows_arrive_through_the_triggers(self):
        """The rebuild runs only when this pass created the FTS table (028's rule)."""
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            MediaTranscript.__table__.create(conn)
            _insert(conn, "m1", "done", "hola mundo")
            _run(conn, migration_032.upgrade)
            hits = conn.execute(
                sa.text("SELECT rowid FROM media_transcripts_fts WHERE media_transcripts_fts MATCH '\"hola\"*'")
            ).fetchall()
            assert len(hits) == 1

            _insert(conn, "m2", "done", "adios mundo")
            _run(conn, migration_032.upgrade)  # a re-run must not re-index (which would duplicate)
            hits = conn.execute(
                sa.text("SELECT rowid FROM media_transcripts_fts WHERE media_transcripts_fts MATCH '\"mundo\"*'")
            ).fetchall()
            assert len(hits) == 2

    def test_downgrade_drops_the_table_and_search_objects_and_is_idempotent(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_032.upgrade)
            _run(conn, migration_032.downgrade)
            assert TABLE not in sa.inspect(conn).get_table_names()
            assert SQLITE_TRANSCRIPT_FTS_TABLE not in _sqlite_objects(conn, "table")
            assert not set(SQLITE_TRANSCRIPT_TRIGGER_NAMES) & _sqlite_objects(conn, "trigger")
            _run(conn, migration_032.downgrade)
            assert TABLE not in sa.inspect(conn).get_table_names()

    def test_roundtrip_upgrade_downgrade_upgrade(self):
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn:
            _run(conn, migration_032.upgrade)
            _run(conn, migration_032.downgrade)
            _run(conn, migration_032.upgrade)
            assert _indexes(conn) == EXPECTED_INDEXES
            assert set(SQLITE_TRANSCRIPT_TRIGGER_NAMES) <= _sqlite_objects(conn, "trigger")
