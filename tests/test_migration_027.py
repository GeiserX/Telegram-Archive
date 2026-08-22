"""Migration 027 - widen idx_messages_topic with date, guarded both directions.

The index keeps its name across the widening, so the guards key on the COLUMN
LIST: an upgraded database carries the 2-column shape, a create_all() database
already has the 4-column shape from the model declaration and must no-op. The
entrypoint stamping ladder tops out at 018 and relies on exactly this.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

spec = importlib.util.spec_from_file_location(
    "migration_027", _VERSIONS_DIR / "20260822_027_widen_topic_index_with_date.py"
)
migration_027 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration_027)


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _index_columns(conn):
    for index in sa.inspect(conn).get_indexes("messages"):
        if index["name"] == "idx_messages_topic":
            return list(index["column_names"])
    return None


def _create_messages_table(conn, index_columns=None):
    conn.execute(
        sa.text(
            "CREATE TABLE messages (account_id INTEGER NOT NULL DEFAULT 1, id BIGINT NOT NULL, "
            "chat_id BIGINT, reply_to_top_id BIGINT, date TIMESTAMP, text TEXT, "
            "PRIMARY KEY (account_id, id))"
        )
    )
    if index_columns:
        conn.execute(sa.text(f"CREATE INDEX idx_messages_topic ON messages ({', '.join(index_columns)})"))


def test_revision_chain():
    assert migration_027.revision == "027"
    assert migration_027.down_revision == "026"


def test_upgrade_widens_the_old_shape_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn, ["chat_id", "reply_to_top_id"])

        _run(conn, migration_027.upgrade)
        assert _index_columns(conn) == ["chat_id", "account_id", "reply_to_top_id", "date"]

        _run(conn, migration_027.upgrade)  # re-run must be a no-op
        assert _index_columns(conn) == ["chat_id", "account_id", "reply_to_top_id", "date"]


def test_upgrade_noop_on_create_all_shape():
    """Simulates create_all(): the model already declares the 4-column index."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn, ["chat_id", "account_id", "reply_to_top_id", "date"])

        _run(conn, migration_027.upgrade)  # must not raise or churn
        assert _index_columns(conn) == ["chat_id", "account_id", "reply_to_top_id", "date"]


def test_upgrade_creates_index_when_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn)

        _run(conn, migration_027.upgrade)
        assert _index_columns(conn) == ["chat_id", "account_id", "reply_to_top_id", "date"]


def test_upgrade_noop_when_messages_table_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_027.upgrade)  # no messages table -> no-op, no raise
        assert "messages" not in sa.inspect(conn).get_table_names()


def test_downgrade_restores_the_old_shape_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn, ["chat_id", "account_id", "reply_to_top_id", "date"])

        _run(conn, migration_027.downgrade)
        assert _index_columns(conn) == ["chat_id", "reply_to_top_id"]

        _run(conn, migration_027.downgrade)  # re-run must be a no-op
        assert _index_columns(conn) == ["chat_id", "reply_to_top_id"]


def test_roundtrip_upgrade_downgrade_upgrade():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table(conn, ["chat_id", "reply_to_top_id"])

        _run(conn, migration_027.upgrade)
        _run(conn, migration_027.downgrade)
        assert _index_columns(conn) == ["chat_id", "reply_to_top_id"]

        _run(conn, migration_027.upgrade)
        assert _index_columns(conn) == ["chat_id", "account_id", "reply_to_top_id", "date"]
