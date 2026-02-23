"""
Async database adapter for Telegram Backup.

Provides all database operations using SQLAlchemy async.
This is a drop-in replacement for the old Database class.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
from datetime import datetime
from functools import wraps
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .base import DatabaseManager
from .models import (
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    Metadata,
    Reaction,
    SyncStatus,
    User,
    ViewerAccount,
    ViewerAuditLog,
)

logger = logging.getLogger(__name__)


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Strip timezone info from datetime for PostgreSQL compatibility."""
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def retry_on_locked(
    max_retries: int = 5, initial_delay: float = 0.1, max_delay: float = 2.0, backoff_factor: float = 2.0
):
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
                    if "locked" not in error_str and "connection" not in error_str:
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
                        logger.error(f"Database error on {func.__name__} after {max_retries + 1} attempts. Giving up.")
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
            return "{}"

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
                return "{}"

    # ========== Metadata Operations ==========

    async def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        async with self.db_manager.async_session_factory() as session:
            # Use upsert
            if self._is_sqlite:
                stmt = sqlite_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            else:
                stmt = pg_insert(Metadata).values(key=key, value=value)
                stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
            await session.execute(stmt)
            await session.commit()

    async def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Metadata.value).where(Metadata.key == key))
            row = result.scalar_one_or_none()
            return row

    # ========== Chat Operations ==========

    @retry_on_locked()
    async def upsert_chat(self, chat_data: dict[str, Any]) -> int:
        """Insert or update a chat record.

        Only fields present in chat_data will be updated on conflict.
        This prevents the listener (which only provides basic fields)
        from overwriting is_forum/is_archived set by the backup.
        """
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": chat_data["id"],
                "type": chat_data.get("type", "unknown"),
                "title": chat_data.get("title"),
                "username": chat_data.get("username"),
                "first_name": chat_data.get("first_name"),
                "last_name": chat_data.get("last_name"),
                "phone": chat_data.get("phone"),
                "description": chat_data.get("description"),
                "participants_count": chat_data.get("participants_count"),
                "is_forum": chat_data.get("is_forum", 0),
                "is_archived": chat_data.get("is_archived", 0),
                "updated_at": datetime.utcnow(),
            }

            # Build update set from only the fields explicitly provided in chat_data.
            # This prevents partial upserts (e.g. from the listener) from resetting
            # is_forum/is_archived to their defaults.
            update_set = {
                "updated_at": datetime.utcnow(),
            }
            # Always update these basic metadata fields
            for field in (
                "type",
                "title",
                "username",
                "first_name",
                "last_name",
                "phone",
                "description",
                "participants_count",
            ):
                if field in chat_data:
                    update_set[field] = values[field]
            # Only update is_forum/is_archived if explicitly provided
            if "is_forum" in chat_data:
                update_set["is_forum"] = values["is_forum"]
            if "is_archived" in chat_data:
                update_set["is_archived"] = values["is_archived"]

            if self._is_sqlite:
                stmt = sqlite_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)
            else:
                stmt = pg_insert(Chat).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()
            return chat_data["id"]

    async def get_all_chats(
        self,
        limit: int = None,
        offset: int = 0,
        search: str = None,
        archived: bool | None = None,
        folder_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get chats with their last message date, with optional pagination and search.

        Args:
            limit: Maximum number of chats to return
            offset: Offset for pagination
            search: Optional search query (case-insensitive, matches title/first_name/last_name/username)
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
        """
        async with self.db_manager.async_session_factory() as session:
            # Subquery for last message date
            subq = (
                select(Message.chat_id, func.max(Message.date).label("last_message_date"))
                .group_by(Message.chat_id)
                .subquery()
            )

            stmt = select(Chat, subq.c.last_message_date).outerjoin(subq, Chat.id == subq.c.chat_id)

            # Filter by folder membership
            if folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember, and_(ChatFolderMember.chat_id == Chat.id, ChatFolderMember.folder_id == folder_id)
                )

            # Filter by archived status
            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            # Apply search filter if provided
            if search:
                search_pattern = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern),
                        Chat.first_name.ilike(search_pattern),
                        Chat.last_name.ilike(search_pattern),
                        Chat.username.ilike(search_pattern),
                    )
                )

            # Order by last message date
            stmt = stmt.order_by(subq.c.last_message_date.is_(None), subq.c.last_message_date.desc())

            # Apply pagination if limit is specified
            if limit is not None:
                stmt = stmt.limit(limit).offset(offset)

            result = await session.execute(stmt)
            chats = []
            for row in result:
                chat_dict = {
                    "id": row.Chat.id,
                    "type": row.Chat.type,
                    "title": row.Chat.title,
                    "username": row.Chat.username,
                    "first_name": row.Chat.first_name,
                    "last_name": row.Chat.last_name,
                    "phone": row.Chat.phone,
                    "description": row.Chat.description,
                    "participants_count": row.Chat.participants_count,
                    "is_forum": row.Chat.is_forum,
                    "is_archived": row.Chat.is_archived,
                    "last_synced_message_id": row.Chat.last_synced_message_id,
                    "created_at": row.Chat.created_at,
                    "updated_at": row.Chat.updated_at,
                    "last_message_date": row.last_message_date,
                }
                chats.append(chat_dict)
            return chats

    async def get_chat_count(
        self, search: str = None, archived: bool | None = None, folder_id: int | None = None
    ) -> int:
        """Get total number of chats (fast count for pagination).

        Args:
            search: Optional search query to filter count
            archived: If True, only archived chats; if False, only non-archived; if None, all
            folder_id: If set, only chats in this folder
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Chat.id))

            if folder_id is not None:
                stmt = stmt.join(
                    ChatFolderMember, and_(ChatFolderMember.chat_id == Chat.id, ChatFolderMember.folder_id == folder_id)
                )

            if archived is True:
                stmt = stmt.where(Chat.is_archived == 1)
            elif archived is False:
                stmt = stmt.where(or_(Chat.is_archived == 0, Chat.is_archived.is_(None)))

            if search:
                search_pattern = f"%{search}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern),
                        Chat.first_name.ilike(search_pattern),
                        Chat.last_name.ilike(search_pattern),
                        Chat.username.ilike(search_pattern),
                    )
                )

            result = await session.execute(stmt)
            return result.scalar() or 0

    # ========== User Operations ==========

    async def upsert_user(self, user_data: dict[str, Any]) -> None:
        """Insert or update a user record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": user_data["id"],
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name"),
                "last_name": user_data.get("last_name"),
                "phone": user_data.get("phone"),
                "is_bot": 1 if user_data.get("is_bot") else 0,
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "username": stmt.excluded.username,
                        "first_name": stmt.excluded.first_name,
                        "last_name": stmt.excluded.last_name,
                        "phone": stmt.excluded.phone,
                        "is_bot": stmt.excluded.is_bot,
                        "updated_at": datetime.utcnow(),
                    },
                )
            else:
                stmt = pg_insert(User).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "username": stmt.excluded.username,
                        "first_name": stmt.excluded.first_name,
                        "last_name": stmt.excluded.last_name,
                        "phone": stmt.excluded.phone,
                        "is_bot": stmt.excluded.is_bot,
                        "updated_at": datetime.utcnow(),
                    },
                )

            await session.execute(stmt)
            await session.commit()

    # ========== Message Operations ==========

    async def insert_message(self, message_data: dict[str, Any]) -> None:
        """Insert a message record.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": message_data["id"],
                "chat_id": message_data["chat_id"],
                "sender_id": message_data.get("sender_id"),
                "date": _strip_tz(message_data["date"]),
                "text": message_data.get("text"),
                "reply_to_msg_id": message_data.get("reply_to_msg_id"),
                "reply_to_top_id": message_data.get("reply_to_top_id"),
                "reply_to_text": message_data.get("reply_to_text"),
                "forward_from_id": message_data.get("forward_from_id"),
                "edit_date": _strip_tz(message_data.get("edit_date")),
                "raw_data": self._serialize_raw_data(message_data.get("raw_data", {})),
                "is_outgoing": message_data.get("is_outgoing", 0),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)
            else:
                stmt = pg_insert(Message).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)

            await session.execute(stmt)
            await session.commit()

    @retry_on_locked()
    async def insert_messages_batch(self, messages_data: list[dict[str, Any]]) -> None:
        """Insert multiple message records in a single transaction.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        if not messages_data:
            return

        async with self.db_manager.async_session_factory() as session:
            for m in messages_data:
                values = {
                    "id": m["id"],
                    "chat_id": m["chat_id"],
                    "sender_id": m.get("sender_id"),
                    "date": _strip_tz(m["date"]),
                    "text": m.get("text"),
                    "reply_to_msg_id": m.get("reply_to_msg_id"),
                    "reply_to_top_id": m.get("reply_to_top_id"),
                    "reply_to_text": m.get("reply_to_text"),
                    "forward_from_id": m.get("forward_from_id"),
                    "edit_date": _strip_tz(m.get("edit_date")),
                    "raw_data": self._serialize_raw_data(m.get("raw_data", {})),
                    "is_outgoing": m.get("is_outgoing", 0),
                    "is_pinned": m.get("is_pinned", 0),
                }

                if self._is_sqlite:
                    stmt = sqlite_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)
                else:
                    stmt = pg_insert(Message).values(**values)
                    stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=values)

                await session.execute(stmt)

            await session.commit()

    async def get_messages_by_date_range(
        self, chat_id: int | None = None, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> list[dict[str, Any]]:
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

    async def find_message_by_date(self, chat_id: int, target_date: datetime) -> dict[str, Any] | None:
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

    async def get_messages_sync_data(self, chat_id: int) -> dict[int, str | None]:
        """Get message IDs and their edit dates for sync checking."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.id, Message.edit_date).where(Message.chat_id == chat_id)
            result = await session.execute(stmt)
            return {row.id: row.edit_date for row in result}

    async def get_chat_id_for_message(self, message_id: int) -> int | None:
        """
        Look up the chat_id for a message by its ID.

        Used when Telegram sends deletion events without chat_id.
        Note: Message IDs are only unique within a chat, so this may return
        multiple results. Returns the first match.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message.chat_id).where(Message.id == message_id).limit(1)
            result = await session.execute(stmt)
            row = result.first()
            return row[0] if row else None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a specific message and its media."""
        async with self.db_manager.async_session_factory() as session:
            # Delete associated media
            await session.execute(delete(Media).where(and_(Media.chat_id == chat_id, Media.message_id == message_id)))
            # Delete reactions
            await session.execute(
                delete(Reaction).where(and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id))
            )
            # Delete the message
            await session.execute(delete(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)))
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
            result = await session.execute(select(Message.chat_id).where(Message.id == message_id))
            chat_ids = [row[0] for row in result.fetchall()]

            if not chat_ids:
                return False

            # Delete from all matching chats (usually just one)
            for chat_id in chat_ids:
                # Delete associated media
                await session.execute(
                    delete(Media).where(and_(Media.chat_id == chat_id, Media.message_id == message_id))
                )
                # Delete reactions
                await session.execute(
                    delete(Reaction).where(and_(Reaction.chat_id == chat_id, Reaction.message_id == message_id))
                )
                # Delete the message
                await session.execute(delete(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)))

            await session.commit()
            logger.debug(f"Deleted message {message_id} from {len(chat_ids)} chat(s)")
            return True

    async def update_message_text(
        self, chat_id: int, message_id: int, new_text: str, edit_date: datetime | None
    ) -> None:
        """Update a message's text and edit_date."""
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
                .where(
                    and_(Message.sender_id == owner_id, or_(Message.is_outgoing == 0, Message.is_outgoing.is_(None)))
                )
                .values(is_outgoing=1)
            )
            await session.commit()
            if result.rowcount > 0:
                logger.info(f"Backfilled is_outgoing=1 for {result.rowcount} messages from owner {owner_id}")

    def _message_to_dict(self, message: Message) -> dict[str, Any]:
        """Convert Message model to dictionary.

        v6.0.0: media_type, media_id, media_path removed - use media_items relationship.
        """
        return {
            "id": message.id,
            "chat_id": message.chat_id,
            "sender_id": message.sender_id,
            "date": message.date,
            "text": message.text,
            "reply_to_msg_id": message.reply_to_msg_id,
            "reply_to_top_id": message.reply_to_top_id,
            "reply_to_text": message.reply_to_text,
            "forward_from_id": message.forward_from_id,
            "edit_date": message.edit_date,
            "raw_data": message.raw_data,
            "created_at": message.created_at,
            "is_outgoing": message.is_outgoing,
            "is_pinned": message.is_pinned,
        }

    async def get_chat_stats(self, chat_id: int) -> dict[str, Any]:
        """Get statistics for a specific chat (message count, media count, total size).

        Returns:
            Dict with keys: messages, media_files, total_size_bytes, first_message_date, last_message_date
        """
        async with self.db_manager.async_session_factory() as session:
            # Message count
            msg_result = await session.execute(select(func.count(Message.id)).where(Message.chat_id == chat_id))
            message_count = msg_result.scalar() or 0

            # Media count and total size
            media_result = await session.execute(
                select(func.count(Media.id), func.coalesce(func.sum(Media.file_size), 0)).where(
                    Media.chat_id == chat_id
                )
            )
            media_row = media_result.one()
            media_count = media_row[0] or 0
            total_size = media_row[1] or 0

            # First and last message dates
            date_result = await session.execute(
                select(func.min(Message.date), func.max(Message.date)).where(Message.chat_id == chat_id)
            )
            date_row = date_result.one()
            first_message = date_row[0]
            last_message = date_row[1]

            return {
                "chat_id": chat_id,
                "messages": int(message_count),
                "media_files": int(media_count),
                "total_size_bytes": int(total_size),
                "total_size_mb": round(total_size / (1024 * 1024), 2) if total_size else 0,
                "first_message_date": first_message.isoformat() if first_message else None,
                "last_message_date": last_message.isoformat() if last_message else None,
            }

    # ========== Media Operations ==========

    async def insert_media(self, media_data: dict[str, Any]) -> None:
        """Insert a media file record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": media_data["id"],
                "message_id": media_data.get("message_id"),
                "chat_id": media_data.get("chat_id"),
                "type": media_data["type"],
                "file_name": media_data.get("file_name"),
                "file_path": media_data.get("file_path"),
                "file_size": media_data.get("file_size"),
                "mime_type": media_data.get("mime_type"),
                "width": media_data.get("width"),
                "height": media_data.get("height"),
                "duration": media_data.get("duration"),
                "downloaded": 1 if media_data.get("downloaded") else 0,
                "download_date": media_data.get("download_date"),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)
            else:
                stmt = pg_insert(Media).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=values)

            await session.execute(stmt)
            await session.commit()

    async def get_media_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        """
        Get all media records for a specific chat.

        Args:
            chat_id: Chat identifier

        Returns:
            List of media records with file paths and metadata
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(Media.chat_id == chat_id)
            result = await session.execute(stmt)
            media_records = result.scalars().all()

            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                }
                for m in media_records
            ]

    async def delete_media_for_chat(self, chat_id: int) -> int:
        """
        Delete all media records for a specific chat.
        Does not delete message records or the chat itself.

        Args:
            chat_id: Chat identifier

        Returns:
            Number of media records deleted
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = delete(Media).where(Media.chat_id == chat_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    async def get_media_for_verification(self) -> list[dict[str, Any]]:
        """
        Get all media records that should have files on disk.
        Used by VERIFY_MEDIA to check for missing/corrupted files.

        Returns media where downloaded=1 OR file_path is not null.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media)
                .where(or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                .order_by(Media.chat_id, Media.message_id)
            )
            result = await session.execute(stmt)
            return [
                {
                    "id": m.id,
                    "message_id": m.message_id,
                    "chat_id": m.chat_id,
                    "type": m.type,
                    "file_path": m.file_path,
                    "file_name": m.file_name,
                    "file_size": m.file_size,
                    "downloaded": m.downloaded,
                }
                for m in result.scalars()
            ]

    async def mark_media_for_redownload(self, media_id: str) -> None:
        """Mark a media record as needing re-download."""
        async with self.db_manager.async_session_factory() as session:
            stmt = update(Media).where(Media.id == media_id).values(downloaded=0, file_path=None, download_date=None)
            await session.execute(stmt)
            await session.commit()

    # ========== Reaction Operations ==========

    @retry_on_locked()
    async def insert_reactions(self, message_id: int, chat_id: int, reactions: list[dict[str, Any]]) -> None:
        """Insert reactions for a message using upsert to avoid sequence issues."""
        if not reactions:
            return

        async with self.db_manager.async_session_factory() as session:
            # Delete existing reactions first
            await session.execute(
                delete(Reaction).where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
            )
            await session.commit()

        # Insert in a separate transaction to avoid sequence conflicts
        async with self.db_manager.async_session_factory() as session:
            for reaction in reactions:
                try:
                    r = Reaction(
                        message_id=message_id,
                        chat_id=chat_id,
                        emoji=reaction["emoji"],
                        user_id=reaction.get("user_id"),
                        count=reaction.get("count", 1),
                    )
                    session.add(r)
                    await session.flush()  # Flush each to catch errors early
                except Exception as e:
                    if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
                        # Sequence out of sync - reset and retry
                        logger.warning("Reactions sequence out of sync, resetting...")
                        await session.rollback()
                        await self._reset_reactions_sequence()
                        # Retry the insert
                        async with self.db_manager.async_session_factory() as retry_session:
                            r = Reaction(
                                message_id=message_id,
                                chat_id=chat_id,
                                emoji=reaction["emoji"],
                                user_id=reaction.get("user_id"),
                                count=reaction.get("count", 1),
                            )
                            retry_session.add(r)
                            await retry_session.commit()
                        return  # Exit after recovery
                    raise

            await session.commit()

    async def _reset_reactions_sequence(self) -> None:
        """Reset the reactions table sequence to max(id) + 1."""
        async with self.db_manager.async_session_factory() as session:
            if self.db_manager.db_type == "postgresql":
                await session.execute(
                    text("SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false)")
                )
                await session.commit()
                logger.info("Reset reactions_id_seq sequence")

    async def get_reactions(self, message_id: int, chat_id: int) -> list[dict[str, Any]]:
        """Get all reactions for a message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Reaction)
                .where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
                .order_by(Reaction.emoji)
            )
            result = await session.execute(stmt)
            return [{"emoji": r.emoji, "user_id": r.user_id, "count": r.count} for r in result.scalars()]

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
                "chat_id": chat_id,
                "last_message_id": last_message_id,
                "last_sync_date": now,
                "message_count": message_count,
            }

            if self._is_sqlite:
                stmt = sqlite_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={
                        "last_message_id": stmt.excluded.last_message_id,
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )
            else:
                stmt = pg_insert(SyncStatus).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["chat_id"],
                    set_={
                        "last_message_id": stmt.excluded.last_message_id,
                        "last_sync_date": stmt.excluded.last_sync_date,
                        "message_count": SyncStatus.message_count + stmt.excluded.message_count,
                    },
                )

            await session.execute(stmt)
            await session.commit()

    # ========== Statistics ==========

    async def get_statistics(self) -> dict[str, Any]:
        """Get statistics - alias for get_cached_statistics for backwards compatibility."""
        return await self.get_cached_statistics()

    async def get_cached_statistics(self) -> dict[str, Any]:
        """Get cached statistics (fast, no expensive queries)."""
        # Get cached stats from metadata
        cached_stats = await self.get_metadata("cached_stats")
        stats_calculated_at = await self.get_metadata("stats_calculated_at")
        last_backup_time = await self.get_metadata("last_backup_time")

        result = {
            "chats": 0,
            "messages": 0,
            "media_files": 0,
            "total_size_mb": 0,
            "stats_calculated_at": stats_calculated_at,
        }

        if cached_stats:
            import json

            try:
                result.update(json.loads(cached_stats))
            except json.JSONDecodeError, TypeError:
                pass

        if last_backup_time:
            result["last_backup_time"] = last_backup_time
            result["last_backup_time_source"] = "metadata"

        return result

    async def calculate_and_store_statistics(self) -> dict[str, Any]:
        """Calculate statistics and store in metadata (expensive, run daily)."""
        import json
        from datetime import datetime

        async with self.db_manager.async_session_factory() as session:
            logger.info("Calculating statistics (this may take a while)...")

            # Chat count
            chat_count = await session.execute(select(func.count(Chat.id)))
            chat_count = chat_count.scalar() or 0

            # Message count
            msg_count = await session.execute(select(func.count()).select_from(Message))
            msg_count = msg_count.scalar() or 0

            # Media count
            media_count = await session.execute(select(func.count(Media.id)).where(Media.downloaded == 1))
            media_count = media_count.scalar() or 0

            # Total media size
            total_size = await session.execute(select(func.sum(Media.file_size)).where(Media.downloaded == 1))
            total_size = total_size.scalar() or 0

            # Per-chat statistics
            chat_stats_query = select(Message.chat_id, func.count(Message.id).label("message_count")).group_by(
                Message.chat_id
            )
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {row.chat_id: row.message_count for row in chat_stats_result}

            stats = {
                "chats": int(chat_count),
                "messages": int(msg_count),
                "media_files": int(media_count),
                "total_size_mb": float(round(total_size / (1024 * 1024), 2)),
                "per_chat_message_counts": {int(k): int(v) for k, v in per_chat_stats.items()},
            }

            logger.info(f"Statistics calculated: {chat_count} chats, {msg_count} messages, {media_count} media files")

        # Store in metadata
        await self.set_metadata("cached_stats", json.dumps(stats))
        await self.set_metadata("stats_calculated_at", datetime.utcnow().isoformat())

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

            for avatar_type in ["chats", "users"]:
                avatar_pattern = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}_*.jpg")
                avatar_files = glob.glob(avatar_pattern)

                # Legacy fallback: remove old <chat_id>.jpg files as well
                legacy_avatar = os.path.join(media_base_path, "avatars", avatar_type, f"{chat_id}.jpg")
                if os.path.exists(legacy_avatar):
                    avatar_files.append(legacy_avatar)
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
        search: str | None = None,
        before_date: datetime | None = None,
        before_id: int | None = None,
        topic_id: int | None = None,
        sender_id: int | None = None,
        media_type: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get messages with user info and media info for web viewer.

        v6.0.0: Media is now returned as a nested object from the media table.
        v6.2.0: Added topic_id filter for forum topic messages.

        Supports two pagination modes:
        1. Offset-based (legacy): Uses offset parameter - slower for large offsets
        2. Cursor-based (preferred): Uses before_date/before_id - O(1) regardless of position

        Args:
            chat_id: Chat ID
            limit: Maximum messages to return
            offset: Pagination offset (used only if before_date/before_id not provided)
            search: Optional text search filter
            before_date: Cursor - get messages before this date (faster than offset)
            before_id: Cursor - message ID to use as tiebreaker for same-date messages
            topic_id: Optional forum topic ID to filter messages by thread

        Returns:
            List of message dictionaries with user and media info
        """
        async with self.db_manager.async_session_factory() as session:
            # Build query with joins - v6.0.0: join on composite key
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(Message.chat_id == chat_id)
            )

            # v6.2.0: Filter by forum topic
            if topic_id is not None:
                stmt = stmt.where(Message.reply_to_top_id == topic_id)

            if search:
                stmt = stmt.where(Message.text.ilike(f"%{search}%"))

            # Advanced search filters
            if sender_id is not None:
                stmt = stmt.where(Message.sender_id == sender_id)
            if media_type is not None:
                stmt = stmt.where(Media.type == media_type)
            if date_from is not None:
                stmt = stmt.where(Message.date >= date_from)
            if date_to is not None:
                stmt = stmt.where(Message.date <= date_to)

            # Cursor-based pagination (preferred - O(1) performance)
            if before_date is not None:
                # Use composite cursor: (date, id) for deterministic ordering
                # Messages with same date are ordered by id DESC
                if before_id is not None:
                    stmt = stmt.where(
                        or_(Message.date < before_date, and_(Message.date == before_date, Message.id < before_id))
                    )
                else:
                    stmt = stmt.where(Message.date < before_date)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit)
            else:
                # Offset-based pagination (legacy fallback)
                stmt = stmt.order_by(Message.date.desc()).limit(limit).offset(offset)

            result = await session.execute(stmt)
            messages = []

            for row in result:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username

                # v6.0.0: Media as nested object
                if row.media_type:
                    msg["media"] = {
                        "id": row.media_id,
                        "type": row.media_type,
                        "file_path": row.media_file_path,
                        "file_name": row.media_file_name,
                        "file_size": row.media_file_size,
                        "mime_type": row.media_mime_type,
                        "width": row.media_width,
                        "height": row.media_height,
                        "duration": row.media_duration,
                    }
                else:
                    msg["media"] = None

                # Parse raw_data JSON
                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except json.JSONDecodeError, TypeError:
                        msg["raw_data"] = {}

                messages.append(msg)

            # Get reply texts and reactions for each message
            for msg in messages:
                if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                    reply_result = await session.execute(
                        select(Message.text).where(
                            and_(Message.chat_id == chat_id, Message.id == msg["reply_to_msg_id"])
                        )
                    )
                    reply_text = reply_result.scalar_one_or_none()
                    if reply_text:
                        msg["reply_to_text"] = reply_text[:100]

                # Get reactions
                reactions = await self.get_reactions(msg["id"], chat_id)
                reactions_by_emoji = {}
                for reaction in reactions:
                    emoji = reaction["emoji"]
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                    reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                    if reaction.get("user_id"):
                        reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
                msg["reactions"] = list(reactions_by_emoji.values())

            return messages

    async def search_messages_global(
        self,
        query: str,
        display_chat_ids: list[int] | None = None,
        sender: str | None = None,
        media_type: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search messages across all chats for the web viewer global search.

        Returns dict with results list and total count.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Message.id,
                    Message.chat_id,
                    Message.text,
                    Message.date,
                    Message.sender_id,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Chat.title.label("chat_title"),
                    Chat.type.label("chat_type"),
                    Media.type.label("media_type"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Chat, Message.chat_id == Chat.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
            )

            # Restrict to allowed chats
            if display_chat_ids:
                stmt = stmt.where(Message.chat_id.in_(display_chat_ids))

            # Text search
            if query:
                stmt = stmt.where(Message.text.ilike(f"%{query}%"))

            # Sender name filter
            if sender:
                stmt = stmt.where(
                    or_(
                        User.first_name.ilike(f"%{sender}%"),
                        User.last_name.ilike(f"%{sender}%"),
                        User.username.ilike(f"%{sender}%"),
                    )
                )

            if media_type:
                stmt = stmt.where(Media.type == media_type)
            if date_from:
                stmt = stmt.where(Message.date >= date_from)
            if date_to:
                stmt = stmt.where(Message.date <= date_to)

            # Count total
            from sqlalchemy import func as sa_func

            count_stmt = select(sa_func.count()).select_from(stmt.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0

            # Fetch results
            stmt = stmt.order_by(Message.date.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)

            results = []
            for row in result:
                sender_name = row.first_name or ""
                if row.last_name:
                    sender_name += f" {row.last_name}"
                text_snippet = (row.text or "")[:150]
                results.append(
                    {
                        "message_id": row.id,
                        "chat_id": row.chat_id,
                        "chat_title": row.chat_title or str(row.chat_id),
                        "chat_type": row.chat_type,
                        "text_snippet": text_snippet,
                        "sender_name": sender_name.strip() or row.username or str(row.sender_id or ""),
                        "date": row.date,
                        "media_type": row.media_type,
                    }
                )

            return {"results": results, "total": total}

    async def get_chat_media(
        self,
        chat_id: int,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get media items for a chat, for the media gallery view."""
        async with self.db_manager.async_session_factory() as session:
            base = (
                select(
                    Media.id,
                    Media.message_id,
                    Media.type,
                    Media.file_path,
                    Media.file_name,
                    Media.file_size,
                    Media.width,
                    Media.height,
                    Media.duration,
                    Message.date,
                )
                .join(Message, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(Media.chat_id == chat_id)
                .where(Media.file_path.isnot(None))
            )

            if media_type:
                base = base.where(Media.type == media_type)

            # Count
            count_stmt = select(func.count()).select_from(base.subquery())
            total = (await session.execute(count_stmt)).scalar() or 0

            # Fetch
            stmt = base.order_by(Message.date.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)

            media_list = []
            for row in result:
                media_list.append(
                    {
                        "id": row.id,
                        "message_id": row.message_id,
                        "type": row.type,
                        "file_path": row.file_path,
                        "file_name": row.file_name,
                        "file_size": row.file_size,
                        "width": row.width,
                        "height": row.height,
                        "duration": row.duration,
                        "date": row.date,
                    }
                )

            return {"media": media_list, "total": total, "has_more": offset + len(media_list) < total}

    async def find_message_by_id(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        """Find a single message by ID with basic info."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
            result = await session.execute(stmt)
            msg = result.scalar_one_or_none()
            if msg:
                return self._message_to_dict(msg)
            return None

    async def find_message_by_date_with_joins(self, chat_id: int, target_date: datetime) -> dict[str, Any] | None:
        """
        Find message by date with full user/media joins for web viewer.

        v6.0.0: Media is now returned as a nested object from the media table.

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
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
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
            msg["first_name"] = row.first_name
            msg["last_name"] = row.last_name
            msg["username"] = row.username

            # v6.0.0: Media as nested object
            if row.media_type:
                msg["media"] = {
                    "id": row.media_id,
                    "type": row.media_type,
                    "file_path": row.media_file_path,
                    "file_name": row.media_file_name,
                    "file_size": row.media_file_size,
                    "mime_type": row.media_mime_type,
                    "width": row.media_width,
                    "height": row.media_height,
                    "duration": row.media_duration,
                }
            else:
                msg["media"] = None

            # Parse raw_data
            if msg.get("raw_data"):
                try:
                    msg["raw_data"] = json.loads(msg["raw_data"])
                except:
                    msg["raw_data"] = {}

            # Get reply text
            if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                reply_result = await session.execute(
                    select(Message.text).where(and_(Message.chat_id == chat_id, Message.id == msg["reply_to_msg_id"]))
                )
                reply_text = reply_result.scalar_one_or_none()
                if reply_text:
                    msg["reply_to_text"] = reply_text[:100]

            # Get reactions
            reactions = await self.get_reactions(msg["id"], chat_id)
            reactions_by_emoji = {}
            for reaction in reactions:
                emoji = reaction["emoji"]
                if emoji not in reactions_by_emoji:
                    reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                if reaction.get("user_id"):
                    reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
            msg["reactions"] = list(reactions_by_emoji.values())

            return msg

    async def get_chat_by_id(self, chat_id: int) -> dict[str, Any] | None:
        """Get a single chat by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Chat).where(Chat.id == chat_id))
            chat = result.scalar_one_or_none()
            if not chat:
                return None
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
                "is_forum": chat.is_forum,
                "is_archived": chat.is_archived,
            }

    async def get_pinned_messages(self, chat_id: int) -> list[dict[str, Any]]:
        """Get all pinned messages for a chat, ordered by date descending (newest first).

        v6.0.0: Media is now returned as a nested object from the media table.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(
                    Message,
                    User.first_name,
                    User.last_name,
                    User.username,
                    Media.id.label("media_id"),
                    Media.type.label("media_type"),
                    Media.file_path.label("media_file_path"),
                    Media.file_name.label("media_file_name"),
                    Media.file_size.label("media_file_size"),
                    Media.mime_type.label("media_mime_type"),
                    Media.width.label("media_width"),
                    Media.height.label("media_height"),
                    Media.duration.label("media_duration"),
                )
                .outerjoin(User, Message.sender_id == User.id)
                .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                .where(Message.chat_id == chat_id)
                .where(Message.is_pinned == 1)
                .order_by(Message.date.desc())
            )

            result = await session.execute(stmt)
            rows = result.all()

            messages = []
            for row in rows:
                msg = self._message_to_dict(row.Message)
                msg["first_name"] = row.first_name
                msg["last_name"] = row.last_name
                msg["username"] = row.username

                # v6.0.0: Media as nested object
                if row.media_type:
                    msg["media"] = {
                        "id": row.media_id,
                        "type": row.media_type,
                        "file_path": row.media_file_path,
                        "file_name": row.media_file_name,
                        "file_size": row.media_file_size,
                        "mime_type": row.media_mime_type,
                        "width": row.media_width,
                        "height": row.media_height,
                        "duration": row.media_duration,
                    }
                else:
                    msg["media"] = None

                # Parse raw_data JSON
                if msg.get("raw_data"):
                    try:
                        msg["raw_data"] = json.loads(msg["raw_data"])
                    except json.JSONDecodeError, TypeError:
                        msg["raw_data"] = {}

                messages.append(msg)

            return messages

    async def sync_pinned_messages(self, chat_id: int, pinned_message_ids: list[int]) -> None:
        """
        Sync pinned messages for a chat.

        Sets is_pinned=1 for messages in the list and is_pinned=0 for all others.
        This ensures the database reflects the current state of pinned messages.

        Args:
            chat_id: Chat ID
            pinned_message_ids: List of message IDs that are currently pinned
        """
        async with self.db_manager.async_session_factory() as session:
            # First, unpin all messages in this chat
            await session.execute(
                update(Message).where(Message.chat_id == chat_id).where(Message.is_pinned == 1).values(is_pinned=0)
            )

            # Then, pin the specified messages (if any exist in our database)
            if pinned_message_ids:
                await session.execute(
                    update(Message)
                    .where(Message.chat_id == chat_id)
                    .where(Message.id.in_(pinned_message_ids))
                    .values(is_pinned=1)
                )

            await session.commit()

    async def update_message_pinned(self, chat_id: int, message_id: int, is_pinned: bool) -> None:
        """
        Update the pinned status of a single message.

        Used by the real-time listener when pin/unpin events are received.

        Args:
            chat_id: Chat ID
            message_id: Message ID
            is_pinned: Whether the message is pinned
        """
        async with self.db_manager.async_session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.chat_id == chat_id)
                .where(Message.id == message_id)
                .values(is_pinned=1 if is_pinned else 0)
            )
            await session.commit()

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """Get a user by ID."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return None
            return {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "is_bot": user.is_bot,
            }

    async def get_messages_for_export(self, chat_id: int, include_media: bool = False):
        """
        Get messages for export with user info.
        Returns an async generator for streaming.

        v6.0.0: Media info now comes from the media table via JOIN.

        Args:
            chat_id: Chat ID to export
            include_media: If True, include media info from media table

        Yields:
            Message dictionaries with user info
        """
        async with self.db_manager.async_session_factory() as session:
            if include_media:
                stmt = (
                    select(
                        Message.id,
                        Message.date,
                        Message.text,
                        Message.is_outgoing,
                        Message.reply_to_msg_id,
                        Media.type.label("media_type"),
                        Media.file_path.label("media_file_path"),
                        User.first_name,
                        User.last_name,
                        User.username,
                    )
                    .outerjoin(User, Message.sender_id == User.id)
                    .outerjoin(Media, and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id))
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.date.asc())
                )
            else:
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
                msg = {
                    "id": row.id,
                    "date": row.date.isoformat() if row.date else None,
                    "sender": {
                        "name": f"{row.first_name or ''} {row.last_name or ''}".strip() or row.username or "Unknown",
                        "username": row.username,
                    },
                    "text": row.text,
                    "is_outgoing": bool(row.is_outgoing),
                    "reply_to": row.reply_to_msg_id,
                }
                if include_media:
                    msg["media_type"] = row.media_type
                    msg["media_path"] = row.media_file_path
                yield msg

    # ========== Forum Topic Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_forum_topic(self, topic_data: dict[str, Any]) -> None:
        """Insert or update a forum topic record."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": topic_data["id"],
                "chat_id": topic_data["chat_id"],
                "title": topic_data["title"],
                "icon_color": topic_data.get("icon_color"),
                "icon_emoji_id": topic_data.get("icon_emoji_id"),
                "icon_emoji": topic_data.get("icon_emoji"),
                "is_closed": topic_data.get("is_closed", 0),
                "is_pinned": topic_data.get("is_pinned", 0),
                "is_hidden": topic_data.get("is_hidden", 0),
                "date": _strip_tz(topic_data.get("date")),
                "updated_at": datetime.utcnow(),
            }

            update_set = {
                "title": values["title"],
                "icon_color": values["icon_color"],
                "icon_emoji_id": values["icon_emoji_id"],
                "icon_emoji": values["icon_emoji"],
                "is_closed": values["is_closed"],
                "is_pinned": values["is_pinned"],
                "is_hidden": values["is_hidden"],
                "date": values["date"],
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=update_set)
            else:
                stmt = pg_insert(ForumTopic).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id", "chat_id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    async def get_forum_topics(self, chat_id: int) -> list[dict[str, Any]]:
        """Get all forum topics for a chat, with message count per topic."""
        async with self.db_manager.async_session_factory() as session:
            # Subquery for message counts and last message date per topic.
            # Messages with reply_to_top_id=NULL are treated as General topic (id=1),
            # since pre-v6.2.0 messages and pre-forum messages lack topic assignment
            # and Telegram's client displays them under General.
            effective_topic_id = func.coalesce(Message.reply_to_top_id, 1).label("effective_topic_id")
            msg_subq = (
                select(
                    effective_topic_id,
                    func.count(Message.id).label("message_count"),
                    func.max(Message.date).label("last_message_date"),
                )
                .where(Message.chat_id == chat_id)
                .group_by(effective_topic_id)
                .subquery()
            )

            stmt = (
                select(ForumTopic, msg_subq.c.message_count, msg_subq.c.last_message_date)
                .outerjoin(msg_subq, ForumTopic.id == msg_subq.c.effective_topic_id)
                .where(ForumTopic.chat_id == chat_id)
                .order_by(ForumTopic.is_pinned.desc(), msg_subq.c.last_message_date.desc().nullslast())
            )

            result = await session.execute(stmt)
            topics = []
            for row in result:
                topic = row.ForumTopic
                topics.append(
                    {
                        "id": topic.id,
                        "chat_id": topic.chat_id,
                        "title": topic.title,
                        "icon_color": topic.icon_color,
                        "icon_emoji_id": topic.icon_emoji_id,
                        "icon_emoji": topic.icon_emoji,
                        "is_closed": topic.is_closed,
                        "is_pinned": topic.is_pinned,
                        "is_hidden": topic.is_hidden,
                        "date": topic.date,
                        "message_count": row.message_count or 0,
                        "last_message_date": row.last_message_date,
                    }
                )
            return topics

    # ========== Chat Folder Operations (v6.2.0) ==========

    @retry_on_locked()
    async def upsert_chat_folder(self, folder_data: dict[str, Any]) -> None:
        """Insert or update a chat folder."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "id": folder_data["id"],
                "title": folder_data["title"],
                "emoticon": folder_data.get("emoticon"),
                "sort_order": folder_data.get("sort_order", 0),
                "updated_at": datetime.utcnow(),
            }

            update_set = {
                "title": values["title"],
                "emoticon": values["emoticon"],
                "sort_order": values["sort_order"],
                "updated_at": datetime.utcnow(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)
            else:
                stmt = pg_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    @retry_on_locked()
    async def sync_folder_members(self, folder_id: int, chat_ids: list[int]) -> None:
        """Sync folder membership: replace all members for a folder."""
        async with self.db_manager.async_session_factory() as session:
            # Delete existing members
            await session.execute(delete(ChatFolderMember).where(ChatFolderMember.folder_id == folder_id))

            # Insert new members (only for chats that exist in our DB)
            if chat_ids:
                # Verify which chat_ids actually exist
                existing = await session.execute(select(Chat.id).where(Chat.id.in_(chat_ids)))
                existing_ids = {row[0] for row in existing}

                for cid in chat_ids:
                    if cid in existing_ids:
                        session.add(ChatFolderMember(folder_id=folder_id, chat_id=cid))

            await session.commit()

    async def get_all_folders(self) -> list[dict[str, Any]]:
        """Get all chat folders with their chat counts."""
        async with self.db_manager.async_session_factory() as session:
            count_subq = (
                select(ChatFolderMember.folder_id, func.count(ChatFolderMember.chat_id).label("chat_count"))
                .group_by(ChatFolderMember.folder_id)
                .subquery()
            )

            stmt = (
                select(ChatFolder, count_subq.c.chat_count)
                .outerjoin(count_subq, ChatFolder.id == count_subq.c.folder_id)
                .order_by(ChatFolder.sort_order, ChatFolder.title)
            )

            result = await session.execute(stmt)
            folders = []
            for row in result:
                folder = row.ChatFolder
                folders.append(
                    {
                        "id": folder.id,
                        "title": folder.title,
                        "emoticon": folder.emoticon,
                        "sort_order": folder.sort_order,
                        "chat_count": row.chat_count or 0,
                    }
                )
            return folders

    @retry_on_locked()
    async def cleanup_stale_folders(self, active_folder_ids: list[int]) -> None:
        """Remove folders that no longer exist in Telegram."""
        async with self.db_manager.async_session_factory() as session:
            if active_folder_ids:
                await session.execute(delete(ChatFolder).where(ChatFolder.id.notin_(active_folder_ids)))
            else:
                await session.execute(delete(ChatFolder))
            await session.commit()

    async def get_archived_chat_count(self) -> int:
        """Get the count of archived chats."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(func.count(Chat.id)).where(Chat.is_archived == 1))
            return result.scalar() or 0

    # ========================================================================
    # Viewer Account Management (multi-user access control)
    # ========================================================================

    @retry_on_locked()
    async def get_viewer_account_by_username(self, username: str) -> dict | None:
        """Get viewer account by username (case-insensitive)."""
        async with self.db_manager.get_session() as session:
            result = await session.execute(
                select(ViewerAccount).where(func.lower(ViewerAccount.username) == username.lower())
            )
            account = result.scalar_one_or_none()
            if not account:
                return None
            return {
                "id": account.id,
                "username": account.username,
                "password_hash": account.password_hash,
                "salt": account.salt,
                "allowed_chat_ids": account.allowed_chat_ids,
                "is_active": account.is_active,
                "created_at": account.created_at,
                "updated_at": account.updated_at,
            }

    @retry_on_locked()
    async def get_all_viewer_accounts(self) -> list[dict]:
        """Get all viewer accounts (for admin panel)."""
        async with self.db_manager.get_session() as session:
            result = await session.execute(select(ViewerAccount).order_by(ViewerAccount.created_at))
            accounts = result.scalars().all()
            return [
                {
                    "id": a.id,
                    "username": a.username,
                    "allowed_chat_ids": json.loads(a.allowed_chat_ids or "[]"),
                    "is_active": a.is_active,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in accounts
            ]

    @retry_on_locked()
    async def create_viewer_account(
        self, username: str, password_hash: str, salt: str, allowed_chat_ids: list[int]
    ) -> dict:
        """Create a new viewer account."""
        async with self.db_manager.get_session() as session:
            account = ViewerAccount(
                username=username,
                password_hash=password_hash,
                salt=salt,
                allowed_chat_ids=json.dumps(allowed_chat_ids),
            )
            session.add(account)
            await session.flush()
            result = {"id": account.id, "username": account.username}
            await session.commit()
            return result

    @retry_on_locked()
    async def update_viewer_account(self, account_id: int, **kwargs) -> bool:
        """Update viewer account fields. Supports: password_hash, salt, allowed_chat_ids, is_active."""
        async with self.db_manager.get_session() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                return False
            allowed_fields = {"password_hash", "salt", "allowed_chat_ids", "is_active"}
            for key, value in kwargs.items():
                if key in allowed_fields:
                    setattr(account, key, value)
            account.updated_at = datetime.utcnow()
            await session.commit()
            return True

    @retry_on_locked()
    async def delete_viewer_account(self, account_id: int) -> bool:
        """Delete a viewer account."""
        async with self.db_manager.get_session() as session:
            result = await session.execute(delete(ViewerAccount).where(ViewerAccount.id == account_id))
            await session.commit()
            return result.rowcount > 0

    # ========================================================================
    # Viewer Audit Log
    # ========================================================================

    @retry_on_locked()
    async def create_audit_log(
        self, viewer_id: int, username: str, endpoint: str, chat_id: int | None = None, ip_address: str | None = None
    ):
        """Log a viewer API access for audit trail."""
        async with self.db_manager.get_session() as session:
            entry = ViewerAuditLog(
                viewer_id=viewer_id,
                username=username,
                endpoint=endpoint,
                chat_id=chat_id,
                ip_address=ip_address,
            )
            session.add(entry)
            await session.commit()

    @retry_on_locked()
    async def get_audit_log(self, viewer_id: int | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """Get viewer activity audit log entries."""
        async with self.db_manager.get_session() as session:
            query = select(ViewerAuditLog).order_by(ViewerAuditLog.timestamp.desc())
            if viewer_id:
                query = query.where(ViewerAuditLog.viewer_id == viewer_id)
            query = query.offset(offset).limit(limit)
            result = await session.execute(query)
            return [
                {
                    "id": e.id,
                    "viewer_id": e.viewer_id,
                    "username": e.username,
                    "endpoint": e.endpoint,
                    "chat_id": e.chat_id,
                    "ip_address": e.ip_address,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                }
                for e in result.scalars().all()
            ]

    async def close(self) -> None:
        """Close database connections."""
        await self.db_manager.close()
