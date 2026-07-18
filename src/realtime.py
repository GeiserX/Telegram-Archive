"""
Real-time notification module for Telegram Backup.

Auto-detects database type and uses appropriate mechanism:
- PostgreSQL: LISTEN/NOTIFY
- SQLite: HTTP webhook (internal endpoint)

This module provides a unified interface for pushing real-time updates
from the backup/listener components to the viewer.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def _json_serializer(obj):
    """Custom JSON serializer for datetime objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _database_url_uses_postgres(database_url: str) -> bool:
    return database_url.startswith(("postgresql://", "postgresql+asyncpg://", "postgres://"))


def _env_uses_postgres() -> bool:
    database_url = os.getenv("DATABASE_URL", "").lower()
    if database_url:
        return _database_url_uses_postgres(database_url)
    db_type = os.getenv("DB_TYPE", "sqlite").lower()
    return db_type in ("postgresql", "postgres")


class NotificationType(str, Enum):
    """Types of real-time notifications."""

    NEW_MESSAGE = "new_message"
    EDIT = "edit"
    DELETE = "delete"
    CHAT_UPDATE = "chat_update"
    PIN = "pin"
    REACTION = "reaction"


def _truncate_notify_data(data: dict, max_text: int = 500) -> dict:
    """Truncate large string fields in notification data to stay under PostgreSQL's 8KB NOTIFY limit.

    Handles both ``data["message"]["text"]`` (new_message) and ``data["new_text"]`` (edit)
    paths. Returns a shallow-copied dict so the caller's original is not mutated.
    """
    truncated = False

    # new_message path: data["message"]["text"]
    if "message" in data and isinstance(data["message"], dict):
        msg = data["message"]
        if "text" in msg and msg.get("text") and len(msg["text"]) > max_text:
            data = data.copy()
            data["message"] = msg.copy()
            data["message"]["text"] = msg["text"][:max_text] + "…"
            truncated = True

    # edit path: data["new_text"]
    if "new_text" in data and data.get("new_text") and len(data["new_text"]) > max_text:
        if not truncated:
            data = data.copy()
        data["new_text"] = data["new_text"][:max_text] + "…"

    return data


class RealtimeNotifier:
    """
    Unified real-time notification sender.
    Auto-detects database type and uses appropriate mechanism.
    """

    def __init__(self, db_manager=None):
        """
        Initialize notifier.

        Args:
            db_manager: Optional DatabaseManager instance. If not provided,
                       will auto-detect from environment.
        """
        self._db_manager = db_manager
        self._is_postgresql = False
        self._http_endpoint: str | None = None
        self._pg_connection = None
        self._initialized = False

    async def init(self):
        """Initialize the notifier based on database type."""
        if self._initialized:
            return

        # Detect database type
        if self._db_manager:
            self._is_postgresql = not self._db_manager._is_sqlite
        else:
            self._is_postgresql = _env_uses_postgres()

        if self._is_postgresql:
            logger.info("Realtime notifier: Using PostgreSQL LISTEN/NOTIFY")
        else:
            # SQLite - use HTTP webhook
            viewer_host = os.getenv("VIEWER_HOST", "localhost")
            viewer_port = os.getenv("VIEWER_PORT", "8080")
            self._http_endpoint = f"http://{viewer_host}:{viewer_port}/internal/push"
            logger.info(f"Realtime notifier: Using HTTP webhook ({self._http_endpoint})")

        self._initialized = True

    async def notify(self, notification_type: NotificationType, chat_id: int, data: dict):
        """
        Send a notification.

        Args:
            notification_type: Type of notification (new_message, edit, delete, etc.)
            chat_id: The chat ID associated with the notification
            data: Additional data (message content, etc.)
        """
        if not self._initialized:
            await self.init()

        # Truncate large string fields to stay under PostgreSQL's 8KB NOTIFY limit.
        # The viewer fetches full content via API, so truncation is fine.
        _MAX_NOTIFY_TEXT = 500
        data = _truncate_notify_data(data, _MAX_NOTIFY_TEXT)

        payload = {"type": notification_type.value, "chat_id": chat_id, "data": data}

        try:
            if self._is_postgresql:
                await self._notify_postgres(payload)
            else:
                await self._notify_http(payload)
        except Exception as e:
            # Don't fail the main operation if notification fails
            logger.warning(f"Failed to send realtime notification: {e}")

    async def _notify_postgres(self, payload: dict):
        """Send notification via PostgreSQL NOTIFY.

        Uses ``pg_notify(channel, payload)`` with bound parameters so the
        payload never becomes part of the SQL text. The previous
        ``NOTIFY telegram_updates, '<json>'`` form broke whenever the JSON
        contained tokens like ``$1`` or ``$D`` — asyncpg parses those as
        positional placeholders in the raw SQL string.
        """
        if not self._db_manager:
            return

        async with self._db_manager.async_session_factory() as session:
            from sqlalchemy import text

            payload_json = json.dumps(payload, default=_json_serializer)
            await session.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": "telegram_updates", "payload": payload_json},
            )
            await session.commit()

    async def _notify_http(self, payload: dict):
        """Send notification via HTTP webhook."""
        if not self._http_endpoint:
            return

        headers: dict[str, str] = {}
        push_secret = os.getenv("INTERNAL_PUSH_SECRET")
        if push_secret:
            headers["Authorization"] = f"Bearer {push_secret}"

        try:
            import aiohttp

            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self._http_endpoint, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response,
            ):
                if response.status != 200:
                    logger.warning(f"HTTP notification returned {response.status}")
        except ImportError:
            # aiohttp not available, try httpx
            try:
                import httpx

                async with httpx.AsyncClient() as client:
                    await client.post(self._http_endpoint, json=payload, headers=headers, timeout=5)
            except ImportError:
                logger.warning("Neither aiohttp nor httpx available for HTTP notifications")
        except Exception as e:
            logger.warning(f"HTTP notification failed: {e}")


