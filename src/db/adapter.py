"""
Async database adapter for Telegram Backup.

Provides all database operations using SQLAlchemy async.
This is a drop-in replacement for the old Database class.
"""

import asyncio
import glob
import hashlib
import json
import logging
import os
import secrets
import shutil
from collections.abc import Iterable
from datetime import datetime
from functools import wraps
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..message_utils import compute_directory_size, utcnow_naive
from .base import DatabaseManager
from .models import (
    AppSettings,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    MessageVersion,
    Metadata,
    Reaction,
    SyncStatus,
    User,
    ViewerAccount,
    ViewerAuditLog,
    ViewerSession,
    ViewerToken,
)

logger = logging.getLogger(__name__)


def _strip_tz(dt: datetime | None) -> datetime | None:
    """Strip timezone info from datetime for PostgreSQL compatibility."""
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _message_conflict_update_values(message_data: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Build update values for message upserts without undoing soft deletes."""
    update_values = dict(values)

    if not message_data.get("is_deleted"):
        update_values.pop("is_deleted", None)
        update_values.pop("deleted_at", None)
    elif "deleted_at" not in message_data:
        update_values.pop("deleted_at", None)
    if "is_pinned" not in message_data:
        update_values.pop("is_pinned", None)

    return update_values


def _datetime_hash_value(dt: datetime | None) -> str | None:
    dt = _strip_tz(dt)
    if dt is None:
        return None
    return dt.isoformat(timespec="microseconds")


def _message_version_hash(
    chat_id: int,
    message_id: int,
    text: str | None,
    date: datetime,
) -> str:
    # FROZEN CONTRACT: this exact encoding (key set, sort_keys, separators,
    # microsecond timespec) IS the dedup identity for message_versions rows via
    # the unique change_hash column. Changing any detail silently re-admits
    # duplicates of already-stored versions. Known accepted limit: repeated
    # no-edit_date edits that oscillate back to the same text reuse the same
    # fallback date and dedup into one row.
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "date": _datetime_hash_value(date),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _newer_edit_date(current: datetime | None, incoming: datetime | None) -> bool:
    current = _strip_tz(current)
    incoming = _strip_tz(incoming)
    if incoming is None:
        return False
    if current is None:
        return True
    return incoming > current


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

    def _message_values(self, message_data: dict[str, Any]) -> dict[str, Any]:
        return {
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
            "is_pinned": message_data.get("is_pinned", 0),
            "is_deleted": message_data.get("is_deleted", 0),
            "deleted_at": _strip_tz(message_data.get("deleted_at")),
        }

    def _insert_message_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return sqlite_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id", "chat_id"])
        return pg_insert(Message).values(**values).on_conflict_do_nothing(index_elements=["id", "chat_id"])

    def _insert_message_version_stmt(self, values: dict[str, Any]):
        if self._is_sqlite:
            return sqlite_insert(MessageVersion).values(**values).on_conflict_do_nothing(index_elements=["change_hash"])
        return pg_insert(MessageVersion).values(**values).on_conflict_do_nothing(index_elements=["change_hash"])

    async def _record_message_version(
        self,
        session,
        chat_id: int,
        message_id: int,
        text: str | None,
        date: datetime,
    ) -> bool:
        """Best-effort capture of a superseded text into message_versions.

        Versioning is plain text only — formatting/entity-only edits produce the
        same text and are intentionally not versioned. Runs inside a SAVEPOINT so
        an unexpected failure here can never poison the transaction or abort the
        message upsert/batch it belongs to (the expected duplicate case is already
        silenced by ON CONFLICT DO NOTHING on change_hash).
        """
        date = _strip_tz(date)
        if date is None:
            return False

        change_hash = _message_version_hash(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            date=date,
        )
        values = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "date": date,
            "change_hash": change_hash,
            "captured_at": utcnow_naive(),
        }
        try:
            async with session.begin_nested():
                result = await session.execute(self._insert_message_version_stmt(values))
        except Exception as e:
            logger.warning("Could not record a message version (%s); message update continues", type(e).__name__)
            return False
        return bool(result.rowcount)

    def _message_version_date(self, message: Message) -> datetime:
        return _strip_tz(message.edit_date) or _strip_tz(message.date)

    def _should_apply_upsert_text(self, existing: Message, values: dict[str, Any]) -> bool:
        """Decide whether a re-scanned/imported message may replace archived text.

        Truth table (upsert sources: backup re-scan, gap-fill, import):
        - same text            -> never bump edit_date (#219): Telegram bumps
                                  edit_date server-side for reaction-only changes,
                                  so treating an unchanged-text re-scan as an edit
                                  produced a phantom "edited" marker. Non-text
                                  metadata still refreshes via _pending_update_values.
        - empty -> non-empty   -> always fill (late hydration), even without an
                                  edit_date; caller preserves the existing edit_date.
        - differing text, no incoming edit_date -> refuse: an upsert source with no
                                  edit evidence must never clobber archived text.
        - differing text, incoming edit_date >= archived (or archived None) -> apply.
          ``>=`` (not ``>``) is deliberate: listener and backup can deliver the same
          edit with equal timestamps but the text seen later is the fresher fetch.
        """
        new_text = values.get("text")
        new_edit_date = _strip_tz(values.get("edit_date"))
        old_text = existing.text
        old_edit_date = _strip_tz(existing.edit_date)

        if old_text == new_text:
            # Same text -> never bump edit_date. Telegram bumps a message's
            # edit_date server-side when only reactions change (#219), so bumping
            # here would set edit_date with no version and surface a phantom
            # "edited" marker on re-scan/gap-fill/import too. Non-text metadata
            # still refreshes via _pending_update_values regardless of this gate;
            # reactions are reconciled by reconcile_reactions.
            return False
        if (old_text is None or old_text == "") and new_text not in (None, ""):
            return True
        if new_edit_date is None:
            return False
        if old_edit_date is None:
            return True
        if new_edit_date >= old_edit_date:
            return True
        return False

    def _should_apply_edit_text(self, existing: Message, new_text: str, edit_date: datetime | None) -> bool:
        """Decide whether a live edit event (listener/sync) may replace archived text.

        Differs from the upsert policy on the no-edit_date case: a live event with
        ``edit_date=None`` is applied only when the archived row was never edited —
        an already-edited row is never rolled over on date-less evidence (rare
        bot-API edits may hit this; conservative by design, covered by tests).
        """
        old_edit_date = _strip_tz(existing.edit_date)
        edit_date = _strip_tz(edit_date)

        if existing.text == new_text:
            # Text unchanged -> not a real text edit. Telegram bumps edit_date for
            # reaction-only changes (server-side; message.edit_hide is documented as
            # unreliable), so applying here would set edit_date with no version and
            # surface a phantom "edited" marker (#219). Reactions are captured by the
            # dedicated reaction path instead.
            return False
        if edit_date is None:
            return old_edit_date is None
        if old_edit_date is None:
            return True
        return edit_date >= old_edit_date

    async def _load_message_for_update(self, session, chat_id: int, message_id: int) -> Message | None:
        if self._is_sqlite:
            # SQLite has no row-level SELECT FOR UPDATE. A no-op write acquires the
            # transaction's write lock before we re-read and decide whether to update.
            await session.execute(
                update(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)).values(id=Message.id)
            )
            stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
        else:
            stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id)).with_for_update()

        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def _load_message_snapshot(self, session, chat_id: int, message_id: int) -> Message | None:
        """Plain lock-free read, used only for the fast-path no-change check."""
        stmt = select(Message).where(and_(Message.chat_id == chat_id, Message.id == message_id))
        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    def _pending_update_values(
        self, existing: Message, message_data: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        """Columns an upsert would actually change on ``existing`` (may be empty).

        Applies the text/edit_date gating policy, then drops every key whose value
        already matches the row, so re-scanning an unchanged message performs no
        write at all. Deliberate scope note: when text is withheld (older or
        no-evidence source), the remaining metadata (raw_data, reply_to_*, sender,
        …) still refreshes from the incoming payload — non-text fields stay
        last-writer-wins exactly as before versioning existed.
        """
        update_values = _message_conflict_update_values(message_data, values)
        if self._should_apply_upsert_text(existing, values):
            if values.get("edit_date") is None and existing.edit_date is not None:
                # Text change arrived without edit evidence (e.g. late hydration):
                # keep the existing edit_date rather than nulling it.
                update_values.pop("edit_date", None)
        else:
            update_values.pop("text", None)
            update_values.pop("edit_date", None)

        changed = {}
        for key, value in update_values.items():
            if key in ("id", "chat_id"):
                continue
            if getattr(existing, key) != value:
                changed[key] = value
        return changed

    async def _apply_existing_message_update(
        self,
        session,
        message_data: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        # Fast path: a lock-free read to detect the common re-scan case of a fully
        # unchanged message, so full re-backups don't pay the write lock + extra
        # statements per row. The definitive decision is re-made under the lock
        # below; skipping here is safe because a concurrent writer that changes the
        # row after our snapshot has, by definition, applied data at least as new
        # as ours.
        snapshot = await self._load_message_snapshot(session, values["chat_id"], values["id"])
        if snapshot is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return
        if not self._pending_update_values(snapshot, message_data, values):
            return

        existing = await self._load_message_for_update(session, values["chat_id"], values["id"])
        if existing is None:
            logger.debug("Upsert no-op: message row vanished during conflict resolution")
            return

        update_values = self._pending_update_values(existing, message_data, values)
        if not update_values:
            return
        if "text" in update_values:
            await self._record_message_version(
                session=session,
                chat_id=existing.chat_id,
                message_id=existing.id,
                text=existing.text,
                date=self._message_version_date(existing),
            )
        await session.execute(
            update(Message)
            .where(and_(Message.chat_id == values["chat_id"], Message.id == values["id"]))
            .values(**update_values)
        )

    async def _insert_or_update_message(self, session, message_data: dict[str, Any]) -> None:
        values = self._message_values(message_data)
        result = await session.execute(self._insert_message_stmt(values))
        if result.rowcount:
            return

        await self._apply_existing_message_update(session, message_data, values)

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

    async def get_migration_markers(self) -> list[tuple[int, int]]:
        """Return stored group→supergroup migration pointers (#228).

        Selects service messages whose ``raw_data.action_type`` is
        ``chat_migrate_to`` and returns ``(old_chat_id, new_marked_id)`` pairs,
        where ``new_marked_id`` is ``raw_data.migrate_to_id`` (already in marked
        ``-100…`` form, written by ``_process_message``). SELECT-only; used to
        reconcile scope for migrations that occurred while the archiver was
        offline (the dead basic group may no longer surface as a dialog).

        The ``LIKE`` clause is only a cheap prefilter — the authoritative match
        is the Python-side ``json.loads`` — so the result is portable across the
        SQLite and PostgreSQL backends without dialect-specific JSON operators.
        PII: ids are returned to the caller for scope reconciliation only.
        """
        markers: list[tuple[int, int]] = []
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                select(Message.chat_id, Message.raw_data).where(Message.raw_data.like('%"chat_migrate_to"%'))
            )
            for chat_id, raw in result.all():
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except ValueError, TypeError:
                    continue
                if data.get("action_type") != "chat_migrate_to":
                    continue
                new_id = data.get("migrate_to_id")
                if isinstance(new_id, int):
                    markers.append((chat_id, new_id))
        return markers

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
                "updated_at": utcnow_naive(),
            }

            # Build update set from only the fields explicitly provided in chat_data.
            # This prevents partial upserts (e.g. from the listener) from resetting
            # is_forum/is_archived to their defaults.
            update_set = {
                "updated_at": utcnow_naive(),
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
                escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                search_pattern = f"%{escaped}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern, escape="\\"),
                        Chat.first_name.ilike(search_pattern, escape="\\"),
                        Chat.last_name.ilike(search_pattern, escape="\\"),
                        Chat.username.ilike(search_pattern, escape="\\"),
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
                escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                search_pattern = f"%{escaped}%"
                stmt = stmt.where(
                    or_(
                        Chat.title.ilike(search_pattern, escape="\\"),
                        Chat.first_name.ilike(search_pattern, escape="\\"),
                        Chat.last_name.ilike(search_pattern, escape="\\"),
                        Chat.username.ilike(search_pattern, escape="\\"),
                    )
                )

            result = await session.execute(stmt)
            return result.scalar() or 0

    # ========== User Operations ==========

    @retry_on_locked()
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
                "updated_at": utcnow_naive(),
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
                        "updated_at": utcnow_naive(),
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
                        "updated_at": utcnow_naive(),
                    },
                )

            await session.execute(stmt)
            await session.commit()

    # ========== Message Operations ==========

    @retry_on_locked()
    async def insert_message(self, message_data: dict[str, Any]) -> None:
        """Insert a message record.

        v6.0.0: media_type, media_id, media_path removed - use insert_media() separately.
        """
        async with self.db_manager.async_session_factory() as session:
            await self._insert_or_update_message(session, message_data)
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
                await self._insert_or_update_message(session, m)

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
            # Exclude soft-deleted rows so sync doesn't re-check them. The is_(None) arm is
            # defensive (is_deleted is NOT NULL with server_default 0) and mirrors is_archived.
            stmt = select(Message.id, Message.edit_date).where(
                and_(Message.chat_id == chat_id, or_(Message.is_deleted == 0, Message.is_deleted.is_(None)))
            )
            result = await session.execute(stmt)
            return {row.id: row.edit_date for row in result}

    async def get_message_ids_since(self, chat_id: int, cutoff: datetime, limit: int) -> list[int]:
        """Return the newest message IDs in a chat dated at or after ``cutoff`` (#221).

        Used by the bounded reaction re-sweep to recover self-reactions Telegram
        never pushed to this session. Newest-first (highest id) and capped at
        ``limit`` so the caller re-checks the most recent window at a fixed cost.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id)
                .where(and_(Message.chat_id == chat_id, Message.date >= cutoff))
                .order_by(Message.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [row.id for row in result]

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

    @retry_on_locked()
    async def delete_message(self, chat_id: int, message_id: int) -> None:
        """Delete a specific message and its media."""
        async with self.db_manager.async_session_factory() as session:
            # Delete previous versions
            await session.execute(
                delete(MessageVersion).where(
                    and_(MessageVersion.chat_id == chat_id, MessageVersion.message_id == message_id)
                )
            )
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

    @retry_on_locked()
    async def mark_message_deleted(self, chat_id: int, message_id: int, deleted_at: datetime | None = None) -> None:
        """Mark a message as deleted on Telegram while keeping archive content."""
        deleted_at = _strip_tz(deleted_at) or utcnow_naive()
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(
                    is_deleted=1,
                    deleted_at=func.coalesce(Message.deleted_at, deleted_at),
                )
            )
            await session.commit()
            if result.rowcount:
                logger.debug(f"Marked message {message_id} as deleted")
            else:
                logger.debug(f"Soft-delete no-op: message {message_id} not in archive")

    async def resolve_message_chat_id(self, message_id: int) -> int | None:
        """
        Find which chat a message belongs to.

        Returns the chat_id if found in exactly one chat.
        Returns None if not found or ambiguous (same ID in multiple chats).
        Telegram message IDs are only unique within a chat.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(Message.chat_id).where(Message.id == message_id))
            chat_ids = [row[0] for row in result.fetchall()]

            if len(chat_ids) == 1:
                return chat_ids[0]
            if len(chat_ids) > 1:
                logger.warning(f"Message {message_id} found in {len(chat_ids)} chats, skipping ambiguous deletion")
            return None

    @retry_on_locked()
    async def update_message_text(
        self, chat_id: int, message_id: int, new_text: str, edit_date: datetime | None
    ) -> str:
        """Update a message's text and edit_date.

        Returns the outcome so callers can keep honest counters and only
        broadcast edits that actually changed the archive:
        ``"applied"`` | ``"noop"`` (already current / older evidence) |
        ``"not_found"`` (message not archived).
        """
        edit_date = _strip_tz(edit_date)
        async with self.db_manager.async_session_factory() as session:
            message = await self._load_message_for_update(session, chat_id, message_id)
            if message is None:
                logger.debug("Edit no-op: message not found in archive")
                return "not_found"

            if not self._should_apply_edit_text(message, new_text, edit_date):
                logger.debug("Edit no-op: message already current")
                return "noop"

            if message.text != new_text:
                await self._record_message_version(
                    session=session,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=message.text,
                    date=self._message_version_date(message),
                )
            await session.execute(
                update(Message)
                .where(and_(Message.chat_id == chat_id, Message.id == message_id))
                .values(text=new_text, edit_date=edit_date)
            )
            await session.commit()
            logger.debug("Updated archived message text")
            return "applied"

    async def sender_has_message_in_chats(self, sender_id: int, chat_ids: Iterable[int]) -> bool:
        """Return True if sender_id authored at least one message in any of chat_ids.

        SELECT-only membership probe used by the media ACL to decide whether a
        viewer may fetch a member's avatar: a user avatar is served iff the
        viewer can see a chat in which that user has spoken. Never logs ids.
        """
        chat_id_list = list(chat_ids)
        if not chat_id_list:
            return False
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Message.id)
                .where(and_(Message.sender_id == sender_id, Message.chat_id.in_(chat_id_list)))
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.first() is not None

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
        is_deleted = getattr(message, "is_deleted", 0)
        if not isinstance(is_deleted, int):
            is_deleted = 0
        deleted_at = getattr(message, "deleted_at", None)
        if not isinstance(deleted_at, datetime):
            deleted_at = None

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
            "is_deleted": int(is_deleted),
            "deleted_at": deleted_at,
        }

    def _message_version_to_dict(self, row: MessageVersion) -> dict[str, Any]:
        return {
            "chat_id": row.chat_id,
            "message_id": row.message_id,
            "text": row.text,
            "date": row.date,
        }

    async def get_message_versions(self, chat_id: int, message_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Get preserved previous text versions for a message."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(MessageVersion)
                .where(and_(MessageVersion.chat_id == chat_id, MessageVersion.message_id == message_id))
                .order_by(MessageVersion.date.desc(), MessageVersion.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [self._message_version_to_dict(row) for row in result.scalars()]

    def _message_versions_query(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ):
        # No join to messages: versions already carry (chat_id, message_id), and
        # referential integrity is owned by the explicit deletes in
        # delete_message / delete_chat_and_related_data.
        stmt = select(MessageVersion)

        conditions = []
        if chat_id is not None:
            conditions.append(MessageVersion.chat_id == chat_id)
        if start_date:
            conditions.append(MessageVersion.date >= start_date)
        if end_date:
            conditions.append(MessageVersion.date <= end_date)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        return stmt.order_by(
            MessageVersion.chat_id.asc(),
            MessageVersion.message_id.asc(),
            MessageVersion.date.asc(),
            MessageVersion.id.asc(),
        )

    async def get_message_versions_by_date_range(
        self,
        chat_id: int | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get previous message versions by version date/chat filter."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(self._message_versions_query(chat_id, start_date, end_date))
            return [self._message_version_to_dict(row) for row in result.scalars()]

    async def iter_message_versions_for_export(self, chat_id: int):
        """Stream a chat's message versions one by one (async generator).

        Mirrors get_messages_for_export so the export endpoint never
        materializes an entire edit history in memory.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.stream(self._message_versions_query(chat_id))
            async for row in result.scalars():
                yield self._message_version_to_dict(row)

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

    @retry_on_locked()
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
                "content_hash": media_data.get("content_hash"),
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

    async def find_media_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Find an existing downloaded media record with the given SHA-256 content hash."""
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Media).where(and_(Media.content_hash == content_hash, Media.downloaded == 1)).limit(1)
            result = await session.execute(stmt)
            media = result.scalar_one_or_none()
            if media is None:
                return None
            return {
                "file_path": media.file_path,
                "file_name": media.file_name,
                "content_hash": media.content_hash,
            }

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

    async def get_media_paginated(
        self,
        chat_id: int,
        media_types: list[str] | None = None,
        limit: int = 50,
        before_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get paginated media records for a chat with cursor-based pagination.

        Uses composite cursor (Message.date, Media.id) for deterministic ordering.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media, Message.date, User.first_name, User.last_name)
                .join(
                    Message,
                    and_(
                        Media.message_id == Message.id,
                        Media.chat_id == Message.chat_id,
                    ),
                )
                .outerjoin(User, Message.sender_id == User.id)
            )
            stmt = stmt.where(and_(Media.chat_id == chat_id, Media.downloaded == 1))

            if media_types:
                stmt = stmt.where(Media.type.in_(media_types))

            if before_id:
                cursor_stmt = (
                    select(Media.id, Message.date)
                    .join(
                        Message,
                        and_(Media.message_id == Message.id, Media.chat_id == Message.chat_id),
                    )
                    .where(Media.id == before_id)
                )
                cursor_result = await session.execute(cursor_stmt)
                cursor_row = cursor_result.one_or_none()
                if cursor_row is None:
                    return {"items": [], "has_more": False}
                cursor_media_id, cursor_date = cursor_row
                stmt = stmt.where(
                    or_(
                        Message.date < cursor_date,
                        and_(Message.date == cursor_date, Media.id < cursor_media_id),
                    )
                )

            stmt = stmt.order_by(Message.date.desc(), Media.id.desc())
            stmt = stmt.limit(limit + 1)
            result = await session.execute(stmt)
            rows = result.all()

            has_more = len(rows) > limit
            items = [
                {
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
                    "message_date": msg_date.isoformat() if msg_date else None,
                    "sender_name": f"{first_name or ''} {last_name or ''}".strip() or None,
                }
                for media, msg_date, first_name, last_name in rows[:limit]
            ]

            return {"items": items, "has_more": has_more}

    async def get_media_counts(self, chat_id: int) -> dict[str, int]:
        """
        Get count of downloaded media grouped by type for a chat.

        Args:
            chat_id: Chat identifier

        Returns:
            Dict mapping media type to count (only types with count > 0)
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Media.type, func.count())
                .where(and_(Media.chat_id == chat_id, Media.downloaded == 1))
                .group_by(Media.type)
            )
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

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

    async def iter_media_paths_for_repair(self, batch_size: int = 500):
        """Yield ``(id, file_path, file_name)`` batches for the #175 repair pass.

        Keyset-paginated on the primary key and projecting only the three columns
        the repair needs, so memory stays bounded regardless of table size. The
        full-table materialization in ``get_media_for_verification`` OOM-killed
        the 256m backup container on large archives; this streams instead.
        """
        last_id: str | None = None
        while True:
            async with self.db_manager.async_session_factory() as session:
                stmt = (
                    select(Media.id, Media.file_path, Media.file_name)
                    .where(or_(Media.downloaded == 1, Media.file_path.isnot(None)))
                    .order_by(Media.id)
                    .limit(batch_size)
                )
                if last_id is not None:
                    stmt = stmt.where(Media.id > last_id)
                rows = (await session.execute(stmt)).all()
            if not rows:
                return
            yield [{"id": r[0], "file_path": r[1], "file_name": r[2]} for r in rows]
            last_id = rows[-1][0]
            if len(rows) < batch_size:
                return

    async def get_pending_media_downloads(
        self,
        max_media_size_bytes: int | None = None,
        max_attempts: int | None = None,
        limit: int | None = 1000,
    ) -> list[dict[str, Any]]:
        """Get media records that failed to download and need retry.

        Returns records where downloaded=0 for downloadable media types
        (excludes contact/geo/poll which are metadata-only).
        Files exceeding max_media_size_bytes are excluded to prevent
        infinite retry of over-limit media. Records whose download_attempts have
        reached max_attempts are also excluded, so a permanently-failing file
        (e.g. an unwritable filename) can't be re-fetched every run forever (#212).

        ``limit`` bounds a single retry pass so this can't materialize the whole
        pending-media table in memory (the same OOM class fixed in
        ``iter_media_paths_for_repair``); pass ``None`` to restore the old
        unbounded behavior. Ordered by (download_attempts, id) so a bounded pass
        makes progress on the least-retried rows first.
        """
        async with self.db_manager.async_session_factory() as session:
            conditions = [
                Media.downloaded == 0,
                Media.type.notin_(["contact", "geo", "poll"]),
            ]
            if max_media_size_bytes is not None:
                conditions.append(or_(Media.file_size.is_(None), Media.file_size <= max_media_size_bytes))
            if max_attempts is not None:
                conditions.append(Media.download_attempts < max_attempts)
            where_clause = and_(*conditions)
            stmt = select(Media).where(where_clause).order_by(Media.download_attempts.asc(), Media.id.asc())
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()

            if limit is not None and len(rows) == limit:
                total_stmt = select(func.count(Media.id)).where(where_clause)
                total = (await session.execute(total_stmt)).scalar() or 0
                if total > limit:
                    logger.info("media retry: processing %d of %d pending", limit, total)

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
                    "download_attempts": m.download_attempts,
                }
                for m in rows
            ]

    @retry_on_locked()
    async def increment_media_download_attempts(self, media_id: str) -> None:
        """Bump the failed-download attempt counter for a media record (#212)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(Media.id == media_id)
                .values(download_attempts=func.coalesce(Media.download_attempts, 0) + 1)
            )
            await session.execute(stmt)
            await session.commit()

    async def mark_media_for_redownload(self, media_id: str) -> None:
        """Mark a media record as needing re-download.

        Also resets download_attempts so a row that previously hit the retry
        cap (#212) becomes eligible for the pending-download retry again.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                update(Media)
                .where(Media.id == media_id)
                .values(downloaded=0, file_path=None, download_date=None, download_attempts=0)
            )
            await session.execute(stmt)
            await session.commit()

    async def count_capped_media_downloads(self, max_attempts: int) -> int:
        """Count downloadable media permanently skipped after hitting the retry cap (#212).

        Lets the caller surface an aggregate signal instead of silently abandoning
        files — the very failure mode #212 was about.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(func.count(Media.id)).where(
                and_(
                    Media.downloaded == 0,
                    Media.type.notin_(["contact", "geo", "poll"]),
                    Media.download_attempts >= max_attempts,
                )
            )
            return (await session.execute(stmt)).scalar() or 0

    async def update_media_file_path(self, media_id: str, file_path: str) -> None:
        """Update the stored file_path for a single media record."""
        async with self.db_manager.async_session_factory() as session:
            stmt = update(Media).where(Media.id == media_id).values(file_path=file_path)
            await session.execute(stmt)
            await session.commit()

    # ========== Reaction Operations ==========

    async def _reset_reactions_sequence(self) -> None:
        """Reset the reactions table sequence to max(id) + 1."""
        async with self.db_manager.async_session_factory() as session:
            if not self.db_manager._is_sqlite:
                await session.execute(
                    text("SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false)")
                )
                await session.commit()
                logger.info("Reset reactions_id_seq sequence")

    async def get_reactions(self, message_id: int, chat_id: int) -> list[dict[str, Any]]:
        """Get all currently-active reactions for a message (excludes tombstoned)."""
        async with self.db_manager.async_session_factory() as session:
            stmt = (
                select(Reaction)
                .where(
                    and_(
                        Reaction.message_id == message_id,
                        Reaction.chat_id == chat_id,
                        Reaction.removed_at.is_(None),
                    )
                )
                .order_by(Reaction.emoji)
            )
            result = await session.execute(stmt)
            return [{"emoji": r.emoji, "user_id": r.user_id, "count": r.count} for r in result.scalars()]

    @retry_on_locked()
    async def reconcile_reactions(
        self,
        message_id: int,
        chat_id: int,
        observed: list[dict[str, Any]],
        *,
        mark_removed: bool = True,
        _after_seq_reset: bool = False,
    ) -> str:
        """Reconcile a message's reactions against a fresh FULL snapshot (#219).

        ``observed`` is the complete current per-emoji aggregate
        (``[{"emoji", "count"}]``, ``count`` authoritative) — the same shape the
        scheduled backup and the live UpdateMessageReactions handler both produce.
        Storage is intentionally EMOJI-AGGREGATE ONLY (one row per (message, chat,
        emoji), ``user_id`` NULL): per-user attribution is unsound on a user client
        (Telegram exposes only a tiny ``recent_reactions`` preview, so a reactor
        rolling off it is indistinguishable from a removal), so we never persist or
        rely on it. Unlike the legacy full delete-then-reinsert, this:

        - preserves ``created_at`` on the surviving row (first-seen survives
          re-scans — the reporter's histogram measured backup cadence precisely
          because the old path reset it every run);
        - keeps exactly one row per emoji via UPDATE, never ON CONFLICT (the
          ``uq_reaction`` constraint's nullable ``user_id`` is non-colliding in SQL,
          so an upsert on it would grow unbounded) — and collapses any legacy
          multi-row-per-emoji rows into that single aggregate;
        - reconciles removals INCLUDING to zero: an emoji absent from ``observed``
          is tombstoned (``removed_at``) when ``mark_removed`` (default), else
          deleted — this branch runs even when ``observed`` is empty;
        - is a no-op when the message is not archived (best-effort; never stubs a
          synthetic message row, which would render blank in the viewer and, with
          the FK having no CASCADE, raise on PostgreSQL).

        Returns ``"reconciled"`` | ``"noop"`` | ``"no_message"``.
        """
        async with self.db_manager.async_session_factory() as session:
            # Lock the parent message row for the whole reconcile so the live
            # listener and the scheduled backup can't both read "no rows" and insert
            # duplicate aggregate rows for the same emoji (uq_reaction is inert for
            # NULL user_id, so nothing else dedups them → inflated viewer counts).
            # This also guards the FK: reactions.fk_reaction_message has no CASCADE,
            # so a reaction for an unarchived message would raise on PostgreSQL.
            if await self._load_message_for_update(session, chat_id, message_id) is None:
                return "no_message"

            existing_rows = (
                (
                    await session.execute(
                        select(Reaction).where(and_(Reaction.message_id == message_id, Reaction.chat_id == chat_id))
                    )
                )
                .scalars()
                .all()
            )
            by_emoji: dict[str, list[Reaction]] = {}
            for r in existing_rows:
                by_emoji.setdefault(r.emoji, []).append(r)

            # Authoritative per-emoji counts from the snapshot (later duplicates of an
            # emoji are summed defensively; the extractor yields one entry per emoji).
            desired: dict[str, int] = {}
            for entry in observed:
                emoji = entry.get("emoji")
                if not emoji:
                    continue
                count = int(entry.get("count", 0) or 0)
                if count <= 0:
                    # A zero/negative count is "absent", not a live reaction — leave it
                    # out of `desired` so the emoji is tombstoned/removed below.
                    continue
                desired[emoji] = desired.get(emoji, 0) + count

            now = utcnow_naive()
            changed = False

            for emoji, count in desired.items():
                rows = by_emoji.get(emoji)
                if rows:
                    # Keep the earliest-seen row as the aggregate (preserves
                    # created_at); collapse any others (legacy per-user/dup rows).
                    rows_sorted = sorted(rows, key=lambda r: (r.created_at or now, r.id))
                    keep = rows_sorted[0]
                    if keep.count != count or keep.user_id is not None or keep.removed_at is not None:
                        keep.count = count
                        keep.user_id = None
                        keep.removed_at = None
                        changed = True
                    for extra in rows_sorted[1:]:
                        await session.delete(extra)
                        changed = True
                else:
                    session.add(
                        Reaction(
                            message_id=message_id,
                            chat_id=chat_id,
                            emoji=emoji,
                            user_id=None,
                            count=count,
                            created_at=now,
                            removed_at=None,
                        )
                    )
                    changed = True

            # Emojis no longer present: tombstone (retain) or delete. Collapse any
            # legacy multi-row group into one retained row so counts don't inflate.
            for emoji, rows in by_emoji.items():
                if emoji in desired:
                    continue
                if mark_removed:
                    rows_sorted = sorted(rows, key=lambda r: (r.created_at or now, r.id))
                    keep = rows_sorted[0]
                    total = sum(r.count or 0 for r in rows)
                    if keep.removed_at is None or keep.count != total or keep.user_id is not None:
                        keep.removed_at = keep.removed_at or now
                        keep.count = total
                        keep.user_id = None
                        changed = True
                    for extra in rows_sorted[1:]:
                        await session.delete(extra)
                        changed = True
                else:
                    for row in rows:
                        await session.delete(row)
                        changed = True

            if not changed:
                return "noop"

            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                # A brand-new reaction row can collide with a stale PG serial (the
                # long-standing reactions_id_seq drift). Reset the sequence and retry
                # the reconcile ONCE with the same snapshot so the authoritative state
                # is actually applied (returning early would drop it until the next
                # event). Log the error class only (never ids/emoji — PII).
                if not _after_seq_reset and ("duplicate key" in str(e).lower() or "unique" in str(e).lower()):
                    logger.warning("Reactions sequence out of sync during reconcile, resetting and retrying")
                    await self._reset_reactions_sequence()
                    return await self.reconcile_reactions(
                        message_id, chat_id, observed, mark_removed=mark_removed, _after_seq_reset=True
                    )
                raise
            return "reconciled"

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
            now = utcnow_naive()
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

    # ========== Gap Detection ==========

    async def detect_message_gaps(self, chat_id: int, threshold: int = 50) -> list[tuple[int, int, int]]:
        """Detect gaps in message ID sequences for a chat.

        Uses a SQL LAG() window function to find gaps larger than threshold.

        Returns:
            List of (gap_start_id, gap_end_id, gap_size) tuples where
            gap_start is the last message ID before the gap and
            gap_end is the first message ID after the gap.
        """
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT gap_start, gap_end, gap_size FROM (
                        SELECT
                            LAG(id) OVER (ORDER BY id) AS gap_start,
                            id AS gap_end,
                            id - LAG(id) OVER (ORDER BY id) AS gap_size
                        FROM messages
                        WHERE chat_id = :chat_id
                    ) gaps
                    WHERE gap_size > :threshold
                    ORDER BY gap_start
                    """
                ),
                {"chat_id": chat_id, "threshold": threshold},
            )
            return [(row[0], row[1], row[2]) for row in result.fetchall()]

    async def get_chats_with_messages(self) -> list[int]:
        """Get all chat IDs that exist in the chats table.

        Queries the chats table directly instead of scanning the messages table,
        which would be extremely slow on large databases.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(Chat.id)
            result = await session.execute(stmt)
            return [row[0] for row in result.fetchall()]

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

    async def calculate_and_store_statistics(self, storage_path: str | None = None) -> dict[str, Any]:
        """Calculate statistics and store in metadata (expensive, run daily).

        When ``storage_path`` is given, total media size reflects actual on-disk
        usage (``du`` semantics) via ``compute_directory_size`` so the figure
        tracks real disk consumption. The filesystem walk is a blocking scan, so
        it runs off the event loop (``asyncio.to_thread``) and outside the DB
        session. If the path is missing/unmounted (``du`` is 0 while media rows
        exist), or no path is given, it falls back to the DB snapshot
        ``SUM(media.file_size WHERE downloaded=1)``.
        """
        import asyncio
        import json

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

            # DB snapshot of downloaded media sizes — the fallback when on-disk
            # usage is unavailable (e.g. the backup volume is not mounted yet).
            db_total_size = (
                await session.execute(select(func.sum(Media.file_size)).where(Media.downloaded == 1))
            ).scalar() or 0

            # Per-chat statistics
            chat_stats_query = select(Message.chat_id, func.count(Message.id).label("message_count")).group_by(
                Message.chat_id
            )
            chat_stats_result = await session.execute(chat_stats_query)
            per_chat_stats = {row.chat_id: row.message_count for row in chat_stats_result}

        # Total media size: prefer actual on-disk usage. Run the blocking walk off
        # the event loop and after the session is closed so it never stalls other
        # requests or pins a DB connection.
        if storage_path is not None:
            total_size = await asyncio.to_thread(compute_directory_size, storage_path)
            if total_size == 0 and media_count > 0:
                # Path missing/unmounted: don't cache a spurious 0 over the last good value.
                logger.warning("On-disk storage size is 0 while media exists; using DB snapshot for storage stat")
                total_size = db_total_size
        else:
            total_size = db_total_size

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
        await self.set_metadata("stats_calculated_at", utcnow_naive().isoformat())

        return stats

    # ========== Delete Operations ==========

    async def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None) -> None:
        """Delete a chat and all related data."""
        async with self.db_manager.async_session_factory() as session:
            # Delete previous versions
            await session.execute(delete(MessageVersion).where(MessageVersion.chat_id == chat_id))
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
        after_id: int | None = None,
        topic_id: int | None = None,
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
                (or, without before_date, an id-only bound: rows with id < before_id)
            after_id: Cursor - get messages newer than this message ID (takes
                precedence over the other cursors; used for jump-to-message
                after-context). Response stays newest-first like every other mode.
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

            # v6.2.0: Filter by forum topic. NULL reply_to_top_id == General (id=1),
            # matching the coalesce in get_forum_topics counts.
            # Mirrored by messageBelongsToCurrentTopic in the viewer (GENERAL_TOPIC_ID).
            if topic_id is not None:
                stmt = stmt.where(func.coalesce(Message.reply_to_top_id, 1) == topic_id)

            if search:
                escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
                stmt = stmt.where(Message.text.ilike(f"%{escaped}%", escape="\\"))

            # Cursor-based pagination (preferred - O(1) performance)
            # Mirrored by the viewer (src/web/templates/index.html: compareMessagesDesc/messageCursor) — keep in sync.
            if after_id is not None:
                # Forward window (#213): the LIMIT must take the rows closest to the
                # target, so select oldest-first and reverse to newest-first below to
                # keep the response contract identical to every other mode. Ordering
                # by id (monotonic per chat) instead of date lets the (chat_id, id)
                # index satisfy both the bound and the sort — ordering by date here
                # forced a full per-chat scan plus a temp sort.
                stmt = stmt.where(Message.id > after_id)
                stmt = stmt.order_by(Message.id.asc()).limit(limit)
            elif before_date is not None:
                # Use composite cursor: (date, id) for deterministic ordering
                # Messages with same date are ordered by id DESC
                if before_id is not None:
                    stmt = stmt.where(
                        or_(Message.date < before_date, and_(Message.date == before_date, Message.id < before_id))
                    )
                else:
                    stmt = stmt.where(Message.date < before_date)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit)
            elif before_id is not None:
                # Lone before_id cursor (#213): per-chat Telegram message ids increase
                # monotonically over time, so an id-only bound is chronologically
                # correct within the chat scope. Without this branch a lone before_id
                # silently fell through to the offset path and returned the latest
                # page — the jump-to-message window was never fetched. Ordering by id
                # (not date) lets the (chat_id, id) index seek directly to the bound.
                stmt = stmt.where(Message.id < before_id)
                stmt = stmt.order_by(Message.id.desc()).limit(limit)
            else:
                # Offset-based pagination (legacy fallback)
                stmt = stmt.order_by(Message.date.desc(), Message.id.desc()).limit(limit).offset(offset)

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
                    except ValueError, TypeError:
                        logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
                        msg["raw_data"] = {}

                messages.append(msg)

            if after_id is not None:
                # Selected oldest-first for the LIMIT; restore the newest-first contract.
                messages.reverse()

            version_counts = {msg["id"]: 0 for msg in messages}
            page_message_ids = [msg["id"] for msg in messages]
            if page_message_ids:
                count_stmt = (
                    select(MessageVersion.message_id, func.count(MessageVersion.id).label("version_count"))
                    .where(
                        and_(
                            MessageVersion.chat_id == chat_id,
                            MessageVersion.message_id.in_(page_message_ids),
                        )
                    )
                    .group_by(MessageVersion.message_id)
                )
                count_result = await session.execute(count_stmt)
                version_counts.update({row.message_id: int(row.version_count or 0) for row in count_result})

            # Batch reply-text backfill: one query for the whole page instead of one
            # SELECT per reply row (previously up to `limit` round-trips per page).
            reply_ids_needed = {
                msg["reply_to_msg_id"]
                for msg in messages
                if msg.get("reply_to_msg_id") and not msg.get("reply_to_text")
            }
            reply_texts: dict[int, str] = {}
            if reply_ids_needed:
                reply_stmt = select(Message.id, Message.text).where(
                    and_(Message.chat_id == chat_id, Message.id.in_(reply_ids_needed))
                )
                reply_result = await session.execute(reply_stmt)
                reply_texts = {row.id: row.text for row in reply_result if row.text}

            # Batch reactions: one query for the whole page instead of one
            # get_reactions() call per message. Ties within the same emoji are
            # broken by Reaction.id to match get_reactions' de-facto row order.
            reactions_by_message: dict[int, list[dict[str, Any]]] = {mid: [] for mid in page_message_ids}
            if page_message_ids:
                reactions_stmt = (
                    select(Reaction)
                    .where(
                        and_(
                            Reaction.chat_id == chat_id,
                            Reaction.message_id.in_(page_message_ids),
                            # Tombstoned (retain-on-removal) reactions are archived
                            # history, not part of the live displayed count (#219).
                            Reaction.removed_at.is_(None),
                        )
                    )
                    .order_by(Reaction.message_id, Reaction.emoji, Reaction.id)
                )
                reactions_result = await session.execute(reactions_stmt)
                for r in reactions_result.scalars():
                    reactions_by_message[r.message_id].append(
                        {"emoji": r.emoji, "user_id": r.user_id, "count": r.count}
                    )

            for msg in messages:
                msg["version_count"] = version_counts.get(msg["id"], 0)

                if msg.get("reply_to_msg_id") and not msg.get("reply_to_text"):
                    reply_text = reply_texts.get(msg["reply_to_msg_id"])
                    if reply_text:
                        msg["reply_to_text"] = reply_text[:100]

                reactions_by_emoji = {}
                for reaction in reactions_by_message.get(msg["id"], []):
                    emoji = reaction["emoji"]
                    if emoji not in reactions_by_emoji:
                        reactions_by_emoji[emoji] = {"emoji": emoji, "count": 0, "user_ids": []}
                    reactions_by_emoji[emoji]["count"] += reaction.get("count", 1)
                    if reaction.get("user_id"):
                        reactions_by_emoji[emoji]["user_ids"].append(reaction["user_id"])
                msg["reactions"] = list(reactions_by_emoji.values())

            return messages

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
                except ValueError, TypeError:
                    logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
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
                    except ValueError, TypeError:
                        logger.debug("Malformed raw_data JSON for a message row; substituting empty dict")
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
                "updated_at": utcnow_naive(),
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
                "updated_at": utcnow_naive(),
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
                "updated_at": utcnow_naive(),
            }

            update_set = {
                "title": values["title"],
                "emoticon": values["emoticon"],
                "sort_order": values["sort_order"],
                "updated_at": utcnow_naive(),
            }

            if self._is_sqlite:
                stmt = sqlite_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)
            else:
                stmt = pg_insert(ChatFolder).values(**values)
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_set)

            await session.execute(stmt)
            await session.commit()

    # Flag-based folders can now resolve to very large member sets, so the
    # existence check is chunked to stay well under driver bind-parameter caps
    # (SQLite ~32766, PostgreSQL 65535).
    _FOLDER_MEMBER_CHUNK = 500

    @retry_on_locked()
    async def sync_folder_members(self, folder_id: int, chat_ids: list[int]) -> None:
        """Sync folder membership: replace all members for a folder."""
        async with self.db_manager.async_session_factory() as session:
            # Delete existing members
            await session.execute(delete(ChatFolderMember).where(ChatFolderMember.folder_id == folder_id))

            # Insert new members (only for chats that exist in our DB)
            if chat_ids:
                # Dedup while preserving order; verify existence in bounded chunks.
                unique_ids = list(dict.fromkeys(chat_ids))
                existing_ids: set[int] = set()
                for i in range(0, len(unique_ids), self._FOLDER_MEMBER_CHUNK):
                    chunk = unique_ids[i : i + self._FOLDER_MEMBER_CHUNK]
                    result = await session.execute(select(Chat.id).where(Chat.id.in_(chunk)))
                    existing_ids.update(row[0] for row in result)

                for cid in unique_ids:
                    if cid in existing_ids:
                        session.add(ChatFolderMember(folder_id=folder_id, chat_id=cid))

            await session.commit()

    async def get_all_folders(self, allowed_chat_ids: set[int] | None = None) -> list[dict[str, Any]]:
        """Get all chat folders with their chat counts.

        Only folders that contain at least one backed-up (and, for restricted
        viewers, accessible) chat are returned. The viewer reflects the archive,
        not the full Telegram account: a folder whose chats were all excluded
        from backup — or that is empty on Telegram — would otherwise show as an
        empty filter tab that returns nothing when clicked (#208). Folder
        membership is already limited to chats present in our DB by
        sync_folder_members, so a zero count means "nothing archived here".

        Args:
            allowed_chat_ids: If set, only count chats the user can access.
        """
        async with self.db_manager.async_session_factory() as session:
            count_q = select(ChatFolderMember.folder_id, func.count(ChatFolderMember.chat_id).label("chat_count"))
            if allowed_chat_ids is not None:
                count_q = count_q.where(ChatFolderMember.chat_id.in_(allowed_chat_ids))
            count_subq = count_q.group_by(ChatFolderMember.folder_id).subquery()

            stmt = (
                select(ChatFolder, count_subq.c.chat_count)
                .outerjoin(count_subq, ChatFolder.id == count_subq.c.folder_id)
                .order_by(ChatFolder.sort_order, ChatFolder.title)
            )

            result = await session.execute(stmt)
            folders = []
            for row in result:
                folder = row.ChatFolder
                count = row.chat_count or 0
                # Hide folders with no backed-up chats (empty tabs help no one)
                if count == 0:
                    continue
                folders.append(
                    {
                        "id": folder.id,
                        "title": folder.title,
                        "emoticon": folder.emoticon,
                        "sort_order": folder.sort_order,
                        "chat_count": count,
                    }
                )
            return folders

    async def get_chats_for_folder_resolution(self) -> list[dict[str, Any]]:
        """Return every archived chat with the facts needed to evaluate a folder's
        category flags: id, type, whether it is a bot, and archived state.

        Bot-ness is only meaningful for private chats and is read from the users
        table (chats store bots as type ``private``). The join is on ``User.id ==
        Chat.id`` — a private chat's id is the positive user id, while group and
        channel ids are negative/marked and can never collide with a user id, so
        they always resolve to ``is_bot = 0``.
        """
        async with self.db_manager.async_session_factory() as session:
            stmt = select(
                Chat.id,
                Chat.type,
                Chat.is_archived,
                func.coalesce(User.is_bot, 0).label("is_bot"),
            ).outerjoin(User, User.id == Chat.id)
            result = await session.execute(stmt)
            return [
                {
                    "id": row.id,
                    "type": row.type,
                    "is_bot": bool(row.is_bot),
                    "is_archived": bool(row.is_archived),
                }
                for row in result
            ]

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
    # Viewer Account Management (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_account(
        self,
        username: str,
        password_hash: str,
        salt: str,
        allowed_chat_ids: str | None = None,
        created_by: str | None = None,
        is_active: int = 1,
        no_download: int = 0,
    ) -> dict[str, Any]:
        """Create a new viewer account. Returns the created account dict."""
        async with self.db_manager.async_session_factory() as session:
            account = ViewerAccount(
                username=username,
                password_hash=password_hash,
                salt=salt,
                allowed_chat_ids=allowed_chat_ids,
                created_by=created_by,
                is_active=is_active,
                no_download=no_download,
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    async def get_viewer_account(self, account_id: int) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_viewer_by_username(self, username: str) -> dict[str, Any] | None:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.username == username))
            account = result.scalar_one_or_none()
            return self._viewer_account_to_dict(account) if account else None

    async def get_all_viewer_accounts(self) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).order_by(ViewerAccount.created_at.desc()))
            return [self._viewer_account_to_dict(a) for a in result.scalars().all()]

    @retry_on_locked()
    async def update_viewer_account(self, account_id: int, **kwargs) -> dict[str, Any] | None:
        """Update viewer account fields. Returns updated account or None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerAccount).where(ViewerAccount.id == account_id))
            account = result.scalar_one_or_none()
            if not account:
                return None
            for key, value in kwargs.items():
                if hasattr(account, key):
                    setattr(account, key, value)
            account.updated_at = utcnow_naive()
            await session.commit()
            await session.refresh(account)
            return self._viewer_account_to_dict(account)

    @retry_on_locked()
    async def delete_viewer_account(self, account_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerAccount).where(ViewerAccount.id == account_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_account_to_dict(account: ViewerAccount) -> dict[str, Any]:
        return {
            "id": account.id,
            "username": account.username,
            "password_hash": account.password_hash,
            "salt": account.salt,
            "allowed_chat_ids": account.allowed_chat_ids,
            "is_active": account.is_active,
            "no_download": account.no_download,
            "created_by": account.created_by,
            "created_at": account.created_at.isoformat() if account.created_at else None,
            "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        }

    # ========================================================================
    # Viewer Audit Log (v7.0.0)
    # ========================================================================

    @retry_on_locked()
    async def create_audit_log(
        self,
        username: str,
        role: str,
        action: str,
        endpoint: str | None = None,
        chat_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        async with self.db_manager.async_session_factory() as session:
            entry = ViewerAuditLog(
                username=username,
                role=role,
                action=action,
                endpoint=endpoint,
                chat_id=chat_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.add(entry)
            await session.commit()

    async def get_audit_logs(
        self, limit: int = 100, offset: int = 0, username: str | None = None, action: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.db_manager.async_session_factory() as session:
            stmt = select(ViewerAuditLog).order_by(ViewerAuditLog.created_at.desc())
            if username:
                stmt = stmt.where(ViewerAuditLog.username == username)
            if action:
                stmt = stmt.where(ViewerAuditLog.action.startswith(action))
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [
                {
                    "id": log.id,
                    "username": log.username,
                    "role": log.role,
                    "action": log.action,
                    "endpoint": log.endpoint,
                    "chat_id": log.chat_id,
                    "ip_address": log.ip_address,
                    "user_agent": log.user_agent,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in result.scalars().all()
            ]

    # ========================================================================
    # Viewer Sessions (v7.1.0 - persistent sessions)
    # ========================================================================

    @retry_on_locked()
    async def save_session(
        self,
        token: str,
        username: str,
        role: str,
        allowed_chat_ids: str | None,
        created_at: float,
        last_accessed: float,
        no_download: int = 0,
        source_token_id: int | None = None,
    ) -> None:
        """Save or update a session in the database."""
        async with self.db_manager.async_session_factory() as session:
            values = {
                "token": token,
                "username": username,
                "role": role,
                "allowed_chat_ids": allowed_chat_ids,
                "no_download": no_download,
                "source_token_id": source_token_id,
                "created_at": created_at,
                "last_accessed": last_accessed,
            }
            if self._is_sqlite:
                stmt = sqlite_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            else:
                stmt = pg_insert(ViewerSession).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["token"],
                    set_={"last_accessed": last_accessed},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_session(self, token: str) -> dict[str, Any] | None:
        """Get a session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession).where(ViewerSession.token == token))
            row = result.scalar_one_or_none()
            return self._viewer_session_to_dict(row) if row else None

    async def load_all_sessions(self) -> list[dict[str, Any]]:
        """Load all sessions from the database (used on startup)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerSession))
            return [self._viewer_session_to_dict(s) for s in result.scalars().all()]

    @retry_on_locked()
    async def delete_session(self, token: str) -> bool:
        """Delete a single session by token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.token == token))
            await session.commit()
            return result.rowcount > 0

    @retry_on_locked()
    async def delete_user_sessions(self, username: str) -> int:
        """Delete all sessions for a given username. Returns count deleted."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.username == username))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def cleanup_expired_sessions(self, max_age_seconds: float) -> int:
        """Delete all expired sessions. Returns count deleted."""
        import time

        cutoff = time.time() - max_age_seconds
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.created_at < cutoff))
            await session.commit()
            return result.rowcount

    @retry_on_locked()
    async def delete_sessions_by_source_token_id(self, token_id: int) -> int:
        """Delete all sessions created from a specific share token."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerSession).where(ViewerSession.source_token_id == token_id))
            await session.commit()
            return result.rowcount

    @staticmethod
    def _viewer_session_to_dict(row: ViewerSession) -> dict[str, Any]:
        return {
            "token": row.token,
            "username": row.username,
            "role": row.role,
            "allowed_chat_ids": row.allowed_chat_ids,
            "no_download": row.no_download,
            "source_token_id": row.source_token_id,
            "created_at": row.created_at,
            "last_accessed": row.last_accessed,
        }

    # ========================================================================
    # Viewer Tokens (v7.2.0 - share tokens)
    # ========================================================================

    @retry_on_locked()
    async def create_viewer_token(
        self,
        label: str | None,
        token_hash: str,
        token_salt: str,
        created_by: str,
        allowed_chat_ids: str,
        no_download: int = 0,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a new share token. Returns the created token dict."""
        async with self.db_manager.async_session_factory() as session:
            token = ViewerToken(
                label=label,
                token_hash=token_hash,
                token_salt=token_salt,
                created_by=created_by,
                allowed_chat_ids=allowed_chat_ids,
                no_download=no_download,
                expires_at=expires_at,
            )
            session.add(token)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    async def get_all_viewer_tokens(self) -> list[dict[str, Any]]:
        """Get all tokens (for admin panel)."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).order_by(ViewerToken.created_at.desc()))
            return [self._viewer_token_to_dict(t) for t in result.scalars().all()]

    async def verify_viewer_token(self, plaintext_token: str) -> dict[str, Any] | None:
        """Verify a plaintext token against stored hashes. Returns token dict or None."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.is_revoked == 0))
            for record in result.scalars().all():
                if record.expires_at and record.expires_at < utcnow_naive():
                    continue
                computed = hashlib.pbkdf2_hmac(
                    "sha256", plaintext_token.encode(), bytes.fromhex(record.token_salt), 600_000
                ).hex()
                if secrets.compare_digest(computed, record.token_hash):
                    record.last_used_at = utcnow_naive()
                    record.use_count = (record.use_count or 0) + 1
                    await session.commit()
                    return self._viewer_token_to_dict(record)
            return None

    @retry_on_locked()
    async def update_viewer_token(self, token_id: int, **kwargs) -> dict[str, Any] | None:
        """Update token fields. Supports: label, allowed_chat_ids, is_revoked, no_download."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(ViewerToken).where(ViewerToken.id == token_id))
            token = result.scalar_one_or_none()
            if not token:
                return None
            allowed_fields = {"label", "allowed_chat_ids", "is_revoked", "no_download"}
            for key, value in kwargs.items():
                if key in allowed_fields:
                    setattr(token, key, value)
            await session.commit()
            await session.refresh(token)
            return self._viewer_token_to_dict(token)

    @retry_on_locked()
    async def delete_viewer_token(self, token_id: int) -> bool:
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(delete(ViewerToken).where(ViewerToken.id == token_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    def _viewer_token_to_dict(token: ViewerToken) -> dict[str, Any]:
        return {
            "id": token.id,
            "label": token.label,
            "token_hash": token.token_hash,
            "token_salt": token.token_salt,
            "created_by": token.created_by,
            "allowed_chat_ids": token.allowed_chat_ids,
            "is_revoked": token.is_revoked,
            "no_download": token.no_download,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "use_count": token.use_count,
            "created_at": token.created_at.isoformat() if token.created_at else None,
        }

    # ========================================================================
    # App Settings (v7.2.0 - key-value store)
    # ========================================================================

    @retry_on_locked()
    async def set_setting(self, key: str, value: str) -> None:
        """Set a key-value setting (upsert)."""
        async with self.db_manager.async_session_factory() as session:
            if self._is_sqlite:
                stmt = sqlite_insert(AppSettings).values(key=key, value=value, updated_at=utcnow_naive())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": utcnow_naive()},
                )
            else:
                stmt = pg_insert(AppSettings).values(key=key, value=value, updated_at=utcnow_naive())
                stmt = stmt.on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "updated_at": utcnow_naive()},
                )
            await session.execute(stmt)
            await session.commit()

    async def get_setting(self, key: str) -> str | None:
        """Get a setting value by key. Returns None if not found."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings).where(AppSettings.key == key))
            row = result.scalar_one_or_none()
            return row.value if row else None

    async def get_all_settings(self) -> dict[str, str]:
        """Get all settings as a dict."""
        async with self.db_manager.async_session_factory() as session:
            result = await session.execute(select(AppSettings))
            return {row.key: row.value for row in result.scalars().all()}

    async def close(self) -> None:
        """Close database connections."""
        await self.db_manager.close()
