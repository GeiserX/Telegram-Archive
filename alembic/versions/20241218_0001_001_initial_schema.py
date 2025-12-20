"""Initial schema matching v2.x database structure.

Revision ID: 001
Revises: 
Create Date: 2024-12-18

This migration represents the existing v2.x schema.
For users upgrading from v2.x, this migration will be auto-stamped
as already applied since the tables already exist.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial schema."""
    # Chats table
    op.create_table(
        'chats',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('title', sa.String(255), nullable=True),
        sa.Column('username', sa.String(255), nullable=True),
        sa.Column('first_name', sa.String(255), nullable=True),
        sa.Column('last_name', sa.String(255), nullable=True),
        sa.Column('phone', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('participants_count', sa.Integer(), nullable=True),
        sa.Column('last_synced_message_id', sa.BigInteger(), nullable=True, default=0),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Users table
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(255), nullable=True),
        sa.Column('first_name', sa.String(255), nullable=True),
        sa.Column('last_name', sa.String(255), nullable=True),
        sa.Column('phone', sa.String(50), nullable=True),
        sa.Column('is_bot', sa.Integer(), nullable=True, default=0),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Messages table
    op.create_table(
        'messages',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('sender_id', sa.BigInteger(), nullable=True),
        sa.Column('date', sa.DateTime(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('reply_to_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('reply_to_text', sa.Text(), nullable=True),
        sa.Column('forward_from_id', sa.BigInteger(), nullable=True),
        sa.Column('edit_date', sa.DateTime(), nullable=True),
        sa.Column('media_type', sa.String(50), nullable=True),
        sa.Column('media_id', sa.String(255), nullable=True),
        sa.Column('media_path', sa.String(500), nullable=True),
        sa.Column('raw_data', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('is_outgoing', sa.Integer(), nullable=True, default=0),
        sa.PrimaryKeyConstraint('id', 'chat_id'),
        sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], )
    )
    op.create_index('idx_messages_chat_id', 'messages', ['chat_id'])
    op.create_index('idx_messages_date', 'messages', ['date'])
    op.create_index('idx_messages_sender_id', 'messages', ['sender_id'])
    
    # Media table
    op.create_table(
        'media',
        sa.Column('id', sa.String(255), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('type', sa.String(50), nullable=True),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('file_name', sa.String(255), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('mime_type', sa.String(100), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('downloaded', sa.Integer(), nullable=True, default=0),
        sa.Column('download_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_media_message', 'media', ['message_id', 'chat_id'])
    
    # Reactions table
    op.create_table(
        'reactions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('emoji', sa.String(50), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('count', sa.Integer(), nullable=True, default=1),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'chat_id', 'emoji', 'user_id', name='uq_reaction')
    )
    op.create_index('idx_reactions_message', 'reactions', ['message_id', 'chat_id'])
    
    # Sync status table
    op.create_table(
        'sync_status',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('last_message_id', sa.BigInteger(), nullable=True, default=0),
        sa.Column('last_sync_date', sa.DateTime(), nullable=True),
        sa.Column('message_count', sa.Integer(), nullable=True, default=0),
        sa.PrimaryKeyConstraint('chat_id'),
        sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], )
    )
    
    # Metadata table
    op.create_table(
        'metadata',
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('metadata')
    op.drop_table('sync_status')
    op.drop_index('idx_reactions_message', 'reactions')
    op.drop_table('reactions')
    op.drop_index('idx_media_message', 'media')
    op.drop_table('media')
    op.drop_index('idx_messages_sender_id', 'messages')
    op.drop_index('idx_messages_date', 'messages')
    op.drop_index('idx_messages_chat_id', 'messages')
    op.drop_table('messages')
    op.drop_table('users')
    op.drop_table('chats')

