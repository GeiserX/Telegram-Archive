"""Full-text search: FTS5 external-content table (SQLite) / tsvector GIN (PostgreSQL).

Search was ILIKE '%q%' — no index can serve a leading wildcard, so every
search scanned the chat (or the whole archive on the global path). Official
apps search indexed and instantly. This migration adds the index layer both
engines need; the adapter uses it when present and keeps ILIKE where the
migration has not run (fresh create_all() databases get the tables here on
their first upgrade pass).

Idempotence: every statement is IF NOT EXISTS, and the one non-DDL step —
the FTS5 rebuild that indexes pre-existing rows — runs only when this pass
actually created the table, so a re-run against an already-migrated
database does nothing. The entrypoint stamping ladder tops out at 018 and
relies on exactly that (see migration 027's header).

Revision ID: 028
Revises: 027
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.db.fts import (
    PG_ADD_COLUMN,
    PG_CREATE_INDEX,
    PG_TSVECTOR_COLUMN,
    SQLITE_CREATE_FTS,
    SQLITE_FTS_TABLE,
    SQLITE_REBUILD,
    SQLITE_TRIGGERS,
)

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        fts5_available = (
            conn.exec_driver_sql("SELECT 1 FROM pragma_compile_options WHERE compile_options='ENABLE_FTS5'").first()
            is not None
        )
        if not fts5_available:
            # Exotic SQLite builds without FTS5: search stays on ILIKE (the
            # adapter probes for the table and keeps the old path). Never
            # fail the upgrade ladder over a search accelerator.
            return
        already_present = (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (SQLITE_FTS_TABLE,),
            ).first()
            is not None
        )
        conn.exec_driver_sql(SQLITE_CREATE_FTS)
        for trigger_sql in SQLITE_TRIGGERS:
            conn.exec_driver_sql(trigger_sql)
        if not already_present:
            # Index the rows that existed before this migration; new rows
            # arrive via the triggers.
            conn.exec_driver_sql(SQLITE_REBUILD)
    else:
        conn.exec_driver_sql(PG_ADD_COLUMN)
        conn.exec_driver_sql(PG_CREATE_INDEX)


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        for trigger_name in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
            conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {SQLITE_FTS_TABLE}")
    else:
        conn.exec_driver_sql("DROP INDEX IF EXISTS idx_messages_text_search")
        inspector = sa.inspect(conn)
        if PG_TSVECTOR_COLUMN in {c["name"] for c in inspector.get_columns("messages")}:
            op.drop_column("messages", PG_TSVECTOR_COLUMN)
