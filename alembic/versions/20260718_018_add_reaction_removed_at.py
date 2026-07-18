"""Add reactions.removed_at tombstone + (chat_id, message_id) index (#219).

Real-time reaction capture retains a removed reaction (soft-delete via
removed_at) instead of dropping it, and the page read filters on
(chat_id, message_id) — add the chat-first composite index to match the
schema's convention and serve that read.

Revision ID: 018
Revises: 017
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "reactions"
COLUMN_NAME = "removed_at"
INDEX_NAME = "idx_reactions_chat_message"


def _column_exists(inspector: sa.Inspector) -> bool:
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def _index_exists(inspector: sa.Inspector) -> bool:
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Idempotent: create_all()-provisioned databases may already have these.
    if TABLE_NAME not in inspector.get_table_names():
        return

    if not _column_exists(inspector):
        # Nullable, no server_default: existing rows are treated as active
        # (removed_at IS NULL), which is the correct pre-feature state.
        op.add_column(TABLE_NAME, sa.Column("removed_at", sa.DateTime(), nullable=True))

    if not _index_exists(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, ["chat_id", "message_id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        return

    if _index_exists(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)

    if _column_exists(inspector):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
