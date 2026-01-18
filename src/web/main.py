"""
Web viewer for Telegram Backup.

FastAPI application providing a web interface to browse backed-up messages.
v3.0: Async database operations with SQLAlchemy.
v5.0: WebSocket support for real-time updates and notifications.
"""

from fastapi import FastAPI, Request, HTTPException, Query, Depends, Cookie, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import os
import logging
import glob
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, AsyncGenerator, Set, Dict
from pathlib import Path
import hashlib
import json

from ..config import Config
from ..db import DatabaseAdapter, init_database, close_database, get_db_manager
from ..realtime import RealtimeListener
from .push import PushNotificationManager

# Register MIME types for audio files (required for StaticFiles to serve with correct Content-Type)
import mimetypes
mimetypes.add_type('audio/ogg', '.ogg')
mimetypes.add_type('audio/opus', '.opus')
mimetypes.add_type('audio/mpeg', '.mp3')
mimetypes.add_type('audio/wav', '.wav')
mimetypes.add_type('audio/flac', '.flac')
mimetypes.add_type('audio/x-m4a', '.m4a')
mimetypes.add_type('video/mp4', '.mp4')
mimetypes.add_type('video/webm', '.webm')
mimetypes.add_type('image/webp', '.webp')


# WebSocket Connection Manager for real-time updates
class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""
    
    def __init__(self):
        # Active connections: {websocket: set of subscribed chat_ids}
        self.active_connections: Dict[WebSocket, Set[int]] = {}
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[websocket] = set()
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            del self.active_connections[websocket]
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")
    
    def subscribe(self, websocket: WebSocket, chat_id: int):
        """Subscribe a connection to updates for a specific chat."""
        if websocket in self.active_connections:
            self.active_connections[websocket].add(chat_id)
    
    def unsubscribe(self, websocket: WebSocket, chat_id: int):
        """Unsubscribe a connection from a specific chat."""
        if websocket in self.active_connections:
            self.active_connections[websocket].discard(chat_id)
    
    async def broadcast_to_chat(self, chat_id: int, message: dict):
        """Broadcast a message to all connections subscribed to a chat."""
        disconnected = []
        for websocket, subscribed_chats in self.active_connections.items():
            if chat_id in subscribed_chats or not subscribed_chats:  # Empty set = subscribed to all
                try:
                    await websocket.send_json(message)
                except Exception as e:
                    logger.warning(f"Failed to send to websocket: {e}")
                    disconnected.append(websocket)
        
        # Clean up disconnected sockets
        for ws in disconnected:
            self.disconnect(ws)
    
    async def broadcast_to_all(self, message: dict):
        """Broadcast a message to all connected clients."""
        disconnected = []
        for websocket in self.active_connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to websocket: {e}")
                disconnected.append(websocket)
        
        for ws in disconnected:
            self.disconnect(ws)


# Global connection manager
ws_manager = ConnectionManager()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize config
config = Config()

# Global database adapter (initialized on startup)
db: Optional[DatabaseAdapter] = None


async def _normalize_display_chat_ids():
    """
    Normalize DISPLAY_CHAT_IDS to use marked format.
    
    If a positive ID doesn't exist in DB but -100{id} does, auto-correct it.
    This handles common user mistakes where they forget the -100 prefix for channels.
    """
    if not config.display_chat_ids or not db:
        return
    
    all_chats = await db.get_all_chats()
    existing_ids = {c['id'] for c in all_chats}
    
    normalized = set()
    for chat_id in config.display_chat_ids:
        if chat_id in existing_ids:
            # ID exists as-is
            normalized.add(chat_id)
        elif chat_id > 0:
            # Positive ID not found - try -100 prefix (channel/supergroup format)
            marked_id = -1000000000000 - chat_id
            if marked_id in existing_ids:
                logger.warning(
                    f"DISPLAY_CHAT_IDS: Auto-correcting {chat_id} → {marked_id} "
                    f"(use marked format for channels/supergroups)"
                )
                normalized.add(marked_id)
            else:
                logger.warning(f"DISPLAY_CHAT_IDS: Chat ID {chat_id} not found in database")
                normalized.add(chat_id)  # Keep original, might be backed up later
        else:
            # Negative ID not found
            logger.warning(f"DISPLAY_CHAT_IDS: Chat ID {chat_id} not found in database")
            normalized.add(chat_id)
    
    config.display_chat_ids = normalized


# Background task for stats calculation
stats_task: Optional[asyncio.Task] = None

