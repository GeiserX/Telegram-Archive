"""
Async database adapter for Telegram Backup.

Provides all database operations using SQLAlchemy async.
This is a drop-in replacement for the old Database class.
"""

import json
import os
import shutil
import glob
import logging
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from functools import wraps

from sqlalchemy import select, update, delete, func, text, and_, or_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Chat, Message, User, Media, Reaction, SyncStatus, Metadata
from .base import DatabaseManager

logger = logging.getLogger(__name__)


def _strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip timezone info from datetime for PostgreSQL compatibility."""
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def retry_on_locked(max_retries: int = 5, initial_delay: float = 0.1, max_delay: float = 2.0, backoff_factor: float = 2.0):
    """
    Decorator to retry async database operations on operational errors.
    
    Works for both SQLite (database locked) and PostgreSQL (connection issues).
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    if 'locked' not in error_str and 'connection' not in error_str:
                        raise
                    
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"Database error on {func.__name__}, attempt {attempt + 1}/{max_retries + 1}. "
                            f"Retrying in {delay:.2f}s... Error: {e}"
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.error(
                            f"Database error on {func.__name__} after {max_retries + 1} attempts. Giving up."
                        )
                        raise
            
            if last_exception:
                raise last_exception
                
        return wrapper
    return decorator


class DatabaseAdapter:
    """
    Async database adapter compatible with the old Database class interface.
    
    All methods are async and should be awaited.
    """
    
    def __init__(self, db_manager: DatabaseManager):
        """
        Initialize adapter with a DatabaseManager.
        
        Args:
            db_manager: Initialized DatabaseManager instance
        """
        self.db_manager = db_manager
        self._is_sqlite = db_manager._is_sqlite
    
    def _serialize_raw_data(self, raw_data: Any) -> str:
        """
        Safely serialize raw_data to JSON.
        
        Args:
            raw_data: Data to serialize
            
        Returns:
            JSON string representation
        """
        if not raw_data:
            return '{}'
        
        try:
            return json.dumps(raw_data)
        except (TypeError, ValueError) as e:
            logger.warning(f"Failed to serialize raw_data directly: {e}")
            try:
                def convert_to_serializable(obj):
                    if isinstance(obj, dict):
                        return {k: convert_to_serializable(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [convert_to_serializable(item) for item in obj]
                    elif isinstance(obj, (str, int, float, bool, type(None))):
                        return obj
                    else:
                        return str(obj)
                
                serializable_data = convert_to_serializable(raw_data)
                return json.dumps(serializable_data)
            except Exception as e2:
                logger.error(f"Failed to serialize raw_data even after conversion: {e2}")
                return '{}'
    
    # ========== Metadata Operations ==========
    
    async def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        async with self.db_manager.async_session_factory() as session:
            # Use upsert
            if self._is_sqlite:
                stmt = sqlite_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['key'],
                    set_={'value': value}
                )
            else:
                stmt = pg_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['key'],
                    set_={'value': value}
                )
            await session.execute(stmt)
            await session.commit()
    
    async def get_metadata(self, key: str) -> Optional[str]:
        """Get a metadata value by key."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Metadata.value).where(Metadata.key == key)
            )
            row = result.scalar_one_or_none()
            return row
    
    # ========== Chat Operations ==========
    
    @retry_on_locked()
    async def upsert_chat(self, chat_data: Dict[str, Any]) -> int:
        """Insert or update a chat record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                'id': chat_data['id'],
                'type': chat_data.get('type', 'unknown'),
                'title': chat_data.get('title'),
                'username': chat_data.get('username'),
                'first_name': chat_data.get('first_name'),
                'last_name': chat_data.get('last_name'),
                'phone': chat_data.get('phone'),
                'description': chat_data.get('description'),
                'participants_count': chat_data.get('participants_count'),
                'updated_at': datetime.utcnow(),
            }
            
            if self._is_sqlite:
                stmt = sqlite_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_={
                        'type': stmt.excluded.type,
                        'title': stmt.excluded.title,
                        'username': stmt.excluded.username,
                        'first_name': stmt.excluded.first_name,
                        'last_name': stmt.excluded.last_name,
                        'phone': stmt.excluded.phone,
                        'description': stmt.excluded.description,
                        'participants_count': stmt.excluded.participants_count,
                        'updated_at': datetime.utcnow(),
                    }
                )
            else:
                stmt = pg_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_={
                        'type': stmt.excluded.type,
                        'title': stmt.excluded.title,
                        'username': stmt.excluded.username,
                        'first_name': stmt.excluded.first_name,
                        'last_name': stmt.excluded.last_name,
                        'phone': stmt.excluded.phone,
                        'description': stmt.excluded.description,
                        'participants_count': stmt.excluded.participants_count,
                        'updated_at': datetime.utcnow(),
                    }
                )
            
            await session.execute(stmt)
            await session.commit()
            return chat_data['id']
    
    async def get_all_chats(self) -> List[Dict[str, Any]]:
        """Get all chats with their last message date."""
        async with self.db_manager.async_session_factory() as session:
            # Subquery for last message date
            subq = (
                select(Message.chat_id, func.max(Message.date).label('last_message_date'))
                .group_by(Message.chat_id)
                .subquery()
            )
            
            stmt = (
                select(Chat, subq.c.last_message_date)
                .outerjoin(subq, Chat.id == subq.c.chat_id)
                .order_by(
                    subq.c.last_message_date.is_(None),
                    subq.c.last_message_date.desc()
                )
            )
            
            result = await session.execute(stmt)
            chats = []
            for row in result:
                chat_dict = {
                    'id': row.Chat.id,
                    'type': row.Chat.type,
                    'title': row.Chat.title,
                    'username': row.Chat.username,
                    'first_name': row.Chat.first_name,
                    'last_name': row.Chat.last_name,
                    'phone': row.Chat.phone,
                    'description': row.Chat.description,
                    'participants_count': row.Chat.participants_count,
                    'last_synced_message_id': row.Chat.last_synced_message_id,
                    'created_at': row.Chat.created_at,
                    'updated_at': row.Chat.updated_at,
                    'last_message_date': row.last_message_date,
                }
                chats.append(chat_dict)
            return chats
    
    # ========== User Operations ==========
    
    async def upsert_user(self, user_data: Dict[str, Any]) -> None:
        """Insert or update a user record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                'id': user_data['id'],
                'username': user_data.get('username'),
                'first_name': user_data.get('first_name'),
                'last_name': user_data.get('last_name'),
                'phone': user_data.get('phone'),
                'is_bot': 1 if user_data.get('is_bot') else 0,
                'updated_at': datetime.utcnow(),
            }
            
            if self._is_sqlite:
                stmt = sqlite_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_={
                        'username': stmt.excluded.username,
                        'first_name': stmt.excluded.first_name,
                        'last_name': stmt.excluded.last_name,
                        'phone': stmt.excluded.phone,
                        'is_bot': stmt.excluded.is_bot,
                        'updated_at': datetime.utcnow(),
                    }
                )
            else:
                stmt = pg_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_={
                        'username': stmt.excluded.username,
                        'first_name': stmt.excluded.first_name,
                        'last_name': stmt.excluded.last_name,
                        'phone': stmt.excluded.phone,
                        'is_bot': stmt.excluded.is_bot,
                        'updated_at': datetime.utcnow(),
                    }
                )
            
            await session.execute(stmt)
            await session.commit()
    
    # ========== Message Operations ==========
    
    async def insert_message(self, message_data: Dict[str, Any]) -> None:
        """Insert a message record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                'id': message_data['id'],
                'chat_id': message_data['chat_id'],
                'sender_id': message_data.get('sender_id'),
                'date': _strip_tz(message_data['date']),
                'text': message_data.get('text'),
                'reply_to_msg_id': message_data.get('reply_to_msg_id'),
                'reply_to_text': message_data.get('reply_to_text'),
                'forward_from_id': message_data.get('forward_from_id'),
                'edit_date': _strip_tz(message_data.get('edit_date')),
                'media_type': message_data.get('media_type'),
                'media_id': message_data.get('media_id'),
                'media_path': message_data.get('media_path'),
                'raw_data': self._serialize_raw_data(message_data.get('raw_data', {})),
                'is_outgoing': message_data.get('is_outgoing', 0),
            }
            
            if self._is_sqlite:
                stmt = sqlite_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id', 'chat_id'],
                    set_=values
                )
            else:
                stmt = pg_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id', 'chat_id'],
                    set_=values
                )
            
            await session.execute(stmt)
            await session.commit()
    
    @retry_on_locked()
    async def insert_messages_batch(self, messages_data: List[Dict[str, Any]]) -> None:
        """Insert multiple message records in a single transaction."""
        if not messages_data:
            return
        
        async with self.db_manager.async_session_factory() as session:
            for m in messages_data:
                values = {
                    'id': m['id'],
                    'chat_id': m['chat_id'],
                    'sender_id': m.get('sender_id'),
                    'date': _strip_tz(m['date']),
                    'text': m.get('text'),
                    'reply_to_msg_id': m.get('reply_to_msg_id'),
                    'reply_to_text': m.get('reply_to_text'),
                    'forward_from_id': m.get('forward_from_id'),
                    'edit_date': _strip_tz(m.get('edit_date')),
                    'media_type': m.get('media_type'),
                    'media_id': m.get('media_id'),
                    'media_path': m.get('media_path'),
                    'raw_data': self._serialize_raw_data(m.get('raw_data', {})),
                    'is_outgoing': m.get('is_outgoing', 0),
                }
                
                if self._is_sqlite:
                    stmt = sqlite_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['id', 'chat_id'],
                        set_=values
                    )
                else:
                    stmt = pg_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=['id', 'chat_id'],
                        set_=values
                    )
                
                await session.execute(stmt)
            
            await session.commit()
    
    async def get_messages_by_date_range(
        self,
        chat_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get messages within a date range."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message)
            
            conditions = []
            if chat_id:
                conditions.append(Message.chat_id == chat_id)
            if start_date:
                conditions.append(Message.date >= start_date)
            if end_date:
                conditions.append(Message.date <= end_date)
            
            if conditions:
                stmt = stmt.where(and_(*conditions))
            
            stmt = stmt.order_by(Message.date.asc())
            
            result = await session.execute(stmt)
            return [self._message_to_dict(m) for m in result.scalars()]
    
    async def find_message_by_date(self, chat_id: int, target_date: datetime) -> Optional[Dict[str, Any]]:
        """Find the first message on or after a specific date."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message)
                .where(and_(Message.chat_id == chat_id, Message.date >= target_date))
                .order_by(Message.date.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            message = result.scalar_one_or_none()
            return self._message_to_dict(message) if message else None
    
    async def get_messages_sync_data(self, chat_id: int) -> Dict[int, Optional[str]]:
        """Get message IDs and their edit dates for sync checking."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.id, Message.edit_date).where(Message.chat_id == chat_id)
            result = await session.execute(stmt)
            return {row.id: row.edit_date for row in result}
    
    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a specific message and its media."""
        async with self.db_manager.async_session_factory() as session:
            # Delete associated media
            await session.execute(
                delete(Media).where(
                    and_(Media.chat_id == chat_id, Media.message_id == message_id)
                )
            )
            # Delete reactions
            await session.execute(
                delete(Reaction).where(
                    and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id)
                )
            )
            # Delete the message
            await session.execute(
                delete(Message).where(
                    and_(Message.chat_id == chat_id, Message.id == message_id)
                )
            )
            await session.commit()
            logger.debug(f"Deleted message {message_id} from chat {chat_id}")
    
    async def delete_message_by_id_any_chat(self, message_id: int) -> bool:
        """
        Delete a message by ID when chat is unknown.
        
        This is used by the real-time listener when Telegram sends deletion
        events without specifying the chat (can happen in some edge cases).
        
        Args:
            message_id: The message ID to delete
            
        Returns:
            True if a message was deleted, False otherwise
        """
        async with self.db_manager.async_session_factory() as session:
            # First, find which chat(s) have this message
            result = await session.execute(
                select(Message.chat_id).where(Message.id == message_id)
            )
            chat_ids = [row[0] for row in result.fetchall()]
            
            if not chat_ids:
                return False
            
            # Delete from all matching chats (usually just one)
            for chat_id in chat_ids:
                # Delete associated media
                await session.execute(
                    delete(Media).where(
                        and_(Media.chat_id == chat_id, Media.message_id == message_id)
                    )
                )
                # Delete reactions
                await session.execute(
                    delete(Reaction).where(
                        and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id)
                    )
                )
                # Delete the message
                await session.execute(
                    delete(Message).where(
                        and_(Message.chat_id == chat_id, Message.id == message_id)
                    )
                )
            
            await session.commit()
            logger.debug(f"Deleted message {message_id} from {len(chat_ids)} chat(s)")
            return True
    
    async def update_message_text(self, chat_id: int, message_id: int, new_text: str, edit_date: Optional[datetime]) -> None:
        """Update a message's text and edit date."""
        async with self.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=_strip_tz(edit_date))
            )
            await session.commit()
            logger.debug(f"Updated message {message_id} in chat {chat_id}")
    
    async def backfill_is_outgoing(self, owner_id: int) -> None:
        """Backfill is_outgoing flag for messages sent by the owner."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(and_(
                    Message.sender_id == owner_id,
                    or_(Message.is_outgoing == 0, Message.is_outgoing.is_(None))
                ))
                .values(is_outgoing=1)
            )
            await session.commit()
            if result.rowcount > 0:
                logger.info(f"Backfilled is_outgoing=1 for {result.rowcount} messages from owner {owner_id}")
    
    def _message_to_dict(self, message: Message) -> Dict[str, Any]:
        """Convert Message model to dictionary."""
        return {
            'id': message.id,
            'chat_id': message.chat_id,
            'sender_id': message.sender_id,
            'date': message.date,
            'text': message.text,
            'reply_to_msg_id': message.reply_to_msg_id,
            'reply_to_text': message.reply_to_text,
            'forward_from_id': message.forward_from_id,
            'edit_date': message.edit_date,
            'media_type': message.media_type,
            'media_id': message.media_id,
            'media_path': message.media_path,
            'raw_data': message.raw_data,
            'created_at': message.created_at,
            'is_outgoing': message.is_outgoing,
        }
    
    # ========== Media Operations ==========
    
    async def insert_media(self, media_data: Dict[str, Any]) -> None:
        """Insert a media file record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                'id': media_data['id'],
                'message_id': media_data.get('message_id'),
                'chat_id': media_data.get('chat_id'),
                'type': media_data['type'],
                'file_name': media_data.get('file_name'),
                'file_path': media_data.get('file_path'),
                'file_size': media_data.get('file_size'),
                'mime_type': media_data.get('mime_type'),
                'width': media_data.get('width'),
                'height': media_data.get('height'),
                'duration': media_data.get('duration'),
                'downloaded': 1 if media_data.get('downloaded') else 0,
                'download_date': media_data.get('download_date'),
            }
            
            if self._is_sqlite:
                stmt = sqlite_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_=values
                )
            else:
                stmt = pg_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_=values
                )
            
            await session.execute(stmt)
            await session.commit()
    
    async def get_media_for_verification(self) -> List[Dict[str, Any]]:
        """
        Get all media records that should have files on disk.
        Used by VERIFY_MEDIA to check for missing/corrupted files.
        
        Returns media where downloaded=1 OR file_path is not null.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media)
                .where(
                    or_(
                        Media.downloaded == 1,
                        Media.file_path.isnot(None)
                    )
                )
                .order_by(Media.chat_id, Media.message_id)
            )
            result = await session.execute(stmt)
            return [
                {
                    'id': m.id,
                    'message_id': m.message_id,
                    'chat_id': m.chat_id,
                    'type': m.type,
                    'file_path': m.file_path,
                    'file_name': m.file_name,
                    'file_size': m.file_size,
                    'downloaded': m.downloaded,
                }
                for m in result.scalars()
            ]
    
    async def mark_media_for_redownload(self, media_id: str) -> None:
        """Mark a media record as needing re-download."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(Media.id == media_id)
                .values(downloaded=0, file_path=None, download_date=None)
            )
            await session.execute(stmt)
            await session.commit()
    
    # ========== Reaction Operations ==========
    
    @retry_on_locked()
    async def insert_reactions(self, message_id: int, chat_id: int, reactions: List[Dict[str, Any]]) -> None:
        """Insert reactions for a message."""
        if not reactions:
            return
        
        async with self.db_manager.async_session_factory() as session:
            # Delete existing reactions
            await session.execute(
                delete(Reaction).where(
                    and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id)
                )
            )
            
            # Insert new reactions
            for reaction in reactions:
                r = Reaction(
                    message_id=message_id,
                    chat_id=chat_id,
                    emoji=reaction['emoji'],
                    user_id=reaction.get('user_id'),
                    count=reaction.get('count', 1),
                )
                session.add(r)
            
            await session.commit()
    
    async def get_reactions(self, message_id: int, chat_id: int) -> List[Dict[str, Any]]:
        """Get all reactions for a message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Reaction)
                .where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
                .order_by(Reaction.emoji)
            )
            result = await session.execute(stmt)
            return [
                {'emoji': r.emoji, 'user_id': r.user_id, 'count': r.count}
                for r in result.scalars()
            ]
    
    # ========== Sync Status Operations ==========
    
    async def get_last_message_id(self, chat_id: int) -> int:
        """Get the last synced message ID for a chat."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(SyncStatus.last_message_id).where(SyncStatus.chat_id == chat_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else 0
    
    @retry_on_locked()
    async def update_sync_status(self, chat_id: int, last_message_id: int, message_count: int) -> None:
        """Update sync status for a chat using atomic upsert."""
        async with self.db_manager.async_session_factory() as session:
            now = datetime.utcnow()
            values = {
                'chat_id': chat_id,
                'last_message_id': last_message_id,
                'last_sync_date': now,
                'message_count': message_count
            }

            if self._is_sqlite:
                stmt = sqlite_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['chat_id'],
                    set_={
                        'last_message_id': stmt.excluded.last_message_id,
                        'last_sync_date': stmt.excluded.last_sync_date,
                        'message_count': SyncStatus.message_count + stmt.excluded.message_count
                    }
                )
            else:
                stmt = pg_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['chat_id'],
                    set_={
                        'last_message_id': stmt.excluded.last_message_id,
                        'last_sync_date': stmt.excluded.last_sync_date,
                        'message_count': SyncStatus.message_count + stmt.excluded.message_count
                    }
                )

            await session.execute(stmt)
            await session.commit()
    
    # ========== Statistics ==========
    
    async def get_statistics(self) -> Dict[str, Any]:
        """Get backup statistics."""
        async with self.db_manager.async_session_factory() as session:
            # Chat count
            chat_count = await session.execute(select(func.count(Chat.id)))
            chat_count = chat_count.scalar()
            
            # Message count
            msg_count = await session.execute(select(func.count()).select_from(Message))
            msg_count = msg_count.scalar()
            
            # Media count
            media_count = await session.execute(
                select(func.count(Media.id)).where(Media.downloaded == 1)
            )
            media_count = media_count.scalar()
            
            # Total media size
            total_size = await session.execute(
                select(func.sum(Media.file_size)).where(Media.downloaded == 1)
            )
            total_size = total_size.scalar() or 0
            
            # Last backup time
            last_backup_time = await self.get_metadata('last_backup_time')
            timezone_source = 'metadata'
            
            if not last_backup_time:
                last_sync = await session.execute(
                    select(func.max(SyncStatus.last_sync_date))
                )
                last_backup_time = last_sync.scalar()
                if last_backup_time:
                    timezone_source = 'sync_status'
            
            stats = {
                'chats': chat_count,
                'messages': msg_count,
                'media_files': media_count,
                'total_size_mb': round(total_size / (1024 * 1024), 2)
            }
            
            if last_backup_time:
                stats['last_backup_time'] = last_backup_time
                stats['last_backup_time_source'] = timezone_source
            
            return stats
    
    # ========== Delete Operations ==========
    
    async def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None) -> None:
        """Delete a chat and all related data."""
        async with self.db_manager.async_session_factory() as session:
            # Delete media records
            await session.execute(delete(Media).where(Media.chat_id == chat_id))
            # Delete reactions
            await session.execute(delete(Reaction).where(Reaction.chat_id == chat_id))
            # Delete messages
            await session.execute(delete(Message).where(Message.chat_id == chat_id))
            # Delete sync status
            await session.execute(delete(SyncStatus).where(SyncStatus.chat_id == chat_id))
            # Delete chat
            await session.execute(delete(Chat).where(Chat.id == chat_id))
            
            await session.commit()
            logger.info(f"Deleted chat {chat_id} and all related data from database")
        
        # Delete physical files
        if media_base_path and os.path.exists(media_base_path):
            chat_media_dir = os.path.join(media_base_path, str(chat_id))
            if os.path.exists(chat_media_dir):
                try:
                    shutil.rmtree(chat_media_dir)
                    logger.info(f"Deleted media folder: {chat_media_dir}")
                except Exception as e:
                    logger.error(f"Failed to delete media folder {chat_media_dir}: {e}")
            
            for avatar_type in ['chats', 'users']:
                avatar_pattern = os.path.join(media_base_path, 'avatars', avatar_type, f'{chat_id}_*.jpg')
                avatar_files = glob.glob(avatar_pattern)
                for avatar_file in avatar_files:
                    try:
                        os.remove(avatar_file)
                        logger.info(f"Deleted avatar file: {avatar_file}")
                    except Exception as e:
                        logger.error(f"Failed to delete avatar {avatar_file}: {e}")
    
    # ========== Web Viewer Operations ==========
    
    async def get_messages_paginated(
        self,
        chat_id: int,
        limit: int = 50,
        offset: int = 0,
        search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get messages with user info and media info for web viewer.
        
        Args:
            chat_id: Chat ID
            limit: Maximum messages to return
            offset: Pagination offset
            search: Optional text search filter
            
        Returns:
            List of message dictionaries with user and media info
        """
        async with self.db_manager.async_session_factory() as session:
            # Build query with joins
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.file_name.label('media_file_name'),
                    Media.mime_type.label('media_mime_type'),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, Message.media_id == Media.id)
                .where(Message.chat_id == chat_id)
            )
            
            if search:
                stmt = stmt.where(Message.text.ilike(f'%{search}%'))
            
            stmt = stmt.order_by(Message.date.desc()).limit(limit).offset(offset)
            
            result = await session.execute(stmt)
            messages = []
            
            for row in result:
                msg = self._message_to_dict(row.Message)
                msg['first_name'] = row.first_name
                msg['last_name'] = row.last_name
                msg['username'] = row.username
                msg['media_file_name'] = row.media_file_name
                msg['media_mime_type'] = row.media_mime_type
                
                # Parse raw_data JSON
                if msg.get('raw_data'):
                    try:
                        msg['raw_data'] = json.loads(msg['raw_data'])
                    except:
                        msg['raw_data'] = {}
                
                messages.append(msg)
            
            # Get reply texts and reactions for each message
            for msg in messages:
                if msg.get('reply_to_msg_id') and not msg.get('reply_to_text'):
                    reply_result = await session.execute(
                        select(Message.text)
                        .where(and_(Message.chat_id == chat_id, Message.id == msg['reply_to_msg_id']))
                    )
                    reply_text = reply_result.scalar_one_or_none()
                    if reply_text:
                        msg['reply_to_text'] = reply_text[:100]
                
                # Get reactions
                reactions = await self.get_reactions(msg['id'], chat_id)
                reactions_by_emoji = {}
                for reaction in reactions:
                    emoji = reaction['emoji']
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {'emoji': emoji, 'count': 0, 'user_ids': []}
                    reactions_by_emoji[emoji]['count'] += reaction.get('count', 1)
                    if reaction.get('user_id'):
                        reactions_by_emoji[emoji]['user_ids'].append(reaction['user_id'])
                msg['reactions'] = list(reactions_by_emoji.values())
            
            return messages
    
    async def find_message_by_date_with_joins(
        self,
        chat_id: int,
        target_date: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Find message by date with full user/media joins for web viewer.
        
        Args:
            chat_id: Chat ID
            target_date: Target date to find message for
            
        Returns:
            Message dictionary with user and media info, or None
        """
        async with self.db_manager.async_session_factory() as session:
            base_stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.file_name.label('media_file_name'),
                    Media.mime_type.label('media_mime_type'),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, Message.media_id == Media.id)
                .where(Message.chat_id == chat_id)
            )
            
            # Try on or after target date
            stmt = base_stmt.where(Message.date >= target_date).order_by(Message.date.asc()).limit(1)
            result = await session.execute(stmt)
            row = result.first()
            
            if not row:
                # Try before target date
                stmt = base_stmt.where(Message.date < target_date).order_by(Message.date.desc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()
            
            if not row:
                # Try first message in chat
                stmt = base_stmt.order_by(Message.date.asc()).limit(1)
                result = await session.execute(stmt)
                row = result.first()
            
            if not row:
                return None
            
            msg = self._message_to_dict(row.Message)
            msg['first_name'] = row.first_name
            msg['last_name'] = row.last_name
            msg['username'] = row.username
            msg['media_file_name'] = row.media_file_name
            msg['media_mime_type'] = row.media_mime_type
            
            # Parse raw_data
            if msg.get('raw_data'):
                try:
                    msg['raw_data'] = json.loads(msg['raw_data'])
                except:
                    msg['raw_data'] = {}
            
            # Get reply text
            if msg.get('reply_to_msg_id') and not msg.get('reply_to_text'):
                reply_result = await session.execute(
                    select(Message.text)
                    .where(and_(Message.chat_id == chat_id, Message.id == msg['reply_to_msg_id']))
                )
                reply_text = reply_result.scalar_one_or_none()
                if reply_text:
                    msg['reply_to_text'] = reply_text[:100]
            
            # Get reactions
            reactions = await self.get_reactions(msg['id'], chat_id)
            reactions_by_emoji = {}
            for reaction in reactions:
                emoji = reaction['emoji']
                if emoji not in reactions_by_emoji:
                    reactions_by_emoji[emoji] = {'emoji': emoji, 'count': 0, 'user_ids': []}
                reactions_by_emoji[emoji]['count'] += reaction.get('count', 1)
                if reaction.get('user_id'):
                    reactions_by_emoji[emoji]['user_ids'].append(reaction['user_id'])
            msg['reactions'] = list(reactions_by_emoji.values())
            
            return msg
    
    async def get_chat_by_id(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Get a single chat by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Chat).where(Chat.id == chat_id))
            chat = result.scalar_one_or_none()
            if not chat:
                return None
            return {
                'id': chat.id,
                'type': chat.type,
                'title': chat.title,
                'username': chat.username,
                'first_name': chat.first_name,
                'last_name': chat.last_name,
                'phone': chat.phone,
                'description': chat.description,
                'participants_count': chat.participants_count,
            }
    
    async def get_messages_for_export(self, chat_id: int):
        """
        Get messages for export with user info.
        Returns an async generator for streaming.
        
        Args:
            chat_id: Chat ID to export
            
        Yields:
            Message dictionaries with user info
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Message.id,
                    Message.date,
                    Message.text,
                    Message.is_outgoing,
                    Message.reply_to_msg_id,
                    User.first_name,
                    User.last_name,
                    User.username,
                )
                .outerjoin(User, Message.sender_id == User.id)
                .where(Message.chat_id == chat_id)
                .order_by(Message.date.asc())
            )
            
            result = await session.stream(stmt)
            async for row in result:
                yield {
                    'id': row.id,
                    'date': row.date.isoformat() if row.date else None,
                    'sender': {
                        'name': f"{row.first_name or ''} {row.last_name or ''}".strip() or row.username or "Unknown",
                        'username': row.username
                    },
                    'text': row.text,
                    'is_outgoing': bool(row.is_outgoing),
                    'reply_to': row.reply_to_msg_id
                }
    
    async def close(self) -> None:
        """Close database connections."""
        await self.db_manager.close()

