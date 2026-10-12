"""Migration 041: ``media_transcripts`` columns an earlier build of 032 never made.

A database that applied the earlier 032 lacks ``job_stored_at``,
``copied_from_id`` and ``diarize``; the upgrade adds whichever is missing, keeps
every row, and leaves a complete table alone.
"""

import importlib.util
import re
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"


def _load():
    spec = importlib.util.spec_from_file_location(
        "migration_041", _VERSIONS / "20261012_041_repair_transcript_columns.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load()


def _run(conn, fn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        fn()


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("media_transcripts")}


def _old_table(conn) -> None:
    conn.execute(sa.text("CREATE TABLE media_transcripts (id INTEGER PRIMARY KEY, status VARCHAR(16) NOT NULL)"))
    conn.execute(sa.text("INSERT INTO media_transcripts (id, status) VALUES (1, 'done')"))


def test_revision_chain_does_not_fork():
    revisions = {}
    for path in _VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision: str = "([^"]+)"', source, re.M)
        down = re.search(r'^down_revision: str \| None = "([^"]+)"', source, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    assert migration.down_revision in revisions
    assert list(revisions.values()).count(migration.down_revision) == 1


def test_no_table_is_a_no_op():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)
        assert sa.inspect(conn).get_table_names() == []


def test_a_table_from_the_earlier_build_gains_the_missing_columns_and_keeps_its_rows():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _old_table(conn)
        _run(conn, migration.upgrade)
        assert {"job_stored_at", "copied_from_id", "diarize"} <= _columns(conn)
        assert conn.execute(sa.text("SELECT id, status FROM media_transcripts")).all() == [(1, "done")]


def test_a_partly_repaired_table_gains_only_what_is_missing_and_a_rerun_is_a_no_op():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _old_table(conn)
        conn.execute(sa.text("ALTER TABLE media_transcripts ADD COLUMN diarize BOOLEAN"))
        _run(conn, migration.upgrade)
        _run(conn, migration.upgrade)
        assert {"job_stored_at", "copied_from_id", "diarize"} <= _columns(conn)


def test_downgrade_removes_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _old_table(conn)
        _run(conn, migration.upgrade)
        before = _columns(conn)
        _run(conn, migration.downgrade)
        assert _columns(conn) == before