# Real-time listener (PostgreSQL LISTEN/NOTIFY)
realtime_listener: Optional[RealtimeListener] = None

# Push notification manager (Web Push API)
push_manager: Optional[PushNotificationManager] = None


async def handle_realtime_notification(payload: dict):
    """Handle real-time notifications and broadcast to WebSocket clients + push notifications."""
    notification_type = payload.get('type')
    chat_id = payload.get('chat_id')
    data = payload.get('data', {})
    
    if notification_type == 'new_message':
        await ws_manager.broadcast_to_chat(chat_id, {
            "type": "new_message",
            "message": data.get('message')
        })
        
        # Send Web Push notification for new messages
        if push_manager and push_manager.is_enabled:
            message = data.get('message', {})
            # Get chat info for the notification
            chat = await db.get_chat_by_id(chat_id) if db else None
            chat_title = chat.get('title', 'Telegram') if chat else 'Telegram'
            
            sender_name = ''
            if message.get('sender_id'):
                sender = await db.get_user_by_id(message.get('sender_id')) if db else None
                if sender:
                    sender_name = sender.get('first_name', '') or sender.get('username', '')
            
            await push_manager.notify_new_message(
                chat_id=chat_id,
                chat_title=chat_title,
                sender_name=sender_name,
                message_text=message.get('text', '') or '[Media]',
                message_id=message.get('id', 0)
            )
            
    elif notification_type == 'edit':
        await ws_manager.broadcast_to_chat(chat_id, {
            "type": "edit",
            "message_id": data.get('message_id'),
            "new_text": data.get('new_text')
        })
    elif notification_type == 'delete':
        await ws_manager.broadcast_to_chat(chat_id, {
            "type": "delete",
            "message_id": data.get('message_id')
        })


