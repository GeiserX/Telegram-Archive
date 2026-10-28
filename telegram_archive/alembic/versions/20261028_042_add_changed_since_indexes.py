"""Index the six timestamps ``export --changed-since`` selects on.

An incremental export keeps a message when the archive stored it, saw it
deleted, or added a text version, an earlier media, a poll or preview state or
a reaction state at or after a time (``_changed_since_condition``). None of
those six columns had an index, so the selection tested every message of the
archive, or of the chat, in each of the export's six statements. With one
index per column, each test is a range scan over the rows that changed, and
the export's cost follows how much changed rather than the archive's size.

Named explicitly rather than reflected from models.py, as 026 does: a
migration has to keep meaning what it meant when it was written. Guarded both
directions: a create_all() database already has these indexes from the model
declarations, and the entrypoint stamping ladder relies on every later
migration being idempotent.

Revision ID: 042
Revises: 041
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, column)
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("idx_messages_created_at", "messages", "created_at"),
    ("idx_messages_deleted_at", "messages", "deleted_at"),
    ("idx_message_versions_captured_at", "message_versions", "captured_at"),
    ("ix_media_versions_captured_at", "media_versions", "captured_at"),
    ("ix_message_snapshots_observed_at", "message_snapshots", "observed_at"),
    ("ix_reaction_history_observed_at", "reaction_history", "observed_at"),
)


def _live_indexes(inspector: sa.Inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for name, table, column in INDEXES:
        if table in tables and name not in _live_indexes(inspector, table):
            op.create_index(name, table, [column])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for name, table, _column in INDEXES:
        if table in tables and name in _live_indexes(inspector, table):
            op.drop_index(name, table_name=table)
