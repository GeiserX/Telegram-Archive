"""
SQLAlchemy ORM models for Telegram Backup.

These models match the existing v2.x schema exactly to ensure
backward compatibility with existing SQLite databases.
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    BigInteger, Integer, String, Text, DateTime, Boolean, 
    ForeignKey, Index, UniqueConstraint, event, func
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


class Message(Base):
    """Messages table - all messages from all chats."""
    __tablename__ = 'messages'
    
    # Composite primary key (id, chat_id) - message IDs are only unique within a chat
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('chats.id'), primary_key=True)
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    text: Mapped[Optional[str]] = mapped_column(Text)
    reply_to_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    reply_to_text: Mapped[Optional[str]] = mapped_column(Text)
    forward_from_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    edit_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    media_type: Mapped[Optional[str]] = mapped_column(String(50))
    media_id: Mapped[Optional[str]] = mapped_column(String(255))
    media_path: Mapped[Optional[str]] = mapped_column(String(500))
    raw_data: Mapped[Optional[str]] = mapped_column(Text)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    is_outgoing: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="messages")
    reactions: Mapped[List["Reaction"]] = relationship("Reaction", back_populates="message", lazy="dynamic")
    
    __table_args__ = (
        Index('idx_messages_chat_id', 'chat_id'),
        Index('idx_messages_date', 'date'),
        Index('idx_messages_sender_id', 'sender_id'),
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


class Media(Base):
    """Media table - downloaded media files."""
    __tablename__ = 'media'
    
    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # Telegram file_id
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    type: Mapped[Optional[str]] = mapped_column(String(50))
    file_path: Mapped[Optional[str]] = mapped_column(String(500))
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100))
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    duration: Mapped[Optional[int]] = mapped_column(Integer)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)  # 0 or 1
    download_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    
    __table_args__ = (
        Index('idx_media_message', 'message_id', 'chat_id'),
    )


class Reaction(Base):
    """Reactions table - message reactions."""
    __tablename__ = 'reactions'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    
    # Relationship to message (composite foreign key)
    message: Mapped["Message"] = relationship(
        "Message",
        back_populates="reactions",
        foreign_keys=[message_id, chat_id],
        primaryjoin="and_(Reaction.message_id==Message.id, Reaction.chat_id==Message.chat_id)"
    )
    
    __table_args__ = (
        UniqueConstraint('message_id', 'chat_id', 'emoji', 'user_id', name='uq_reaction'),
        Index('idx_reactions_message', 'message_id', 'chat_id'),
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