async def stats_calculation_scheduler():
    """Background task that runs stats calculation daily at configured hour."""
    while True:
        try:
            # Get current time in configured timezone
            tz = ZoneInfo(config.viewer_timezone)
            now = datetime.now(tz)
            
            # Calculate next run time (configured hour, e.g., 3am)
            target_hour = config.stats_calculation_hour
            next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            
            # If we've passed the target time today, schedule for tomorrow
            if now.hour >= target_hour:
                next_run = next_run.replace(day=now.day + 1)
            
            # Wait until next run
            wait_seconds = (next_run - now).total_seconds()
            logger.info(f"Stats calculation scheduled for {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds/3600:.1f}h from now)")
            await asyncio.sleep(wait_seconds)
            
            # Run stats calculation
            logger.info("Running scheduled stats calculation...")
            await db.calculate_and_store_statistics()
            logger.info("Stats calculation completed")
            
        except asyncio.CancelledError:
            logger.info("Stats calculation scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"Error in stats calculation scheduler: {e}")
            # Wait an hour before retrying on error
            await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle - initialize and cleanup database."""
    global db, stats_task
    logger.info("Initializing database connection...")
    db_manager = await init_database()
    db = DatabaseAdapter(db_manager)
    logger.info("Database connection established")
    
    # Normalize display chat IDs (auto-correct missing -100 prefix)
    await _normalize_display_chat_ids()
    
    # Check if stats have ever been calculated, if not, run initial calculation
    stats_calculated_at = await db.get_metadata('stats_calculated_at')
    if not stats_calculated_at:
        logger.info("No cached stats found, running initial calculation...")
        try:
            await db.calculate_and_store_statistics()
        except Exception as e:
            logger.warning(f"Initial stats calculation failed: {e}")
    
    # Start background stats calculation scheduler
    stats_task = asyncio.create_task(stats_calculation_scheduler())
    logger.info(f"Stats calculation scheduler started (runs daily at {config.stats_calculation_hour}:00 {config.viewer_timezone})")
    
    # Start real-time listener (auto-detects PostgreSQL vs SQLite)
    global realtime_listener
    db_manager_instance = await get_db_manager()
    realtime_listener = RealtimeListener(db_manager_instance, callback=handle_realtime_notification)
    await realtime_listener.init()
    await realtime_listener.start()
    logger.info("Real-time listener started (auto-detected database type)")
    
    # Initialize Web Push notifications (if enabled)
    global push_manager
    if config.push_notifications == 'full':
        push_manager = PushNotificationManager(db, config)
        push_enabled = await push_manager.initialize()
        if push_enabled:
            logger.info("Web Push notifications enabled (PUSH_NOTIFICATIONS=full)")
        else:
            logger.warning("Web Push notifications failed to initialize")
    else:
        logger.info(f"Push notifications mode: {config.push_notifications}")
    
    yield
    
    # Cleanup
    if realtime_listener:
        await realtime_listener.stop()
    
    if stats_task:
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass
    
    logger.info("Closing database connection...")
    await close_database()
    logger.info("Database connection closed")


app = FastAPI(title="Telegram Archive", lifespan=lifespan)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple viewer authentication using env vars
VIEWER_USERNAME = os.getenv("VIEWER_USERNAME", "").strip()
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD", "").strip()
AUTH_ENABLED = bool(VIEWER_USERNAME and VIEWER_PASSWORD)
# Cookie name versioned to avoid conflicts with old cookies from previous versions
AUTH_COOKIE_NAME = "viewer_auth_v3"
AUTH_TOKEN = None

if AUTH_ENABLED:
    AUTH_TOKEN = hashlib.sha256(
        f"{VIEWER_USERNAME}:{VIEWER_PASSWORD}".encode("utf-8")
    ).hexdigest()
    logger.info(f"Viewer authentication is ENABLED (User: {VIEWER_USERNAME})")
else:
    logger.info("Viewer authentication is DISABLED (no VIEWER_USERNAME / VIEWER_PASSWORD set)")


def require_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Dependency that enforces cookie-based viewer auth when enabled."""
    if not AUTH_ENABLED:
        return

    if not auth_cookie or auth_cookie != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Setup paths
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"

# Mount static directory
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Mount media directory (includes avatars)
if os.path.exists(config.media_path):
    app.mount("/media", StaticFiles(directory=config.media_path), name="media")


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page."""
    return FileResponse(templates_dir / "index.html")


@app.get("/api/auth/check")
async def check_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Check current authentication status."""
    if not AUTH_ENABLED:
        return {"authenticated": True, "auth_required": False}
    
    is_valid = auth_cookie and auth_cookie == AUTH_TOKEN
    return {"authenticated": is_valid, "auth_required": True}


@app.post("/api/login")
async def login(request: Request):
    """Authenticate viewer user."""
    if not AUTH_ENABLED:
        return JSONResponse({"success": True, "message": "Auth disabled"})
    
    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if username == VIEWER_USERNAME and password == VIEWER_PASSWORD:
            response = JSONResponse({"success": True})
            
            # Detect HTTPS (via reverse proxy header or direct)
            is_https = (
                request.url.scheme == "https" or
                request.headers.get("x-forwarded-proto", "").lower() == "https"
            )
            
            # Simple cookie strategy: SameSite=Lax works for same-site requests
            # Using versioned cookie name (viewer_auth_v3) avoids conflicts with old cookies
            response.set_cookie(
                key=AUTH_COOKIE_NAME,
                value=AUTH_TOKEN,
                httponly=True,
                secure=is_https,
                samesite="lax",
                max_age=30 * 24 * 60 * 60,  # 30 days
                path="/",
            )
            
            return response
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=400, detail="Invalid request")


def _find_avatar_path(chat_id: int, chat_type: str) -> Optional[str]:
    """Find avatar file path for a chat.
    
    Avatar files are stored as: {chat_id}_{photo_id}.jpg
    For groups/channels, chat_id is negative (marked ID format).
    """
    # Determine folder: 'chats' for groups/channels, 'users' for private
    avatar_folder = 'users' if chat_type == 'private' else 'chats'
    avatar_dir = os.path.join(config.media_path, 'avatars', avatar_folder)
    
    if not os.path.exists(avatar_dir):
        return None
    
    # Look for avatar file matching chat_id
    pattern = os.path.join(avatar_dir, f'{chat_id}_*.jpg')
    matches = glob.glob(pattern)
    
    if matches:
        # Return the most recently modified avatar (newest profile photo)
        newest_avatar = max(matches, key=os.path.getmtime)
        avatar_file = os.path.basename(newest_avatar)
        return f"avatars/{avatar_folder}/{avatar_file}"
    
    return None


# Cache avatar paths to avoid repeated filesystem lookups
_avatar_cache: Dict[int, Optional[str]] = {}
_avatar_cache_time: Optional[datetime] = None
AVATAR_CACHE_TTL_SECONDS = 300  # 5 minutes

