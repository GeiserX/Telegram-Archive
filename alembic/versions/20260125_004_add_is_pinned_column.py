"""Add is_pinned column to messages table for pinned message tracking.

Revision ID: 004
Revises: 003
Create Date: 2026-01-25

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "messages"
COLUMN_NAME = "is_pinned"
INDEX_NAME = "idx_messages_chat_pinned"


def _column_exists(inspector: sa.Inspector) -> bool:
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def _index_exists(inspector: sa.Inspector) -> bool:
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    """Add is_pinned column and index for pinned message queries."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        return

    # Add is_pinned column with default 0 (not pinned)
    # Idempotent: create_all()-provisioned databases may already have this column.
    if not _column_exists(inspector):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Integer(), nullable=False, server_default="0"))
        inspector = sa.inspect(conn)

    # Create composite index for efficient pinned message queries per chat
    if not _index_exists(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, ["chat_id", "is_pinned"])


def downgrade() -> None:
    """Remove is_pinned column and index."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        return

    if _index_exists(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
        inspector = sa.inspect(conn)

    if _column_exists(inspector):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
