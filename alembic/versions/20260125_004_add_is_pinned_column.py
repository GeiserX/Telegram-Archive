"""Add is_pinned column to messages table for pinned message tracking.

Revision ID: 004
Revises: 003
Create Date: 2026-01-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_pinned column and index for pinned message queries."""
    # Add is_pinned column with default 0 (not pinned)
    op.add_column('messages', sa.Column('is_pinned', sa.Integer(), nullable=False, server_default='0'))
    
    # Create composite index for efficient pinned message queries per chat
    op.create_index('idx_messages_chat_pinned', 'messages', ['chat_id', 'is_pinned'])


def downgrade() -> None:
    """Remove is_pinned column and index."""
    op.drop_index('idx_messages_chat_pinned', table_name='messages')
    op.drop_column('messages', 'is_pinned')
