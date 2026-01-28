"""
SQLAlchemy ORM models for Telegram Backup.

v6.0.0 - Normalized schema with proper foreign key constraints.
Media data is now stored only in the media table, not duplicated in messages.
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    BigInteger, Integer, String, Text, DateTime, Boolean, 
    ForeignKey, ForeignKeyConstraint, Index, UniqueConstraint, event, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class Chat(Base):
    """Chats table - users, groups, channels."""
    __tablename__ = 'chats'
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    description: Mapped[Optional[str]] = mapped_column(Text)
    participants_count: Mapped[Optional[int]] = mapped_column(Integer)
    last_synced_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now())
    
    # Relationships
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="chat", lazy="dynamic")
    sync_status: Mapped[Optional["SyncStatus"]] = relationship("SyncStatus", back_populates="chat", uselist=False)
    
    __table_args__ = (
        Index('idx_chats_username', 'username'),
    )


class Message(Base):
    """Messages table - all messages from all chats.
    
    v6.0.0: media_type, media_id, media_path removed - use media_items relationship instead.
    """
    __tablename__ = 'messages'
    
    # Composite primary key (id, chat_id) - message IDs are only unique within a chat
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('chats.id'), primary_key=True)
    # NOTE: sender_id has no FK constraint because it can be channel/group IDs (not in users table)
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text)
    reply_to_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    reply_to_text: Mapped[Optional[str]] = mapped_column(Text)
    forward_from_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    edit_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # v6.0.0: media_type, media_id, media_path REMOVED - normalized to media table
    raw_data: Mapped[Optional[str]] = mapped_column(Text)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    is_outgoing: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    is_pinned: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1 - whether this message is pinned
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="messages")
    # NOTE: sender relationship works via ORM join, no DB-level FK (sender_id can be channel/group IDs)
    sender: Mapped[Optional["User"]] = relationship(
        "User", 
        back_populates="messages",
        primaryjoin="Message.sender_id == User.id",
        foreign_keys="[Message.sender_id]"
    )
    reactions: Mapped[List["Reaction"]] = relationship("Reaction", back_populates="message", lazy="dynamic")
    media_items: Mapped[List["Media"]] = relationship("Media", back_populates="message", lazy="selectin")
    
    __table_args__ = (
        Index('idx_messages_chat_id', 'chat_id'),
        Index('idx_messages_date', 'date'),
        Index('idx_messages_sender_id', 'sender_id'),
        # Composite index for fast pagination: WHERE chat_id = ? ORDER BY date DESC
        Index('idx_messages_chat_date_desc', 'chat_id', date.desc()),
        # Index for finding pinned messages in a chat
        Index('idx_messages_chat_pinned', 'chat_id', 'is_pinned'),
        # Index for reply lookups
        Index('idx_messages_reply_to', 'chat_id', 'reply_to_msg_id'),
    )


class User(Base):
    """Users table - message senders."""
    __tablename__ = 'users'
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    is_bot: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now())
    
    # Relationships
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="sender", lazy="dynamic")
    
    __table_args__ = (
        Index('idx_users_username', 'username'),
    )


class Media(Base):
    """Media table - downloaded media files.
    
    v6.0.0: Now the single source of truth for media metadata.
    Foreign key constraint to messages table added.
    """
    __tablename__ = 'media'
    
    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Telegram file_id
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    type: Mapped[Optional[str]] = mapped_column(String(50))
    file_path: Mapped[Optional[str]] = mapped_column(Text)  # v6.0.0: Changed to Text for long paths
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    download_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    
    # Relationship to message
    message: Mapped[Optional["Message"]] = relationship(
        "Message",
        back_populates="media_items",
        primaryjoin="and_(Media.message_id==Message.id, Media.chat_id==Message.chat_id)",
        foreign_keys="[Media.message_id, Media.chat_id]",
    )
    
    __table_args__ = (
        ForeignKeyConstraint(
            ['message_id', 'chat_id'],
            ['messages.id', 'messages.chat_id'],
            name='fk_media_message',
            ondelete='CASCADE'
        ),
        Index('idx_media_message', 'message_id', 'chat_id'),
        Index('idx_media_downloaded', 'chat_id', 'downloaded'),
        Index('idx_media_type', 'type'),
    )


class Reaction(Base):
    """Reactions table - message reactions."""
    __tablename__ = 'reactions'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey('users.id', ondelete='SET NULL'))
    count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    
    # Relationship to message (composite foreign key)
    message: Mapped["Message"] = relationship(
        "Message",
        back_populates="reactions",
        primaryjoin="and_(Reaction.message_id==Message.id, Reaction.chat_id==Message.chat_id)",
        foreign_keys="[Reaction.message_id, Reaction.chat_id]",
    )
    
    __table_args__ = (
        ForeignKeyConstraint(
            ['message_id', 'chat_id'],
            ['messages.id', 'messages.chat_id'],
            name='fk_reaction_message'
        ),
        UniqueConstraint('message_id', 'chat_id', 'emoji', 'user_id', name='uq_reaction'),
        Index('idx_reactions_message', 'message_id', 'chat_id'),
        Index('idx_reactions_user', 'user_id'),
    )


class SyncStatus(Base):
    """Sync status table - tracks backup progress per chat."""
    __tablename__ = 'sync_status'
    
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('chats.id'), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    
    # Relationship
    chat: Mapped["Chat"] = relationship("Chat", back_populates="sync_status")


class Metadata(Base):
    """Metadata table - key-value store for app settings."""
    __tablename__ = 'metadata'
    
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


class PushSubscription(Base):
    """Push notification subscriptions for Web Push API."""
    __tablename__ = 'push_subscriptions'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # Push service URL
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)  # Public key
    auth: Mapped[str] = mapped_column(String(255), nullable=False)  # Auth secret
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)  # Optional: subscribe to specific chat only
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))  # Browser info for debugging
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # Track activity
    
    __table_args__ = (
        Index('idx_push_sub_chat', 'chat_id'),
    )
