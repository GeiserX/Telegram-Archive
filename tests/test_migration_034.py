"""Migration 034: ``messages.edit_hide``, added by an inspector-guarded step."""

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
