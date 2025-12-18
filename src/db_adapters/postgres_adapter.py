"""
PostgreSQL adapter using SQLAlchemy

This adapter provides PostgreSQL database access through SQLAlchemy ORM.
It offers the same interface as the SQLite adapter but with PostgreSQL-specific
optimizations and features.
"""

import logging
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text as sql_text, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .adapter import DatabaseAdapter
from .models import (
    Base, Chat, Message, User, Media, Reaction, SyncStatus, Metadata
)

logger = logging.getLogger(__name__)


class PostgreSQLAdapter(DatabaseAdapter):
    """PostgreSQL database adapter using SQLAlchemy"""

    def __init__(self, host: str, port: int, database: str,
                 user: str, password: str, pool_size: int = 5):
        """
        Initialize PostgreSQL adapter.

        Args:
            host: PostgreSQL server host
            port: PostgreSQL server port
            database: Database name
            user: Database user
            password: Database password
            pool_size: Connection pool size
        """
        # Create PostgreSQL connection string
        db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"

        # Create engine with PostgreSQL-specific settings
        self.engine: Engine = create_engine(
            db_url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=10,
            pool_pre_ping=True,  # Validate connections before use
            pool_recycle=3600,   # Recycle connections after 1 hour
            echo=False,          # Set to True for SQL logging
            connect_args={
                "application_name": "telegram-archive",
                "connect_timeout": 10,
            }
        )

        # Session factory
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

        logger.info(f"PostgreSQL adapter initialized with {host}:{port}/{database}")

    def initialize_schema(self) -> None:
        """Create database tables if they don't exist"""
        logger.info("Initializing PostgreSQL database schema")
        Base.metadata.create_all(bind=self.engine)

        # Create additional indexes for better performance
        self._create_additional_indexes()

    def _create_additional_indexes(self) -> None:
        """Create additional indexes for better query performance"""
        with self.engine.connect() as conn:
            # Create indexes if they don't exist
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_message_date_desc ON messages (date DESC)",
                "CREATE INDEX IF NOT EXISTS idx_message_chat_sender ON messages (chat_id, sender_id)",
                "CREATE INDEX IF NOT EXISTS idx_media_file_size ON media (file_size)",
                "CREATE INDEX IF NOT EXISTS idx_sync_chat_sync ON sync_status (chat_id, last_sync_date DESC)",
            ]

            for index_sql in indexes:
                try:
                    conn.execute(text(index_sql))
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Failed to create index: {e}")
                    conn.rollback()

    def close(self) -> None:
        """Close database connection"""
        self.engine.dispose()
        logger.info("PostgreSQL adapter closed")

    def _get_session(self) -> Session:
        """Get a new database session"""
        return self.SessionLocal()

    # Chat operations
    def upsert_chat(self, chat_data: Dict[str, Any]) -> None:
        """Insert or update a chat record"""
        with self._get_session() as session:
            try:
                # Use ON CONFLICT for efficient upsert
                chat = session.query(Chat).filter_by(id=chat_data["id"]).first()

                if chat:
                    # Update existing chat
                    for key, value in chat_data.items():
                        if key != "id":
                            setattr(chat, key, value)
                    chat.updated_at = datetime.utcnow()
                else:
                    # Create new chat
                    chat = Chat(**chat_data)
                    session.add(chat)

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error upserting chat {chat_data.get('id')}: {e}")
                raise

    def get_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Get chat by ID"""
        with self._get_session() as session:
            chat = session.query(Chat).filter_by(id=chat_id).first()
            if chat:
                return {
                    "id": chat.id,
                    "type": chat.type,
                    "title": chat.title,
                    "username": chat.username,
                    "first_name": chat.first_name,
                    "last_name": chat.last_name,
                    "phone": chat.phone,
                    "description": chat.description,
                    "participants_count": chat.participants_count,
                    "last_synced_message_id": chat.last_synced_message_id,
                    "created_at": chat.created_at,
                    "updated_at": chat.updated_at
                }
            return None

    def get_all_chats(self, include_empty: bool = False, order_by: str = "last_message_date") -> List[Dict[str, Any]]:
        """Get all chats with optional filtering and ordering"""
        with self._get_session() as session:
            query = session.query(Chat)

            if not include_empty:
                # Use EXISTS to filter out empty chats
                query = query.filter(
                    session.query(Message.id)
                           .filter(Message.chat_id == Chat.id)
                           .exists()
                )

            # Apply ordering
            if order_by == "last_message_date":
                query = query.order_by(Chat.updated_at.desc())
            elif order_by == "title":
                query = query.order_by(func.lower(Chat.title).asc())

            chats = []
            for chat in query.all():
                chats.append({
                    "id": chat.id,
                    "type": chat.type,
                    "title": chat.title,
                    "username": chat.username,
                    "first_name": chat.first_name,
                    "last_name": chat.last_name,
                    "phone": chat.phone,
                    "description": chat.description,
                    "participants_count": chat.participants_count,
                    "last_synced_message_id": chat.last_synced_message_id,
                    "created_at": chat.created_at,
                    "updated_at": chat.updated_at
                })
            return chats

    def delete_chat(self, chat_id: int) -> bool:
        """Delete a chat and all related data"""
        with self._get_session() as session:
            try:
                # The cascade will handle related records
                deleted = session.query(Chat).filter_by(id=chat_id).delete()
                session.commit()
                return deleted > 0
            except Exception as e:
                session.rollback()
                logger.error(f"Error deleting chat {chat_id}: {e}")
                return False

    # Message operations
    def insert_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Insert multiple messages in batch"""
        if not messages:
            return

        with self._get_session() as session:
            try:
                # Create message objects
                message_objects = []
                for msg in messages:
                    # Convert dates to datetime if needed
                    if "date" in msg and isinstance(msg["date"], str):
                        msg["date"] = datetime.fromisoformat(msg["date"])
                    if "edit_date" in msg and isinstance(msg["edit_date"], str):
                        msg["edit_date"] = datetime.fromisoformat(msg["edit_date"])

                    # Convert raw_data dict to JSON string if present
                    if "raw_data" in msg and isinstance(msg["raw_data"], dict):
                        msg["raw_data"] = json.dumps(msg["raw_data"])

                    message_objects.append(Message(**msg))

                # Use bulk_insert_mappings for better performance
                session.bulk_save_objects(message_objects)
                session.commit()
                logger.debug(f"Inserted {len(messages)} messages")
            except Exception as e:
                session.rollback()
                logger.error(f"Error inserting messages: {e}")
                raise

    def get_messages(self, chat_id: int, limit: int = 100, offset: int = 0,
                    search_query: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get messages from a chat with pagination"""
        with self._get_session() as session:
            # Join with users to get sender info
            from sqlalchemy import and_

            query = session.query(Message, User).outerjoin(
                User, Message.sender_id == User.id
            ).filter(Message.chat_id == chat_id)

            if search_query:
                # Use ILIKE for case-insensitive search in PostgreSQL
                query = query.filter(Message.text.ilike(f"%{search_query}%"))

            query = query.order_by(Message.date.desc()).limit(limit).offset(offset)

            messages = []
            for msg, user in query.all():
                messages.append({
                    "id": msg.id,
                    "chat_id": msg.chat_id,
                    "sender_id": msg.sender_id,
                    "sender_name": user.first_name if user else None,
                    "sender_last_name": user.last_name if user else None,
                    "sender_username": user.username if user else None,
                    "date": msg.date,
                    "text": msg.text,
                    "reply_to_msg_id": msg.reply_to_msg_id,
                    "reply_to_text": msg.reply_to_text,
                    "forward_from_id": msg.forward_from_id,
                    "edit_date": msg.edit_date,
                    "media_type": msg.media_type,
                    "media_id": msg.media_id,
                    "media_path": msg.media_path,
                    "raw_data": msg.raw_data,
                    "created_at": msg.created_at,
                    "is_outgoing": msg.is_outgoing
                })
            return messages

    def get_message_count(self, chat_id: int, search_query: Optional[str] = None) -> int:
        """Get total message count for a chat"""
        with self._get_session() as session:
            query = session.query(Message).filter_by(chat_id=chat_id)

            if search_query:
                query = query.filter(Message.text.ilike(f"%{search_query}%"))

            return query.count()

    def get_last_synced_message_id(self, chat_id: int) -> int:
        """Get ID of the last synced message for a chat"""
        with self._get_session() as session:
            chat = session.query(Chat).filter_by(id=chat_id).first()
            return chat.last_synced_message_id if chat else 0

    # User operations
    def upsert_user(self, user_data: Dict[str, Any]) -> None:
        """Insert or update a user record"""
        with self._get_session() as session:
            try:
                user = session.query(User).filter_by(id=user_data["id"]).first()

                if user:
                    for key, value in user_data.items():
                        if key != "id":
                            setattr(user, key, value)
                    user.updated_at = datetime.utcnow()
                else:
                    user = User(**user_data)
                    session.add(user)

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error upserting user {user_data.get('id')}: {e}")
                raise

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by ID"""
        with self._get_session() as session:
            user = session.query(User).filter_by(id=user_id).first()
            if user:
                return {
                    "id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "phone": user.phone,
                    "is_bot": user.is_bot,
                    "created_at": user.created_at,
                    "updated_at": user.updated_at
                }
            return None

    # Media operations
    def upsert_media(self, media_data: Dict[str, Any]) -> None:
        """Insert or update a media record"""
        with self._get_session() as session:
            try:
                media = session.query(Media).filter_by(id=media_data["id"]).first()

                if media:
                    for key, value in media_data.items():
                        if key != "id":
                            setattr(media, key, value)
                else:
                    media = Media(**media_data)
                    session.add(media)

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error upserting media {media_data.get('id')}: {e}")
                raise

    def get_media_by_chat(self, chat_id: int, media_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get media files for a chat"""
        with self._get_session() as session:
            query = session.query(Media).filter_by(chat_id=chat_id)

            if media_type:
                query = query.filter_by(type=media_type)

            media_list = []
            for media in query.all():
                media_list.append({
                    "id": media.id,
                    "message_id": media.message_id,
                    "chat_id": media.chat_id,
                    "type": media.type,
                    "file_path": media.file_path,
                    "file_name": media.file_name,
                    "file_size": media.file_size,
                    "mime_type": media.mime_type,
                    "width": media.width,
                    "height": media.height,
                    "duration": media.duration,
                    "downloaded": media.downloaded,
                    "download_date": media.download_date,
                    "created_at": media.created_at
                })
            return media_list

    def get_media_stats(self) -> Dict[str, Any]:
        """Get media statistics"""
        with self._get_session() as session:
            total_count = session.query(Media).count()
            total_size = session.query(func.sum(Media.file_size)).scalar() or 0

            # Stats by type
            type_stats = session.query(Media.type, func.count(Media.id)) \
                              .group_by(Media.type) \
                              .all()

            return {
                "total_count": total_count,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "by_type": dict(type_stats)
            }

    # Reaction operations
    def insert_reactions(self, reactions: List[Dict[str, Any]]) -> None:
        """Insert message reactions"""
        if not reactions:
            return

        with self._get_session() as session:
            try:
                reaction_objects = [Reaction(**r) for r in reactions]
                session.bulk_save_objects(reaction_objects)
                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error inserting reactions: {e}")
                raise

    # Sync status operations
    def update_sync_status(self, chat_id: int, last_message_id: int,
                          message_count: int) -> None:
        """Update synchronization status for a chat"""
        with self._get_session() as session:
            try:
                sync = session.query(SyncStatus).filter_by(chat_id=chat_id).first()

                if sync:
                    sync.last_message_id = last_message_id
                    sync.last_sync_date = datetime.utcnow()
                    sync.message_count = message_count
                else:
                    sync = SyncStatus(
                        chat_id=chat_id,
                        last_message_id=last_message_id,
                        message_count=message_count
                    )
                    session.add(sync)

                # Also update chat's last_synced_message_id
                chat = session.query(Chat).filter_by(id=chat_id).first()
                if chat:
                    chat.last_synced_message_id = last_message_id
                    chat.updated_at = datetime.utcnow()

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error updating sync status: {e}")
                raise

    # Metadata operations
    def get_metadata(self, key: str) -> Optional[str]:
        """Get metadata value by key"""
        with self._get_session() as session:
            metadata = session.query(Metadata).filter_by(key=key).first()
            return metadata.value if metadata else None

    def set_metadata(self, key: str, value: str) -> None:
        """Set metadata value"""
        with self._get_session() as session:
            try:
                metadata = session.query(Metadata).filter_by(key=key).first()

                if metadata:
                    metadata.value = value
                else:
                    metadata = Metadata(key=key, value=value)
                    session.add(metadata)

                session.commit()
            except Exception as e:
                session.rollback()
                logger.error(f"Error setting metadata: {e}")
                raise

    # Statistics
    def get_stats(self) -> Dict[str, Any]:
        """Get overall database statistics"""
        with self._get_session() as session:
            chat_count = session.query(Chat).count()
            message_count = session.query(Message).count()
            user_count = session.query(User).count()
            media_count = session.query(Media).count()
            media_size = session.query(func.sum(Media.file_size)).scalar() or 0

            return {
                "chats": chat_count,
                "messages": message_count,
                "users": user_count,
                "media_files": media_count,
                "total_size_mb": round(media_size / (1024 * 1024), 2)
            }

    # Export
    def export_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Export all data for a chat"""
        chat = self.get_chat(chat_id)
        if not chat:
            return None

        # PostgreSQL can handle larger batches
        messages = self.get_messages(chat_id, limit=50000)
        media = self.get_media_by_chat(chat_id)

        return {
            "chat": chat,
            "messages": messages,
            "media": media,
            "exported_at": datetime.utcnow().isoformat()
        }