def _get_cached_avatar_path(chat_id: int, chat_type: str) -> Optional[str]:
    """Get avatar path with caching."""
    global _avatar_cache, _avatar_cache_time
    
    # Invalidate cache if too old
    if _avatar_cache_time and (datetime.utcnow() - _avatar_cache_time).total_seconds() > AVATAR_CACHE_TTL_SECONDS:
        _avatar_cache.clear()
        _avatar_cache_time = None
    
    # Check cache
    if chat_id in _avatar_cache:
        return _avatar_cache[chat_id]
    
    # Lookup and cache
    avatar_path = _find_avatar_path(chat_id, chat_type)
    _avatar_cache[chat_id] = avatar_path
    if _avatar_cache_time is None:
        _avatar_cache_time = datetime.utcnow()
    
    return avatar_path


@app.get("/api/chats", dependencies=[Depends(require_auth)])
async def get_chats(
    limit: int = Query(50, ge=1, le=500, description="Number of chats to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    search: str = Query(None, description="Search query for chat names/usernames")
):
    """Get chats with metadata, paginated. Returns most recent chats first.
    
    If 'search' is provided, returns all chats matching the search query (up to limit).
    Search is case-insensitive and matches title, first_name, last_name, or username.
    """
    try:
        # If display_chat_ids is configured, we need to load all matching chats
        # Otherwise, use pagination
        if config.display_chat_ids:
            chats = await db.get_all_chats()
            chats = [c for c in chats if c['id'] in config.display_chat_ids]
            total = len(chats)
            # Apply pagination after filtering
            chats = chats[offset:offset + limit]
        else:
            chats = await db.get_all_chats(limit=limit, offset=offset, search=search)
            total = await db.get_chat_count(search=search)
        
        # Add avatar URLs using cache
        for chat in chats:
            try:
                avatar_path = _get_cached_avatar_path(chat['id'], chat.get('type', 'private'))
                if avatar_path:
                    chat['avatar_url'] = f"/media/{avatar_path}"
                else:
                    chat['avatar_url'] = None
            except Exception as e:
                logger.error(f"Error finding avatar for chat {chat.get('id')}: {e}")
                chat['avatar_url'] = None
        
        return {
            "chats": chats,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(chats) < total
        }
    except Exception as e:
        logger.error(f"Error fetching chats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats/{chat_id}/messages", dependencies=[Depends(require_auth)])
async def get_messages(
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    before_date: Optional[str] = None,
    before_id: Optional[int] = None,
):
    """
    Get messages for a specific chat with user and media info.
    
    Supports two pagination modes:
    - Offset-based: ?offset=100 (slower for large offsets)
    - Cursor-based: ?before_date=2026-01-15T12:00:00&before_id=12345 (O(1) performance)
    
    Cursor-based pagination is preferred for infinite scroll.
    """
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Parse before_date if provided
    parsed_before_date = None
    if before_date:
        try:
            parsed_before_date = datetime.fromisoformat(before_date.replace('Z', '+00:00'))
            # Strip timezone for DB compatibility
            if parsed_before_date.tzinfo:
                parsed_before_date = parsed_before_date.replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid before_date format. Use ISO 8601.")
    
    try:
        messages = await db.get_messages_paginated(
            chat_id=chat_id,
            limit=limit,
            offset=offset,
            search=search,
            before_date=parsed_before_date,
            before_id=before_id
        )
        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def get_stats():
    """Get cached backup statistics (fast, calculated daily)."""
    try:
        stats = await db.get_cached_statistics()
        stats['timezone'] = config.viewer_timezone
        stats['stats_calculation_hour'] = config.stats_calculation_hour
        stats['show_stats'] = config.show_stats  # Whether to show stats UI
        
        # Check if real-time listener is active (written by backup container)
        listener_active_since = await db.get_metadata('listener_active_since')
        stats['listener_active'] = bool(listener_active_since)
        stats['listener_active_since'] = listener_active_since if listener_active_since else None
        
        # Notifications config
        stats['push_notifications'] = config.push_notifications  # off, basic, full
        stats['push_enabled'] = push_manager is not None and push_manager.is_enabled
        
        # Notifications enabled if ENABLE_NOTIFICATIONS=true OR PUSH_NOTIFICATIONS is basic/full
        stats['enable_notifications'] = (
            config.enable_notifications or 
            config.push_notifications in ('basic', 'full')
        )
        
        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stats/refresh", dependencies=[Depends(require_auth)])
async def refresh_stats():
    """Manually trigger stats recalculation (expensive, use sparingly)."""
    try:
        stats = await db.calculate_and_store_statistics()
        stats['timezone'] = config.viewer_timezone
        return stats
    except Exception as e:
        logger.error(f"Error calculating stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Web Push Notification Endpoints
# ============================================================================

@app.get("/api/push/config")
async def get_push_config():
    """
    Get push notification configuration.
    
    Returns the push notification mode and VAPID public key if available.
    This endpoint is public (no auth) so clients can check before subscribing.
    """
    result = {
        'mode': config.push_notifications,
        'enabled': config.push_notifications == 'full' and push_manager is not None and push_manager.is_enabled,
        'vapid_public_key': None
    }
    
    if push_manager and push_manager.is_enabled:
        result['vapid_public_key'] = push_manager.public_key
    
    return result


@app.post("/api/push/subscribe", dependencies=[Depends(require_auth)])
async def push_subscribe(request: Request):
    """
    Subscribe to push notifications.
    
    Body should contain:
    - endpoint: Push service URL
    - keys.p256dh: Client public key (base64)
    - keys.auth: Auth secret (base64)
    - chat_id: Optional chat ID for chat-specific subscriptions
    """
    if not push_manager or not push_manager.is_enabled:
        raise HTTPException(
            status_code=400,
            detail="Push notifications not enabled. Set PUSH_NOTIFICATIONS=full"
        )
    
    try:
        data = await request.json()
        
        endpoint = data.get('endpoint')
        keys = data.get('keys', {})
        p256dh = keys.get('p256dh')
        auth = keys.get('auth')
        chat_id = data.get('chat_id')
        
        if not endpoint or not p256dh or not auth:
            raise HTTPException(status_code=400, detail="Missing required subscription data")
        
        # Get user agent for debugging
        user_agent = request.headers.get('user-agent', '')[:500]
        
        success = await push_manager.subscribe(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            chat_id=chat_id,
            user_agent=user_agent
        )
        
        if success:
            return {"status": "subscribed", "chat_id": chat_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to store subscription")
            
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push subscribe error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/push/unsubscribe", dependencies=[Depends(require_auth)])
async def push_unsubscribe(request: Request):
    """
    Unsubscribe from push notifications.
    
    Body should contain:
    - endpoint: Push service URL to unsubscribe
    """
    if not push_manager:
        raise HTTPException(status_code=400, detail="Push notifications not enabled")
    
    try:
        data = await request.json()
        endpoint = data.get('endpoint')
        
        if not endpoint:
            raise HTTPException(status_code=400, detail="Missing endpoint")
        
        success = await push_manager.unsubscribe(endpoint)
        return {"status": "unsubscribed" if success else "not_found"}
        
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push unsubscribe error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/internal/push")
async def internal_push(request: Request):
    """
    Internal endpoint for SQLite real-time push notifications.
    
    The backup/listener container POSTs to this endpoint when using SQLite,
    and this broadcasts to connected WebSocket clients.
    
    For PostgreSQL, use LISTEN/NOTIFY instead (auto-detected).
    """
    # Only allow from localhost/internal network
    client_host = request.client.host if request.client else None
    if client_host not in ('127.0.0.1', 'localhost', '::1', None):
        # In Docker, containers communicate via internal network
        # We'll be permissive here as this is internal-only
        pass
    
    try:
        payload = await request.json()
        if realtime_listener:
            await realtime_listener.handle_http_push(payload)
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Error handling internal push: {e}")
        return {"status": "error", "detail": str(e)}


@app.get("/api/chats/{chat_id}/stats", dependencies=[Depends(require_auth)])
async def get_chat_stats(chat_id: int):
    """Get statistics for a specific chat (message count, media files, size)."""
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        stats = await db.get_chat_stats(chat_id)
        return stats
    except Exception as e:
        logger.error(f"Error getting chat stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats/{chat_id}/messages/by-date", dependencies=[Depends(require_auth)])
async def get_message_by_date(
    chat_id: int, 
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(None, description="Timezone for date interpretation (e.g., 'Europe/Madrid')")
):
    """
    Find the first message on or after a specific date for navigation.
    Used by the date picker to jump to a specific date.
    """
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        # Use provided timezone, fall back to config, then UTC
        tz_str = timezone or config.viewer_timezone or 'UTC'
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            logger.warning(f"Invalid timezone '{tz_str}', falling back to UTC")
            user_tz = ZoneInfo('UTC')
        
        # Parse date string (YYYY-MM-DD) as a date in the user's timezone
        naive_date = datetime.strptime(date, "%Y-%m-%d")
        # Create timezone-aware datetime at start of day in user's timezone
        local_start_of_day = naive_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
        # Convert to UTC for database query
        target_date = local_start_of_day.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
        
        message = await db.find_message_by_date_with_joins(chat_id, target_date)
        
        if not message:
            raise HTTPException(status_code=404, detail="No messages found for this date")
        
        return message
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error finding message by date: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats/{chat_id}/export", dependencies=[Depends(require_auth)])
async def export_chat(chat_id: int):
    """Export chat history to JSON."""
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        chat = await db.get_chat_by_id(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        chat_name = chat.get('title') or chat.get('username') or str(chat_id)
        # Sanitize filename
        safe_name = "".join(c for c in chat_name if c.isalnum() or c in (' ', '-', '_')).strip()
        filename = f"{safe_name}_export.json"
        
        async def iter_json():
            yield '[\n'
            first = True
            async for msg in db.get_messages_for_export(chat_id):
                if not first:
                    yield ',\n'
                first = False
                yield json.dumps(msg)
            yield '\n]'
        
        return StreamingResponse(
            iter_json(),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Real-time WebSocket Endpoints (v5.0)
# ============================================================================

@app.get("/api/notifications/settings")
async def get_notification_settings(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Get notification settings for the viewer."""
    # Check auth if enabled
    if AUTH_ENABLED and (not auth_cookie or auth_cookie != AUTH_TOKEN):
        return {"enabled": False, "reason": "Not authenticated"}
    
    # Notifications enabled if:
    # - ENABLE_NOTIFICATIONS=true (legacy), OR
    # - PUSH_NOTIFICATIONS is 'basic' or 'full'
    notifications_active = (
        config.enable_notifications or 
        config.push_notifications in ('basic', 'full')
    )
    
    return {
        "enabled": notifications_active,
        "mode": config.push_notifications,  # off, basic, full
        "websocket_url": "/ws/updates"
    }


@app.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates.
    
    Clients can subscribe to specific chats or receive all updates.
    
    Message format (client -> server):
        {"action": "subscribe", "chat_id": 123456}
        {"action": "unsubscribe", "chat_id": 123456}
        {"action": "ping"}
    
    Message format (server -> client):
        {"type": "new_message", "chat_id": 123, "message": {...}}
        {"type": "edit", "chat_id": 123, "message_id": 456, "new_text": "..."}
        {"type": "delete", "chat_id": 123, "message_id": 456}
        {"type": "pong"}
        {"type": "subscribed", "chat_id": 123}
        {"type": "unsubscribed", "chat_id": 123}
    """
    await ws_manager.connect(websocket)
    
    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            action = data.get("action")
            
            if action == "subscribe":
                chat_id = data.get("chat_id")
                if chat_id:
                    # Check access in display mode
                    if config.display_chat_ids and chat_id not in config.display_chat_ids:
                        await websocket.send_json({"type": "error", "message": "Access denied"})
                    else:
                        ws_manager.subscribe(websocket, chat_id)
                        await websocket.send_json({"type": "subscribed", "chat_id": chat_id})
            
            elif action == "unsubscribe":
                chat_id = data.get("chat_id")
                if chat_id:
                    ws_manager.unsubscribe(websocket, chat_id)
                    await websocket.send_json({"type": "unsubscribed", "chat_id": chat_id})
            
            elif action == "ping":
                await websocket.send_json({"type": "pong"})
            
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)


# ============================================================================
# Helper functions for broadcasting updates (called from listener)
# ============================================================================

async def broadcast_new_message(chat_id: int, message: dict):
    """Broadcast a new message to subscribed clients."""
    await ws_manager.broadcast_to_chat(chat_id, {
        "type": "new_message",
        "chat_id": chat_id,
        "message": message
    })


async def broadcast_message_edit(chat_id: int, message_id: int, new_text: str, edit_date: str):
    """Broadcast a message edit to subscribed clients."""
    await ws_manager.broadcast_to_chat(chat_id, {
        "type": "edit",
        "chat_id": chat_id,
        "message_id": message_id,
        "new_text": new_text,
        "edit_date": edit_date
    })


async def broadcast_message_delete(chat_id: int, message_id: int):
    """Broadcast a message deletion to subscribed clients."""
    await ws_manager.broadcast_to_chat(chat_id, {
        "type": "delete",
        "chat_id": chat_id,
        "message_id": message_id
    })
