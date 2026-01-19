"""
Real-time notification module for Telegram Backup.

Auto-detects database type and uses appropriate mechanism:
- PostgreSQL: LISTEN/NOTIFY
- SQLite: HTTP webhook (internal endpoint)

This module provides a unified interface for pushing real-time updates
from the backup/listener components to the viewer.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional, Callable, Any
from enum import Enum

logger = logging.getLogger(__name__)


def _json_serializer(obj):
    """Custom JSON serializer for datetime objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class NotificationType(str, Enum):
    """Types of real-time notifications."""
    NEW_MESSAGE = "new_message"
    EDIT = "edit"
    DELETE = "delete"
    CHAT_UPDATE = "chat_update"


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
        self._http_endpoint: Optional[str] = None
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
            db_type = os.getenv('DB_TYPE', 'sqlite').lower()
            self._is_postgresql = db_type in ('postgresql', 'postgres')
        
        if self._is_postgresql:
            logger.info("Realtime notifier: Using PostgreSQL LISTEN/NOTIFY")
        else:
            # SQLite - use HTTP webhook
            viewer_host = os.getenv('VIEWER_HOST', 'localhost')
            viewer_port = os.getenv('VIEWER_PORT', '8080')
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
        
        # Truncate message text to avoid PostgreSQL NOTIFY 8KB limit
        # The viewer fetches full content via API, so truncation is fine
        if 'message' in data and isinstance(data['message'], dict):
            msg = data['message']
            if 'text' in msg and msg.get('text') and len(msg['text']) > 500:
                data = data.copy()
                data['message'] = msg.copy()
                data['message']['text'] = msg['text'][:500] + '…'
        
        payload = {
            "type": notification_type.value,
            "chat_id": chat_id,
            "data": data
        }
        
        try:
            if self._is_postgresql:
                await self._notify_postgres(payload)
            else:
                await self._notify_http(payload)
        except Exception as e:
            # Don't fail the main operation if notification fails
            logger.warning(f"Failed to send realtime notification: {e}")
    
    async def _notify_postgres(self, payload: dict):
        """Send notification via PostgreSQL NOTIFY."""
        if not self._db_manager:
            return
        
        async with self._db_manager.async_session_factory() as session:
            from sqlalchemy import text
            # Escape the JSON payload (handle datetime objects)
            payload_json = json.dumps(payload, default=_json_serializer).replace("'", "''")
            await session.execute(
                text(f"NOTIFY telegram_updates, '{payload_json}'")
            )
            await session.commit()
    
    async def _notify_http(self, payload: dict):
        """Send notification via HTTP webhook."""
        if not self._http_endpoint:
            return
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._http_endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        logger.warning(f"HTTP notification returned {response.status}")
        except ImportError:
            # aiohttp not available, try httpx
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.post(self._http_endpoint, json=payload, timeout=5)
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
        self._task: Optional[asyncio.Task] = None
    
    async def init(self):
        """Initialize and detect database type."""
        if self._db_manager:
            self._is_postgresql = not self._db_manager._is_sqlite
        else:
            db_type = os.getenv('DB_TYPE', 'sqlite').lower()
            self._is_postgresql = db_type in ('postgresql', 'postgres')
        
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
        url = url.replace('postgresql+asyncpg://', 'postgresql://')
        
        while self._running:
            try:
                conn = await asyncpg.connect(url)
                await conn.add_listener('telegram_updates', self._pg_callback)
                logger.info("PostgreSQL LISTEN connected")
                
                # Keep connection alive
                while self._running:
                    await asyncio.sleep(1)
                
                await conn.remove_listener('telegram_updates', self._pg_callback)
                await conn.close()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"PostgreSQL LISTEN error: {e}")
                await asyncio.sleep(5)  # Retry after 5 seconds
    
    def _pg_callback(self, connection, pid, channel, payload):
        """Handle PostgreSQL notification."""
        if self._callback:
            try:
                data = json.loads(payload)
                # Schedule the async callback
                asyncio.create_task(self._callback(data))
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in notification: {payload}")
    
    async def handle_http_push(self, payload: dict):
        """Handle HTTP push notification (for SQLite mode)."""
        if self._callback:
            await self._callback(payload)
