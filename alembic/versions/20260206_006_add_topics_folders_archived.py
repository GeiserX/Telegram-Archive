"""Add forum topics, chat folders, and archived chat support.

This migration:
1. Adds is_forum and is_archived columns to chats table
2. Adds reply_to_top_id column to messages table (for forum topic threading)
3. Creates forum_topics table for topic metadata
4. Creates chat_folders and chat_folder_members tables

Revision ID: 006
Revises: 005
Create Date: 2026-02-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add topics, folders, and archived chat support."""

    conn = op.get_bind()
    dialect = conn.dialect.name

    # =========================================================================
    # STEP 1: Add columns to chats table
    # =========================================================================

    # is_forum: whether the chat is a forum with topics
    op.add_column('chats', sa.Column('is_forum', sa.Integer(), nullable=True, server_default='0'))
    # is_archived: whether the chat is in the archive folder
    op.add_column('chats', sa.Column('is_archived', sa.Integer(), nullable=True, server_default='0'))

    # =========================================================================
    # STEP 2: Add reply_to_top_id to messages table
    # =========================================================================

    op.add_column('messages', sa.Column('reply_to_top_id', sa.BigInteger(), nullable=True))

    # Index for fast topic message lookups
    op.create_index('idx_messages_topic', 'messages', ['chat_id', 'reply_to_top_id'])

    # =========================================================================
    # STEP 3: Create forum_topics table
    # =========================================================================

    op.create_table(
        'forum_topics',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), sa.ForeignKey('chats.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('icon_color', sa.Integer(), nullable=True),
        sa.Column('icon_emoji_id', sa.BigInteger(), nullable=True),
        sa.Column('icon_emoji', sa.String(32), nullable=True),
        sa.Column('is_closed', sa.Integer(), server_default='0'),
        sa.Column('is_pinned', sa.Integer(), server_default='0'),
        sa.Column('is_hidden', sa.Integer(), server_default='0'),
        sa.Column('date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id', 'chat_id'),
    )
    op.create_index('idx_forum_topics_chat', 'forum_topics', ['chat_id'])

    # =========================================================================
    # STEP 4: Create chat_folders table
    # =========================================================================

    op.create_table(
        'chat_folders',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('emoticon', sa.String(50), nullable=True),
        sa.Column('sort_order', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )

    # =========================================================================
    # STEP 5: Create chat_folder_members table
    # =========================================================================

    op.create_table(
        'chat_folder_members',
        sa.Column('folder_id', sa.Integer(), sa.ForeignKey('chat_folders.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), sa.ForeignKey('chats.id', ondelete='CASCADE'), nullable=False),
        sa.PrimaryKeyConstraint('folder_id', 'chat_id'),
    )
    op.create_index('idx_folder_members_chat', 'chat_folder_members', ['chat_id'])
    op.create_index('idx_folder_members_folder', 'chat_folder_members', ['folder_id'])


def downgrade() -> None:
    """Remove topics, folders, and archived chat support."""

    op.drop_index('idx_folder_members_folder', table_name='chat_folder_members')
    op.drop_index('idx_folder_members_chat', table_name='chat_folder_members')
    op.drop_table('chat_folder_members')
    op.drop_table('chat_folders')
    op.drop_index('idx_forum_topics_chat', table_name='forum_topics')
    op.drop_table('forum_topics')
    op.drop_index('idx_messages_topic', table_name='messages')
    op.drop_column('messages', 'reply_to_top_id')
    op.drop_column('chats', 'is_archived')
    op.drop_column('chats', 'is_forum')
