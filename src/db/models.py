"""
SQLAlchemy ORM models for Telegram Backup.

v6.0.0 - Normalized schema with proper foreign key constraints.
Media data is now stored only in the media table, not duplicated in messages.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Chat(Base):
    """Chats table - users, groups, channels."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    participants_count: Mapped[int | None] = mapped_column(Integer)
    is_forum: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v6.2.0: forum with topics
    is_archived: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # v6.2.0: archived chat
    last_synced_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now()
    )

    # Relationships
    messages: Mapped[list[Message]] = relationship("Message", back_populates="chat", lazy="dynamic")
    sync_status: Mapped[SyncStatus | None] = relationship("SyncStatus", back_populates="chat", uselist=False)
    forum_topics: Mapped[list[ForumTopic]] = relationship("ForumTopic", back_populates="chat", lazy="dynamic")

    __table_args__ = (Index("idx_chats_username", "username"),)


class Message(Base):
    """Messages table - all messages from all chats.

    v6.0.0: media_type, media_id, media_path removed - use media_items relationship instead.
    """

    __tablename__ = "messages"

    # Composite primary key (id, chat_id) - message IDs are only unique within a chat
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"), primary_key=True)
    # NOTE: sender_id has no FK constraint because it can be channel/group IDs (not in users table)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_to_top_id: Mapped[int | None] = mapped_column(BigInteger)  # v6.2.0: forum topic thread ID
    reply_to_text: Mapped[str | None] = mapped_column(Text)
    forward_from_id: Mapped[int | None] = mapped_column(BigInteger)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime)
    # v6.0.0: media_type, media_id, media_path REMOVED - normalized to media table
    raw_data: Mapped[str | None] = mapped_column(Text)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    is_outgoing: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    is_pinned: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1 - whether this message is pinned

    # Relationships
    chat: Mapped[Chat] = relationship("Chat", back_populates="messages")
    # NOTE: sender relationship works via ORM join, no DB-level FK (sender_id can be channel/group IDs)
    sender: Mapped[User | None] = relationship(
        "User",
        back_populates="messages",
        primaryjoin="Message.sender_id == User.id",
        foreign_keys="[Message.sender_id]",
    )
    reactions: Mapped[list[Reaction]] = relationship("Reaction", back_populates="message", lazy="dynamic")
    media_items: Mapped[list[Media]] = relationship("Media", back_populates="message", lazy="selectin")

    __table_args__ = (
        Index("idx_messages_chat_id", "chat_id"),
        Index("idx_messages_date", "date"),
        Index("idx_messages_sender_id", "sender_id"),
        # Composite index for fast pagination: WHERE chat_id = ? ORDER BY date DESC
        Index("idx_messages_chat_date_desc", "chat_id", date.desc()),
        # Index for finding pinned messages in a chat
        Index("idx_messages_chat_pinned", "chat_id", "is_pinned"),
        # Index for reply lookups
        Index("idx_messages_reply_to", "chat_id", "reply_to_msg_id"),
        # v6.2.0: Index for topic message lookups in forum chats
        Index("idx_messages_topic", "chat_id", "reply_to_top_id"),
    )


