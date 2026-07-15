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

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def _index_exists(inspector: sa.Inspector, table: str, index: str) -> bool:
    return index in {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    """Add topics, folders, and archived chat support."""

    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 1: Add columns to chats table
    # =========================================================================

    if "chats" in inspector.get_table_names():
        # is_forum: whether the chat is a forum with topics
        if not _column_exists(inspector, "chats", "is_forum"):
            op.add_column("chats", sa.Column("is_forum", sa.Integer(), nullable=True, server_default="0"))
        # is_archived: whether the chat is in the archive folder
        if not _column_exists(inspector, "chats", "is_archived"):
            op.add_column("chats", sa.Column("is_archived", sa.Integer(), nullable=True, server_default="0"))
        inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 2: Add reply_to_top_id to messages table
    # =========================================================================

    if "messages" in inspector.get_table_names():
        if not _column_exists(inspector, "messages", "reply_to_top_id"):
            op.add_column("messages", sa.Column("reply_to_top_id", sa.BigInteger(), nullable=True))
            inspector = sa.inspect(conn)

        # Index for fast topic message lookups
        if not _index_exists(inspector, "messages", "idx_messages_topic"):
            op.create_index("idx_messages_topic", "messages", ["chat_id", "reply_to_top_id"])
        inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 3: Create forum_topics table
    # =========================================================================

    if "forum_topics" not in inspector.get_table_names():
        op.create_table(
            "forum_topics",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("icon_color", sa.Integer(), nullable=True),
            sa.Column("icon_emoji_id", sa.BigInteger(), nullable=True),
            sa.Column("icon_emoji", sa.String(32), nullable=True),
            sa.Column("is_closed", sa.Integer(), server_default="0"),
            sa.Column("is_pinned", sa.Integer(), server_default="0"),
            sa.Column("is_hidden", sa.Integer(), server_default="0"),
            sa.Column("date", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id", "chat_id"),
        )
        inspector = sa.inspect(conn)
    if not _index_exists(inspector, "forum_topics", "idx_forum_topics_chat"):
        op.create_index("idx_forum_topics_chat", "forum_topics", ["chat_id"])
    inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 4: Create chat_folders table
    # =========================================================================

    if "chat_folders" not in inspector.get_table_names():
        op.create_table(
            "chat_folders",
            sa.Column("id", sa.Integer(), nullable=False, autoincrement=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("emoticon", sa.String(50), nullable=True),
            sa.Column("sort_order", sa.Integer(), server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 5: Create chat_folder_members table
    # =========================================================================

    if "chat_folder_members" not in inspector.get_table_names():
        op.create_table(
            "chat_folder_members",
            sa.Column("folder_id", sa.Integer(), sa.ForeignKey("chat_folders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
            sa.PrimaryKeyConstraint("folder_id", "chat_id"),
        )
        inspector = sa.inspect(conn)
    if not _index_exists(inspector, "chat_folder_members", "idx_folder_members_chat"):
        op.create_index("idx_folder_members_chat", "chat_folder_members", ["chat_id"])
    inspector = sa.inspect(conn)
    if not _index_exists(inspector, "chat_folder_members", "idx_folder_members_folder"):
        op.create_index("idx_folder_members_folder", "chat_folder_members", ["folder_id"])


def downgrade() -> None:
    """Remove topics, folders, and archived chat support."""

    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "chat_folder_members" in inspector.get_table_names():
        if _index_exists(inspector, "chat_folder_members", "idx_folder_members_folder"):
            op.drop_index("idx_folder_members_folder", table_name="chat_folder_members")
        if _index_exists(inspector, "chat_folder_members", "idx_folder_members_chat"):
            op.drop_index("idx_folder_members_chat", table_name="chat_folder_members")
        op.drop_table("chat_folder_members")
        inspector = sa.inspect(conn)

    if "chat_folders" in inspector.get_table_names():
        op.drop_table("chat_folders")
        inspector = sa.inspect(conn)

    if "forum_topics" in inspector.get_table_names():
        if _index_exists(inspector, "forum_topics", "idx_forum_topics_chat"):
            op.drop_index("idx_forum_topics_chat", table_name="forum_topics")
        op.drop_table("forum_topics")
        inspector = sa.inspect(conn)

    if "messages" in inspector.get_table_names():
        if _index_exists(inspector, "messages", "idx_messages_topic"):
            op.drop_index("idx_messages_topic", table_name="messages")
            inspector = sa.inspect(conn)
        if _column_exists(inspector, "messages", "reply_to_top_id"):
            op.drop_column("messages", "reply_to_top_id")
        inspector = sa.inspect(conn)

    if "chats" in inspector.get_table_names():
        if _column_exists(inspector, "chats", "is_archived"):
            op.drop_column("chats", "is_archived")
            inspector = sa.inspect(conn)
        if _column_exists(inspector, "chats", "is_forum"):
            op.drop_column("chats", "is_forum")
