"""Migration 033: ``media_transcripts.options_tag``, added by an inspector-guarded step."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parent.parent / "telegram_archive" / "alembic" / "versions"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration_032 = _load("migration_032", "20260925_032_add_media_transcripts.py")
migration_033 = _load("migration_033", "20260927_033_add_transcript_options_tag.py")


def _run(conn, fn) -> None:
    context = MigrationContext.configure(conn)
    with Operations.context(context):
        fn()


def _columns(conn) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns("media_transcripts")}


def test_revision_chain():
    assert (migration_033.revision, migration_033.down_revision) == ("033", "032")


def test_upgrade_adds_the_column_keeps_rows_and_is_idempotent_and_downgrade_drops_it():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_032.upgrade)
        conn.execute(
            sa.text(
                "INSERT INTO media_transcripts (account_id, media_id, status, text, requested_at, created_at) "
                "VALUES (1, 'm1', 'done', 'kept', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        assert "options_tag" not in _columns(conn)
        _run(conn, migration_033.upgrade)
        assert "options_tag" in _columns(conn)
        assert conn.execute(sa.text("SELECT text, options_tag FROM media_transcripts")).all() == [("kept", None)]
        _run(conn, migration_033.upgrade)  # a re-run, or a create_all() database, changes nothing
        assert "options_tag" in _columns(conn)
        _run(conn, migration_033.downgrade)
        assert "options_tag" not in _columns(conn)
        _run(conn, migration_033.downgrade)
        assert conn.execute(sa.text("SELECT text FROM media_transcripts")).all() == [("kept",)]


def test_upgrade_without_the_table_does_nothing():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_033.upgrade)
        assert "media_transcripts" not in sa.inspect(conn).get_table_names()