class User(Base):
    """Users table - message senders."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    is_bot: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now()
    )

    # Relationships
    # NOTE: Explicit join because sender_id has no DB-level FK (can contain channel/group IDs)
    messages: Mapped[list[Message]] = relationship(
        "Message",
        back_populates="sender",
        primaryjoin="User.id == Message.sender_id",
        foreign_keys="[Message.sender_id]",
        lazy="dynamic",
    )

    __table_args__ = (Index("idx_users_username", "username"),)


class Media(Base):
    """Media table - downloaded media files.

    v6.0.0: Now the single source of truth for media metadata.
    Foreign key constraint to messages table added.
    """

    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Telegram file_id
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str | None] = mapped_column(String(50))
    file_path: Mapped[str | None] = mapped_column(Text)  # v6.0.0: Changed to Text for long paths
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    download_date: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    # Relationship to message
    message: Mapped[Message | None] = relationship(
        "Message",
        back_populates="media_items",
        primaryjoin="and_(Media.message_id==Message.id, Media.chat_id==Message.chat_id)",
        foreign_keys="[Media.message_id, Media.chat_id]",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "chat_id"], ["messages.id", "messages.chat_id"], name="fk_media_message", ondelete="CASCADE"
        ),
        Index("idx_media_message", "message_id", "chat_id"),
        Index("idx_media_downloaded", "chat_id", "downloaded"),
        Index("idx_media_type", "type"),
    )


class Reaction(Base):
    """Reactions table - message reactions."""

    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    # Relationship to message (composite foreign key)
    message: Mapped[Message] = relationship(
        "Message",
        back_populates="reactions",
        primaryjoin="and_(Reaction.message_id==Message.id, Reaction.chat_id==Message.chat_id)",
        foreign_keys="[Reaction.message_id, Reaction.chat_id]",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "chat_id"], ["messages.id", "messages.chat_id"], name="fk_reaction_message"
        ),
        UniqueConstraint("message_id", "chat_id", "emoji", "user_id", name="uq_reaction"),
        Index("idx_reactions_message", "message_id", "chat_id"),
        Index("idx_reactions_user", "user_id"),
    )


class SyncStatus(Base):
    """Sync status table - tracks backup progress per chat."""

    __tablename__ = "sync_status"

    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship
    chat: Mapped[Chat] = relationship("Chat", back_populates="sync_status")


class Metadata(Base):
    """Metadata table - key-value store for app settings."""

    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class PushSubscription(Base):
    """Push notification subscriptions for Web Push API."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # Push service URL
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)  # Public key
    auth: Mapped[str] = mapped_column(String(255), nullable=False)  # Auth secret
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # Optional: subscribe to specific chat only
    username: Mapped[str | None] = mapped_column(String(255))  # User who created this subscription
    allowed_chat_ids: Mapped[str | None] = mapped_column(Text)  # JSON snapshot of user's allowed chats at subscribe time
    user_agent: Mapped[str | None] = mapped_column(String(500))  # Browser info for debugging
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)  # Track activity

    __table_args__ = (Index("idx_push_sub_chat", "chat_id"), Index("idx_push_sub_username", "username"))


class ForumTopic(Base):
    """Forum topics table - topics within forum-enabled chats.

    v6.2.0: Stores topic metadata for forum groups/channels.
    """

    __tablename__ = "forum_topics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    icon_color: Mapped[int | None] = mapped_column(Integer)
    icon_emoji_id: Mapped[int | None] = mapped_column(BigInteger)
    icon_emoji: Mapped[str | None] = mapped_column(String(32))  # Unicode emoji resolved from icon_emoji_id
    is_closed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_pinned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_hidden: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    date: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    # Relationships
    chat: Mapped[Chat] = relationship("Chat", back_populates="forum_topics")

    __table_args__ = (Index("idx_forum_topics_chat", "chat_id"),)


class ChatFolder(Base):
    """Chat folders table - user-created Telegram folders.

    v6.2.0: Stores folder metadata from Telegram dialog filters.
    """

    __tablename__ = "chat_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    emoticon: Mapped[str | None] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    # Relationships
    members: Mapped[list[ChatFolderMember]] = relationship(
        "ChatFolderMember", back_populates="folder", cascade="all, delete-orphan"
    )


class ChatFolderMember(Base):
    """Chat folder members table - maps chats to folders.

    v6.2.0: Junction table for many-to-many relationship between folders and chats.
    """

    __tablename__ = "chat_folder_members"

    folder_id: Mapped[int] = mapped_column(Integer, ForeignKey("chat_folders.id", ondelete="CASCADE"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)

    # Relationships
    folder: Mapped[ChatFolder] = relationship("ChatFolder", back_populates="members")
    chat: Mapped[Chat] = relationship("Chat")

    __table_args__ = (
        Index("idx_folder_members_chat", "chat_id"),
        Index("idx_folder_members_folder", "folder_id"),
    )


class ViewerAccount(Base):
    """Viewer accounts for multi-user web viewer access control.

    v7.0.0: DB-backed viewer accounts with per-user chat whitelists.
    Master account uses env vars; these are additional restricted viewers.
    """

    __tablename__ = "viewer_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_chat_ids: Mapped[str | None] = mapped_column(Text)  # JSON array of chat IDs, NULL = all
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now()
    )


class ViewerAuditLog(Base):
    """Audit log for viewer access and admin operations.

    v7.0.0: Tracks login attempts, admin CRUD, and API access.
    Username is denormalized so audit records survive viewer deletion.
    """

    __tablename__ = "viewer_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "master" or "viewer"
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(255))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
