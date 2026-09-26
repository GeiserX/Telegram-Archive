"""Keep every transcript of a voice message or round video (docs/TRANSCRIPTION.md).

``media_transcripts`` is append-only: a transcript is a new row here, never
a change to the media row, and a second transcript with another engine or
preset is another row. ``status`` advances and every other column is written
once. There is no foreign key to ``media`` on purpose: the voice-note twin
cleanup removes media rows, and the viewer reattaches a transcript by hash.

Two unique indexes: a plain one on (account_id, media_id, job_id), and a
partial one on (account_id, media_id) over the open statuses (queued,
running), which both databases support, so the drain in the backup and the
ask-now route in the viewer can never leave two open rows for one media.

Search objects follow migration 028: SQLite gets an external-content FTS5
table kept in sync by triggers, PostgreSQL a stored generated tsvector with
a GIN index. The FTS5 rebuild that indexes existing rows runs only when this
pass created the FTS table, so a re-run does nothing.

``job_stored_at`` records when a row got its job id, so the straggler poll
and the retention expiry count from the submit, not from the insert. A table
made before the column existed gets it added.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has every object, and every step is guarded by the
inspector or by IF NOT EXISTS.

Revision ID: 032
Revises: 031
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.db.fts import (
    PG_TRANSCRIPT_ADD_COLUMN,
    PG_TRANSCRIPT_CREATE_INDEX,
    PG_TRANSCRIPT_INDEX_NAME,
    PG_TSVECTOR_COLUMN,
    SQLITE_CREATE_TRANSCRIPT_FTS,
    SQLITE_TRANSCRIPT_FTS_TABLE,
    SQLITE_TRANSCRIPT_REBUILD,
    SQLITE_TRANSCRIPT_TRIGGER_NAMES,
    SQLITE_TRANSCRIPT_TRIGGERS,
    sqlite_has_fts5,
)

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media_transcripts"

OPEN_WHERE = sa.text("status IN ('queued', 'running')")

# name -> (columns, unique, partial)
INDEXES: dict[str, tuple[list[str], bool, bool]] = {
    "uq_media_transcripts_account_media_job": (["account_id", "media_id", "job_id"], True, False),
    "uq_media_transcripts_open": (["account_id", "media_id"], True, True),
    "ix_media_transcripts_account_media": (["account_id", "media_id"], False, False),
    "ix_media_transcripts_idempotency_key": (["idempotency_key"], False, False),
    "ix_media_transcripts_status": (["status"], False, False),
}


def _create_search_objects(conn) -> None:
    if conn.dialect.name == "sqlite":
        if not sqlite_has_fts5(conn):
            # Exotic SQLite builds without FTS5: transcripts are stored and
            # served, only not searchable. Never fail the upgrade ladder.
            return
        already_present = (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (SQLITE_TRANSCRIPT_FTS_TABLE,),
            ).first()
            is not None
        )
        conn.exec_driver_sql(SQLITE_CREATE_TRANSCRIPT_FTS)
        for trigger_sql in SQLITE_TRANSCRIPT_TRIGGERS:
            conn.exec_driver_sql(trigger_sql)
        if not already_present:
            conn.exec_driver_sql(SQLITE_TRANSCRIPT_REBUILD)
    else:
        conn.exec_driver_sql(PG_TRANSCRIPT_ADD_COLUMN)
        conn.exec_driver_sql(PG_TRANSCRIPT_CREATE_INDEX)


def _drop_search_objects(conn) -> None:
    if conn.dialect.name == "sqlite":
        for trigger_name in SQLITE_TRANSCRIPT_TRIGGER_NAMES:
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {SQLITE_TRANSCRIPT_FTS_TABLE}")
    else:
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {PG_TRANSCRIPT_INDEX_NAME}")
        inspector = sa.inspect(conn)
        if PG_TSVECTOR_COLUMN in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
            op.drop_column(TABLE_NAME, PG_TSVECTOR_COLUMN)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("media_id", sa.String(255), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("idempotency_key", sa.String(64), nullable=True),
            sa.Column("source", sa.String(16), nullable=True),
            sa.Column("engine_name", sa.String(255), nullable=True),
            sa.Column("engine_version", sa.String(255), nullable=True),
            sa.Column("preset", sa.String(16), nullable=True),
            sa.Column("models", sa.Text(), nullable=True),
            sa.Column("language", sa.String(16), nullable=True),
            sa.Column("language_confidence", sa.Float(), nullable=True),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("words", sa.Text(), nullable=True),
            sa.Column("segments", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("duration_s", sa.Float(), nullable=True),
            sa.Column("job_id", sa.String(255), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("requested_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("job_stored_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(conn)
    elif "job_stored_at" not in {c["name"] for c in inspector.get_columns(TABLE_NAME)}:
        # A table made by an earlier build of this unreleased migration.
        op.add_column(TABLE_NAME, sa.Column("job_stored_at", sa.DateTime(), nullable=True))
        inspector = sa.inspect(conn)

    existing = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    for name, (columns, unique, partial) in INDEXES.items():
        if name in existing:
            continue
        kwargs = {"sqlite_where": OPEN_WHERE, "postgresql_where": OPEN_WHERE} if partial else {}
        op.create_index(name, TABLE_NAME, columns, unique=unique, **kwargs)

    _create_search_objects(conn)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    _drop_search_objects(conn)
    existing = {idx["name"] for idx in sa.inspect(conn).get_indexes(TABLE_NAME)}
    for name in INDEXES:
        if name in existing:
            op.drop_index(name, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