class RealtimeListener:
    """
    Unified real-time notification receiver.
    Auto-detects database type and listens via appropriate mechanism.
    """

    def __init__(self, db_manager=None, callback: Callable[[dict], Any] = None):
        """
        Initialize listener.

        Args:
            db_manager: Optional DatabaseManager instance.
            callback: Async function to call when notification received.
        """
        self._db_manager = db_manager
        self._callback = callback
        self._is_postgresql = False
        self._running = False
        self._task: asyncio.Task | None = None
        # Keeps references to in-flight callback tasks so they aren't
        # garbage-collected mid-run and so their exceptions are retrieved
        # (an un-referenced task's exception is otherwise silently dropped).
        self._callback_tasks: set[asyncio.Task] = set()

    async def init(self):
        """Initialize and detect database type."""
        if self._db_manager:
            self._is_postgresql = not self._db_manager._is_sqlite
        else:
            self._is_postgresql = _env_uses_postgres()

        if self._is_postgresql:
            logger.info("Realtime listener: Using PostgreSQL LISTEN")
        else:
            logger.info("Realtime listener: Using HTTP endpoint (SQLite mode)")

    async def start(self):
        """Start listening for notifications (PostgreSQL only)."""
        if not self._is_postgresql:
            # SQLite uses HTTP endpoint, handled by FastAPI route
            return

        self._running = True
        self._task = asyncio.create_task(self._listen_postgres())
        logger.info("PostgreSQL LISTEN started")

    async def stop(self):
        """Stop listening."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _listen_postgres(self):
        """Listen for PostgreSQL notifications."""
        import asyncpg

        # Get connection string from db_manager
        url = self._db_manager.database_url
        # Convert SQLAlchemy URL to asyncpg format
        url = url.replace("postgresql+asyncpg://", "postgresql://")

        while self._running:
            conn = None
            try:
                conn = await asyncpg.connect(url)
                await conn.add_listener("telegram_updates", self._pg_callback)
                logger.info("PostgreSQL LISTEN connected")

                # Keep connection alive
                while self._running:
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"PostgreSQL LISTEN error: {e}")
                await asyncio.sleep(5)  # Retry after 5 seconds
            finally:
                # Always tear down the connection, even when CancelledError or
                # another exception interrupts the "keep connection alive" loop --
                # otherwise a flapping DB leaks one connection per retry.
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.remove_listener("telegram_updates", self._pg_callback)
                    with contextlib.suppress(Exception):
                        await conn.close()

    def _pg_callback(self, connection, pid, channel, payload):
        """Handle PostgreSQL notification."""
        if not self._callback:
            return

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            # Never log the raw payload -- it may contain message content (PII rule).
            logger.warning(f"Invalid JSON in notification: {type(e).__name__}: {e}")
            return

        # Keep a reference so the task isn't GC'd mid-flight, and retrieve its
        # exception via the done callback instead of letting it vanish silently.
        task = asyncio.create_task(self._callback(data))
        self._callback_tasks.add(task)
        task.add_done_callback(self._on_callback_task_done)

    def _on_callback_task_done(self, task: asyncio.Task) -> None:
        """Clean up a finished callback task and surface any exception it raised."""
        self._callback_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Never log notification payload/data here (PII rule) -- exception class/message only.
            logger.warning(f"Realtime callback failed: {type(exc).__name__}: {exc}")

    async def handle_http_push(self, payload: dict):
        """Handle HTTP push notification (for SQLite mode)."""
        if self._callback:
            await self._callback(payload)
