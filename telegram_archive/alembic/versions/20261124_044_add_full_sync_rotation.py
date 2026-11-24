"""Track the rotating full deletion and edit pass per chat, on sync_status.

With SYNC_DELETIONS_EDITS_DAYS above 0 a run re-checks only recent messages.
SYNC_DELETIONS_EDITS_OLDER_PER_RUN (#641) re-checks older messages a slice per
run, rotating over the chats whose last full pass is oldest. Two columns hold
where each (account, chat) stands:

- ``full_sync_cursor``: the pass has checked every older message with an id at
  or above it and continues below it. NULL starts at the newest one.
- ``full_sync_completed_at``: when the last pass reached the oldest message.
  NULL means the chat never finished one, so it goes first.

Both are progress bookkeeping, not observed archive state, and move in place.
Nullable, no backfill: every existing chat starts as never completed.

Idempotent in both directions: a create_all() database already has the columns
from the model declarations, and the entrypoint stamping ladder stops at 018,
so every later migration may meet a schema that already holds its objects.

Revision ID: 044
Revises: 043
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "sync_status"
COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("full_sync_cursor", sa.BigInteger()),
    ("full_sync_completed_at", sa.DateTime()),
)


def _existing_columns(inspector: sa.Inspector) -> set[str] | None:
    """Column names of sync_status, or None when the table is absent."""
    if TABLE not in set(inspector.get_table_names()):
        return None
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing_columns(sa.inspect(op.get_bind()))
    if existing is None:
        return
    for name, type_ in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = _existing_columns(sa.inspect(op.get_bind()))
    if existing is None:
        return
    for name, _type in COLUMNS:
        if name in existing:
            op.drop_column(TABLE, name)
