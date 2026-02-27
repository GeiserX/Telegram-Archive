"""
Web viewer for Telegram Backup.

FastAPI application providing a web interface to browse backed-up messages.
v3.0: Async database operations with SQLAlchemy.
v5.0: WebSocket support for real-time updates and notifications.
"""

import asyncio
import glob
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config
from ..db import DatabaseAdapter, close_database, get_db_manager, init_database
from ..realtime import RealtimeListener

if TYPE_CHECKING:
    from .push import PushNotificationManager

# Register MIME types for audio files (required for StaticFiles to serve with correct Content-Type)
import mimetypes

mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/opus", ".opus")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/x-m4a", ".m4a")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("image/webp", ".webp")


# WebSocket Connection Manager for real-time updates
class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        self.active_connections: dict[WebSocket, set[int]] = {}
        self._allowed_chats: dict[WebSocket, set[int] | None] = {}

    async def connect(self, websocket: WebSocket, allowed_chat_ids: set[int] | None = None):
        await websocket.accept()
        self.active_connections[websocket] = set()
        self._allowed_chats[websocket] = allowed_chat_ids
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.pop(websocket, None)
        self._allowed_chats.pop(websocket, None)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    def subscribe(self, websocket: WebSocket, chat_id: int):
        """Subscribe a connection to updates for a specific chat."""
        if websocket in self.active_connections:
            allowed = self._allowed_chats.get(websocket)
            if allowed is not None and chat_id not in allowed:
                return
            self.active_connections[websocket].add(chat_id)

    def unsubscribe(self, websocket: WebSocket, chat_id: int):
        """Unsubscribe a connection from a specific chat."""
        if websocket in self.active_connections:
            self.active_connections[websocket].discard(chat_id)

    async def broadcast_to_chat(self, chat_id: int, message: dict):
        """Broadcast a message to all connections subscribed to a chat."""
        disconnected = []
        for websocket, subscribed_chats in self.active_connections.items():
            allowed = self._allowed_chats.get(websocket)
            if allowed is not None and chat_id not in allowed:
                continue
            if chat_id in subscribed_chats or not subscribed_chats:
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
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize config
config = Config()

# Global database adapter (initialized on startup)
db: DatabaseAdapter | None = None


async def _normalize_display_chat_ids():
    """
    Normalize DISPLAY_CHAT_IDS to use marked format.

    If a positive ID doesn't exist in DB but -100{id} does, auto-correct it.
    This handles common user mistakes where they forget the -100 prefix for channels.
    """
    if not config.display_chat_ids or not db:
        return

    all_chats = await db.get_all_chats()
    existing_ids = {c["id"] for c in all_chats}

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


# Background tasks
stats_task: asyncio.Task | None = None
_session_cleanup_task: asyncio.Task | None = None

# Real-time listener (PostgreSQL LISTEN/NOTIFY)
realtime_listener: RealtimeListener | None = None

# Push notification manager (Web Push API)
push_manager: PushNotificationManager | None = None


async def handle_realtime_notification(payload: dict):
    """Handle real-time notifications and broadcast to WebSocket clients + push notifications."""
    notification_type = payload.get("type")
    chat_id = payload.get("chat_id")
    data = payload.get("data", {})

    # Check if this chat is allowed (respects DISPLAY_CHAT_IDS restriction)
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        # This viewer is restricted to specific chats, ignore notifications for other chats
        return

    if notification_type == "new_message":
        await ws_manager.broadcast_to_chat(chat_id, {"type": "new_message", "message": data.get("message")})

        # Send Web Push notification for new messages
        if push_manager and push_manager.is_enabled:
            message = data.get("message", {})
            # Get chat info for the notification
            chat = await db.get_chat_by_id(chat_id) if db else None
            chat_title = chat.get("title", "Telegram") if chat else "Telegram"

            sender_name = ""
            if message.get("sender_id"):
                sender = await db.get_user_by_id(message.get("sender_id")) if db else None
                if sender:
                    sender_name = sender.get("first_name", "") or sender.get("username", "")

            await push_manager.notify_new_message(
                chat_id=chat_id,
                chat_title=chat_title,
                sender_name=sender_name,
                message_text=message.get("text", "") or "[Media]",
                message_id=message.get("id", 0),
            )

    elif notification_type == "edit":
        await ws_manager.broadcast_to_chat(
            chat_id, {"type": "edit", "message_id": data.get("message_id"), "new_text": data.get("new_text")}
        )
    elif notification_type == "delete":
        await ws_manager.broadcast_to_chat(chat_id, {"type": "delete", "message_id": data.get("message_id")})


async def session_cleanup_task():
    """Periodically evict expired sessions and stale rate limit entries."""
    while True:
        try:
            await asyncio.sleep(_SESSION_CLEANUP_INTERVAL)
            now = time.time()
            expired = [k for k, v in _sessions.items() if now - v.created_at > AUTH_SESSION_SECONDS]
            for k in expired:
                _sessions.pop(k, None)
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired sessions")
            stale_ips = [ip for ip, ts in _login_attempts.items() if all(now - t > _LOGIN_RATE_WINDOW for t in ts)]
            for ip in stale_ips:
                _login_attempts.pop(ip, None)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Session cleanup error: {e}")


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
            logger.info(
                f"Stats calculation scheduled for {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds / 3600:.1f}h from now)"
            )
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
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifecycle - initialize and cleanup database."""
    global db, stats_task, _session_cleanup_task
    logger.info("Initializing database connection...")
    db_manager = await init_database()
    db = DatabaseAdapter(db_manager)
    logger.info("Database connection established")

    # Normalize display chat IDs (auto-correct missing -100 prefix)
    await _normalize_display_chat_ids()

    # Check if stats have ever been calculated, if not, run initial calculation
    stats_calculated_at = await db.get_metadata("stats_calculated_at")
    if not stats_calculated_at:
        logger.info("No cached stats found, running initial calculation...")
        try:
            await db.calculate_and_store_statistics()
        except Exception as e:
            logger.warning(f"Initial stats calculation failed: {e}")

    # Start background tasks
    stats_task = asyncio.create_task(stats_calculation_scheduler())
    _session_cleanup_task = asyncio.create_task(session_cleanup_task())
    logger.info(
        f"Stats calculation scheduler started (runs daily at {config.stats_calculation_hour}:00 {config.viewer_timezone})"
    )

    # Start real-time listener (auto-detects PostgreSQL vs SQLite)
    global realtime_listener
    db_manager_instance = await get_db_manager()
    realtime_listener = RealtimeListener(db_manager_instance, callback=handle_realtime_notification)
    await realtime_listener.init()
    await realtime_listener.start()
    logger.info("Real-time listener started (auto-detected database type)")

    # Initialize Web Push notifications (if enabled)
    global push_manager
    if config.push_notifications == "full":
        from .push import PushNotificationManager

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

    for task in [stats_task, _session_cleanup_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("Closing database connection...")
    await close_database()
    logger.info("Database connection closed")


app = FastAPI(title="Telegram Archive", lifespan=lifespan)

# Enable CORS
# CORS_ORIGINS env var: comma-separated list of allowed origins (default: "*")
# When using "*", credentials are disabled for security (browser requirement)
_cors_origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
_cors_allow_credentials = _cors_origins != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com"
    )
    return response


# ============================================================================
# Multi-User Authentication (v7.0.0)
# ============================================================================

VIEWER_USERNAME = os.getenv("VIEWER_USERNAME", "").strip()
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD", "").strip()
AUTH_ENABLED = bool(VIEWER_USERNAME and VIEWER_PASSWORD)
AUTH_COOKIE_NAME = "viewer_auth"

AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "30"))
AUTH_SESSION_SECONDS = AUTH_SESSION_DAYS * 24 * 60 * 60
_MAX_SESSIONS_PER_USER = 10
_SESSION_CLEANUP_INTERVAL = 900  # 15 minutes
_LOGIN_RATE_LIMIT = 15  # max attempts
_LOGIN_RATE_WINDOW = 300  # per 5 minutes

if AUTH_ENABLED:
    logger.info(f"Viewer authentication is ENABLED (Master: {VIEWER_USERNAME}, Session: {AUTH_SESSION_DAYS} days)")
else:
    logger.info("Viewer authentication is DISABLED (no VIEWER_USERNAME / VIEWER_PASSWORD set)")


@dataclass
class UserContext:
    username: str
    role: str  # "master" or "viewer"
    allowed_chat_ids: set[int] | None = None  # None = all chats


@dataclass
class SessionData:
    username: str
    role: str
    allowed_chat_ids: set[int] | None = None
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


_sessions: dict[str, SessionData] = {}
_login_attempts: dict[str, list[float]] = {}  # ip -> list of timestamps


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()


def _verify_password(password: str, salt: str, password_hash: str) -> bool:
    return secrets.compare_digest(_hash_password(password, salt), password_hash)


def _check_rate_limit(ip: str) -> bool:
    """Returns True if the request is within rate limits."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_RATE_LIMIT


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _create_session(username: str, role: str, allowed_chat_ids: set[int] | None = None) -> str:
    """Create a new session, evicting oldest if user exceeds max sessions."""
    user_sessions = [(k, v) for k, v in _sessions.items() if v.username == username]
    if len(user_sessions) >= _MAX_SESSIONS_PER_USER:
        user_sessions.sort(key=lambda x: x[1].created_at)
        for token, _ in user_sessions[: len(user_sessions) - _MAX_SESSIONS_PER_USER + 1]:
            _sessions.pop(token, None)

    token = secrets.token_urlsafe(32)
    _sessions[token] = SessionData(username=username, role=role, allowed_chat_ids=allowed_chat_ids)
    return token


def _invalidate_user_sessions(username: str) -> None:
    """Remove all sessions for a given username."""
    to_remove = [k for k, v in _sessions.items() if v.username == username]
    for k in to_remove:
        _sessions.pop(k, None)


def _get_secure_cookies(request: Request) -> bool:
    secure_env = os.getenv("SECURE_COOKIES", "").strip().lower()
    if secure_env == "true":
        return True
    if secure_env == "false":
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto == "https" or str(request.url.scheme) == "https"


def require_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)) -> UserContext:
    """Dependency that enforces session-based auth. Returns UserContext."""
    if not AUTH_ENABLED:
        return UserContext(username="anonymous", role="master", allowed_chat_ids=None)

    if not auth_cookie:
        raise HTTPException(status_code=401, detail="Unauthorized")

    session = _sessions.get(auth_cookie)
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if time.time() - session.created_at > AUTH_SESSION_SECONDS:
        _sessions.pop(auth_cookie, None)
        raise HTTPException(status_code=401, detail="Session expired")

    session.last_accessed = time.time()
    return UserContext(
        username=session.username,
        role=session.role,
        allowed_chat_ids=session.allowed_chat_ids,
    )


def require_master(user: UserContext = Depends(require_auth)) -> UserContext:
    """Dependency that requires master role."""
    if user.role != "master":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_user_chat_ids(user: UserContext) -> set[int] | None:
    """Get the effective chat IDs a user can access.

    Returns None if the user can see all chats (no restriction).
    """
    master_filter = config.display_chat_ids or None  # empty set -> None

    if user.role == "master":
        return master_filter

    # Viewer: use their allowed_chat_ids, intersected with master filter
    if user.allowed_chat_ids is None:
        return master_filter
    if master_filter is None:
        return user.allowed_chat_ids
    return user.allowed_chat_ids & master_filter


# Setup paths
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"


@app.get("/sw.js")
async def serve_service_worker():
    """
    Serve the service worker from root path with proper headers.

    The Service-Worker-Allowed header allows the SW to have scope '/'
    even though the file is served from /static/sw.js.
    """
    sw_path = static_dir / "sw.js"
    if not sw_path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")

    return FileResponse(sw_path, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


# Mount static directory (no auth needed for CSS/JS/icons)
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Media is served via authenticated endpoint below (not StaticFiles)
_media_root = Path(config.media_path).resolve() if os.path.exists(config.media_path) else None


@app.get("/media/{path:path}")
async def serve_media(path: str, user: UserContext = Depends(require_auth)):
    """Serve media files with authentication and path traversal protection."""
    if not _media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    resolved = (_media_root / path).resolve()
    if not resolved.is_relative_to(_media_root):
        raise HTTPException(status_code=403, detail="Access denied")

    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] != "avatars":
            try:
                media_chat_id = int(parts[0])
                if media_chat_id not in user_chat_ids:
                    raise HTTPException(status_code=403, detail="Access denied")
            except ValueError:
                pass

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(resolved)


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page."""
    return FileResponse(templates_dir / "index.html")


@app.get("/api/auth/check")
async def check_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Check current authentication status. Returns role and username if authenticated."""
    if not AUTH_ENABLED:
        return {"authenticated": True, "auth_required": False, "role": "master", "username": "anonymous"}

    if not auth_cookie:
        return {"authenticated": False, "auth_required": True}

    session = _sessions.get(auth_cookie)
    if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
        return {"authenticated": False, "auth_required": True}

    return {
        "authenticated": True,
        "auth_required": True,
        "role": session.role,
        "username": session.username,
    }


@app.post("/api/login")
async def login(request: Request):
    """Authenticate user (master via env vars or viewer via DB accounts)."""
    if not AUTH_ENABLED:
        return JSONResponse({"success": True, "message": "Auth disabled"})

    direct_ip = request.client.host if request.client else "unknown"
    _trusted = direct_ip.startswith(("172.", "10.", "192.168.", "127.")) or direct_ip in ("::1", "localhost")
    if _trusted:
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or request.headers.get("x-real-ip", "")
            or direct_ip
        )
    else:
        client_ip = direct_ip

    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")

    _record_login_attempt(client_ip)
    user_agent = request.headers.get("user-agent", "")[:500]

    # 1. Check DB viewer accounts first
    if db:
        viewer = await db.get_viewer_by_username(username)
        if viewer and viewer["is_active"]:
            if _verify_password(password, viewer["salt"], viewer["password_hash"]):
                allowed = None
                if viewer["allowed_chat_ids"]:
                    try:
                        allowed = set(json.loads(viewer["allowed_chat_ids"]))
                    except (json.JSONDecodeError, TypeError):
                        allowed = None

                token = _create_session(username, "viewer", allowed)
                response = JSONResponse({"success": True, "role": "viewer", "username": username})
                response.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=token,
                    httponly=True,
                    secure=_get_secure_cookies(request),
                    samesite="lax",
                    max_age=AUTH_SESSION_SECONDS,
                )

                if db:
                    await db.create_audit_log(
                        username=username,
                        role="viewer",
                        action="login_success",
                        endpoint="/api/login",
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                return response

    # 2. Fall back to master env var credentials
    if secrets.compare_digest(username, VIEWER_USERNAME) and secrets.compare_digest(password, VIEWER_PASSWORD):
        token = _create_session(username, "master", None)
        response = JSONResponse({"success": True, "role": "master", "username": username})
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=_get_secure_cookies(request),
            samesite="lax",
            max_age=AUTH_SESSION_SECONDS,
        )

        if db:
            await db.create_audit_log(
                username=username,
                role="master",
                action="login_success",
                endpoint="/api/login",
                ip_address=client_ip,
                user_agent=user_agent,
            )
        return response

    # Failed login
    if db:
        await db.create_audit_log(
            username=username or "(empty)",
            role="unknown",
            action="login_failed",
            endpoint="/api/login",
            ip_address=client_ip,
            user_agent=user_agent,
        )
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout")
async def logout(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """Invalidate current session and clear cookie."""
    if auth_cookie and auth_cookie in _sessions:
        session = _sessions.pop(auth_cookie)
        if db:
            await db.create_audit_log(
                username=session.username,
                role=session.role,
                action="logout",
                endpoint="/api/logout",
                ip_address=request.client.host if request.client else None,
            )

    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


def _find_avatar_path(chat_id: int, chat_type: str) -> str | None:
    """Find avatar file path for a chat.

    Avatar files are stored as: {chat_id}_{photo_id}.jpg
    For groups/channels, chat_id is negative (marked ID format).
    """
    # Determine folder: 'chats' for groups/channels, 'users' for private
    avatar_folder = "users" if chat_type == "private" else "chats"
    avatar_dir = os.path.join(config.media_path, "avatars", avatar_folder)

    if not os.path.exists(avatar_dir):
        return None

    # Look for avatar file matching chat_id
    pattern = os.path.join(avatar_dir, f"{chat_id}_*.jpg")
    matches = glob.glob(pattern)

    # Legacy fallback: files saved without photo_id suffix
    legacy_path = os.path.join(avatar_dir, f"{chat_id}.jpg")
    if os.path.exists(legacy_path):
        matches.append(legacy_path)

    if matches:
        # Return the most recently modified avatar (newest profile photo)
        newest_avatar = max(matches, key=os.path.getmtime)
        avatar_file = os.path.basename(newest_avatar)
        return f"avatars/{avatar_folder}/{avatar_file}"

    return None


# Cache avatar paths to avoid repeated filesystem lookups
_avatar_cache: dict[int, str | None] = {}
_avatar_cache_time: datetime | None = None
AVATAR_CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cached_avatar_path(chat_id: int, chat_type: str) -> str | None:
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


@app.get("/api/chats")
async def get_chats(
    user: UserContext = Depends(require_auth),
    limit: int = Query(50, ge=1, le=1000, description="Number of chats to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    search: str = Query(None, description="Search query for chat names/usernames"),
    archived: bool | None = Query(None, description="Filter by archived status"),
    folder_id: int | None = Query(None, description="Filter by folder ID"),
):
    """Get chats with metadata, paginated. Returns most recent chats first.

    If 'search' is provided, returns all chats matching the search query (up to limit).
    Search is case-insensitive and matches title, first_name, last_name, or username.

    v6.2.0: Added archived and folder_id filters.
    """
    try:
        user_chat_ids = get_user_chat_ids(user)
        # If user has chat restrictions, we need to load all matching chats
        # Otherwise, use pagination
        if user_chat_ids is not None:
            chats = await db.get_all_chats(search=search, archived=archived, folder_id=folder_id)
            chats = [c for c in chats if c["id"] in user_chat_ids]
            total = len(chats)
            # Apply pagination after filtering
            chats = chats[offset : offset + limit]
        else:
            chats = await db.get_all_chats(
                limit=limit, offset=offset, search=search, archived=archived, folder_id=folder_id
            )
            total = await db.get_chat_count(search=search, archived=archived, folder_id=folder_id)

        # Add avatar URLs using cache
        for chat in chats:
            try:
                avatar_path = _get_cached_avatar_path(chat["id"], chat.get("type", "private"))
                if avatar_path:
                    chat["avatar_url"] = f"/media/{avatar_path}"
                else:
                    chat["avatar_url"] = None
            except Exception as e:
                logger.error(f"Error finding avatar for chat {chat.get('id')}: {e}")
                chat["avatar_url"] = None

        return {
            "chats": chats,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(chats) < total,
        }
    except Exception as e:
        logger.error(f"Error fetching chats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    before_date: str | None = None,
    before_id: int | None = None,
    topic_id: int | None = None,
):
    """
    Get messages for a specific chat with user and media info.

    Supports two pagination modes:
    - Offset-based: ?offset=100 (slower for large offsets)
    - Cursor-based: ?before_date=2026-01-15T12:00:00&before_id=12345 (O(1) performance)

    v6.2.0: Added topic_id filter for forum topic messages.

    Cursor-based pagination is preferred for infinite scroll.
    """
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    # Parse before_date if provided
    parsed_before_date = None
    if before_date:
        try:
            parsed_before_date = datetime.fromisoformat(before_date.replace("Z", "+00:00"))
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
            before_id=before_id,
            topic_id=topic_id,
        )
        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_id}/pinned")
async def get_pinned_messages(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get all pinned messages for a chat, ordered by date descending (newest first)."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        pinned_messages = await db.get_pinned_messages(chat_id)
        return pinned_messages  # Returns empty list if no pinned messages
    except Exception as e:
        logger.error(f"Error fetching pinned messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/folders")
async def get_folders(user: UserContext = Depends(require_auth)):
    """Get all chat folders with their chat counts.

    v6.2.0: Returns user-created Telegram folders (dialog filters).
    """
    try:
        folders = await db.get_all_folders()
        return {"folders": folders}
    except Exception as e:
        logger.error(f"Error fetching folders: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_id}/topics")
async def get_chat_topics(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get forum topics for a chat.

    v6.2.0: Returns topic list with message counts for forum-enabled chats.
    """
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        topics = await db.get_forum_topics(chat_id)
        return {"topics": topics}
    except Exception as e:
        logger.error(f"Error fetching topics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/archived/count")
async def get_archived_count(user: UserContext = Depends(require_auth)):
    """Get the number of archived chats.

    v6.2.0: Used by the viewer to display the archived section badge.
    Respects DISPLAY_CHAT_IDS so restricted viewers only see relevant archived chats.
    """
    try:
        user_chat_ids = get_user_chat_ids(user)
        if user_chat_ids is not None:
            all_archived = await db.get_all_chats(archived=True)
            count = sum(1 for c in all_archived if c["id"] in user_chat_ids)
        else:
            count = await db.get_archived_chat_count()
        return {"count": count}
    except Exception as e:
        logger.error(f"Error fetching archived count: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/stats")
async def get_stats(user: UserContext = Depends(require_auth)):
    """Get cached backup statistics (fast, calculated daily)."""
    try:
        stats = await db.get_cached_statistics()
        stats["timezone"] = config.viewer_timezone
        stats["stats_calculation_hour"] = config.stats_calculation_hour
        stats["show_stats"] = config.show_stats  # Whether to show stats UI

        # Check if real-time listener is active (written by backup container)
        listener_active_since = await db.get_metadata("listener_active_since")
        stats["listener_active"] = bool(listener_active_since)
        stats["listener_active_since"] = listener_active_since if listener_active_since else None

        # Notifications config
        stats["push_notifications"] = config.push_notifications  # off, basic, full
        stats["push_enabled"] = push_manager is not None and push_manager.is_enabled

        # Notifications enabled if ENABLE_NOTIFICATIONS=true OR PUSH_NOTIFICATIONS is basic/full
        stats["enable_notifications"] = config.enable_notifications or config.push_notifications in ("basic", "full")

        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/stats/refresh")
async def refresh_stats(user: UserContext = Depends(require_master)):
    """Manually trigger stats recalculation (expensive, use sparingly)."""
    try:
        stats = await db.calculate_and_store_statistics()
        stats["timezone"] = config.viewer_timezone
        return stats
    except Exception as e:
        logger.error(f"Error calculating stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


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
        "mode": config.push_notifications,
        "enabled": config.push_notifications == "full" and push_manager is not None and push_manager.is_enabled,
        "vapid_public_key": None,
    }

    if push_manager and push_manager.is_enabled:
        result["vapid_public_key"] = push_manager.public_key

    return result


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request, user: UserContext = Depends(require_auth)):
    """
    Subscribe to push notifications.

    Body should contain:
    - endpoint: Push service URL
    - keys.p256dh: Client public key (base64)
    - keys.auth: Auth secret (base64)
    - chat_id: Optional chat ID for chat-specific subscriptions
    """
    if not push_manager or not push_manager.is_enabled:
        raise HTTPException(status_code=400, detail="Push notifications not enabled. Set PUSH_NOTIFICATIONS=full")

    try:
        data = await request.json()

        endpoint = data.get("endpoint")
        keys = data.get("keys", {})
        p256dh = keys.get("p256dh")
        auth = data.get("auth")
        chat_id = data.get("chat_id")

        if not endpoint or not p256dh or not auth:
            raise HTTPException(status_code=400, detail="Missing required subscription data")

        if chat_id:
            user_chat_ids = get_user_chat_ids(user)
            if user_chat_ids is not None and chat_id not in user_chat_ids:
                raise HTTPException(status_code=403, detail="Access denied to this chat")

        user_agent = request.headers.get("user-agent", "")[:500]

        success = await push_manager.subscribe(
            endpoint=endpoint, p256dh=p256dh, auth=auth, chat_id=chat_id, user_agent=user_agent
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
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, user: UserContext = Depends(require_auth)):
    """
    Unsubscribe from push notifications.

    Body should contain:
    - endpoint: Push service URL to unsubscribe
    """
    if not push_manager:
        raise HTTPException(status_code=400, detail="Push notifications not enabled")

    try:
        data = await request.json()
        endpoint = data.get("endpoint")

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
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/internal/push")
async def internal_push(request: Request):
    """
    Internal endpoint for SQLite real-time push notifications.

    The backup/listener container POSTs to this endpoint when using SQLite,
    and this broadcasts to connected WebSocket clients.

    For PostgreSQL, use LISTEN/NOTIFY instead (auto-detected).

    Access is restricted to private/loopback IPs and Docker internal networks.
    """
    client_host = request.client.host if request.client else None

    allowed = False
    if client_host and (
        client_host in ("127.0.0.1", "localhost", "::1")
        or client_host.startswith(("172.", "10.", "192.168."))
    ):
        allowed = True

    if not allowed:
        logger.warning(f"Rejected /internal/push from non-private IP: {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        payload = await request.json()
        if realtime_listener:
            await realtime_listener.handle_http_push(payload)
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Error handling internal push: {e}")
        return {"status": "error", "detail": "Internal push processing failed"}


@app.get("/api/chats/{chat_id}/stats")
async def get_chat_stats(chat_id: int, user: UserContext = Depends(require_auth)):
    """Get statistics for a specific chat (message count, media files, size)."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        stats = await db.get_chat_stats(chat_id)
        return stats
    except Exception as e:
        logger.error(f"Error getting chat stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_id}/messages/by-date")
async def get_message_by_date(
    chat_id: int,
    user: UserContext = Depends(require_auth),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(None, description="Timezone for date interpretation (e.g., 'Europe/Madrid')"),
):
    """
    Find the first message on or after a specific date for navigation.
    Used by the date picker to jump to a specific date.
    """
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        # Use provided timezone, fall back to config, then UTC
        tz_str = timezone or config.viewer_timezone or "UTC"
        try:
            user_tz = ZoneInfo(tz_str)
        except Exception:
            logger.warning(f"Invalid timezone '{tz_str}', falling back to UTC")
            user_tz = ZoneInfo("UTC")

        # Parse date string (YYYY-MM-DD) as a date in the user's timezone
        naive_date = datetime.strptime(date, "%Y-%m-%d")
        # Create timezone-aware datetime at start of day in user's timezone
        local_start_of_day = naive_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
        # Convert to UTC for database query
        target_date = local_start_of_day.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

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
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_id}/export")
async def export_chat(chat_id: int, user: UserContext = Depends(require_auth)):
    """Export chat history to JSON."""
    user_chat_ids = get_user_chat_ids(user)
    if user_chat_ids is not None and chat_id not in user_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        chat = await db.get_chat_by_id(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")

        chat_name = chat.get("title") or chat.get("username") or str(chat_id)
        # Sanitize filename
        safe_name = "".join(c for c in chat_name if c.isalnum() or c in (" ", "-", "_")).strip()
        filename = f"{safe_name}_export.json"

        async def iter_json():
            yield "[\n"
            first = True
            async for msg in db.get_messages_for_export(chat_id):
                if not first:
                    yield ",\n"
                first = False
                # Ensure UTF-8 encoding for non-Latin characters
                yield json.dumps(msg, ensure_ascii=False)
            yield "\n]"

        # RFC 5987 encoding for non-ASCII filenames
        encoded_filename = quote(filename)
        return StreamingResponse(
            iter_json(),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Admin Endpoints (v7.0.0) — Master-only viewer account management
# ============================================================================


@app.get("/api/admin/viewers")
async def list_viewers(user: UserContext = Depends(require_master)):
    """List all viewer accounts."""
    viewers = await db.get_all_viewer_accounts()
    safe = []
    for v in viewers:
        safe.append(
            {
                "id": v["id"],
                "username": v["username"],
                "allowed_chat_ids": json.loads(v["allowed_chat_ids"]) if v["allowed_chat_ids"] else None,
                "is_active": v["is_active"],
                "created_by": v["created_by"],
                "created_at": v["created_at"],
                "updated_at": v["updated_at"],
            }
        )
    return {"viewers": safe}


@app.post("/api/admin/viewers")
async def create_viewer(request: Request, user: UserContext = Depends(require_master)):
    """Create a new viewer account."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    allowed_chat_ids = data.get("allowed_chat_ids")

    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if AUTH_ENABLED and VIEWER_USERNAME and username.lower() == VIEWER_USERNAME.lower():
        raise HTTPException(status_code=409, detail="Username conflicts with master account")

    existing = await db.get_viewer_by_username(username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    salt = secrets.token_hex(32)
    password_hash = _hash_password(password, salt)

    chat_ids_json = None
    if allowed_chat_ids is not None:
        try:
            chat_ids_json = json.dumps([int(cid) for cid in allowed_chat_ids])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

    account = await db.create_viewer_account(
        username=username,
        password_hash=password_hash,
        salt=salt,
        allowed_chat_ids=chat_ids_json,
        created_by=user.username,
    )

    await db.create_audit_log(
        username=user.username,
        role="master",
        action="viewer_created",
        endpoint="/api/admin/viewers",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": account["id"],
        "username": account["username"],
        "allowed_chat_ids": json.loads(chat_ids_json) if chat_ids_json else None,
        "is_active": account["is_active"],
    }


@app.put("/api/admin/viewers/{viewer_id}")
async def update_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Update a viewer account. Invalidates their existing sessions."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    existing = await db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    updates = {}
    if "password" in data and data["password"]:
        pwd = data["password"].strip()
        if len(pwd) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        salt = secrets.token_hex(32)
        updates["password_hash"] = _hash_password(pwd, salt)
        updates["salt"] = salt

    if "allowed_chat_ids" in data:
        allowed = data["allowed_chat_ids"]
        if allowed is None:
            updates["allowed_chat_ids"] = None
        else:
            try:
                updates["allowed_chat_ids"] = json.dumps([int(cid) for cid in allowed])
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid chat ID format")

    if "is_active" in data:
        updates["is_active"] = 1 if data["is_active"] else 0

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    account = await db.update_viewer_account(viewer_id, **updates)
    _invalidate_user_sessions(existing["username"])

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"viewer_updated:{existing['username']}",
        endpoint=f"/api/admin/viewers/{viewer_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": account["id"],
        "username": account["username"],
        "allowed_chat_ids": json.loads(account["allowed_chat_ids"]) if account["allowed_chat_ids"] else None,
        "is_active": account["is_active"],
    }


@app.delete("/api/admin/viewers/{viewer_id}")
async def delete_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Delete a viewer account and invalidate their sessions."""
    existing = await db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    _invalidate_user_sessions(existing["username"])
    await db.delete_viewer_account(viewer_id)

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"viewer_deleted:{existing['username']}",
        endpoint=f"/api/admin/viewers/{viewer_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True}


@app.get("/api/admin/chats")
async def admin_list_chats(user: UserContext = Depends(require_master)):
    """List all chats for the admin chat picker."""
    chats = await db.get_all_chats()
    return {"chats": [{"id": c["id"], "title": c.get("title"), "type": c.get("type")} for c in chats]}


@app.get("/api/admin/audit")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    username: str | None = Query(None),
    user: UserContext = Depends(require_master),
):
    """Get paginated audit log entries."""
    logs = await db.get_audit_logs(limit=limit, offset=offset, username=username)
    return {"logs": logs, "limit": limit, "offset": offset}


# ============================================================================
# Real-time WebSocket Endpoints (v5.0)
# ============================================================================


@app.get("/api/notifications/settings")
async def get_notification_settings(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Get notification settings for the viewer."""
    if AUTH_ENABLED:
        session = _sessions.get(auth_cookie) if auth_cookie else None
        if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
            return {"enabled": False, "reason": "Not authenticated"}

    # Notifications enabled if:
    # - ENABLE_NOTIFICATIONS=true (legacy), OR
    # - PUSH_NOTIFICATIONS is 'basic' or 'full'
    notifications_active = config.enable_notifications or config.push_notifications in ("basic", "full")

    return {
        "enabled": notifications_active,
        "mode": config.push_notifications,  # off, basic, full
        "websocket_url": "/ws/updates",
    }


@app.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates.

    Auth is enforced via cookie sent during WebSocket upgrade.
    Per-user chat filtering is applied to subscriptions.
    """
    # Validate auth from cookie before accepting
    cookies = websocket.cookies
    auth_cookie = cookies.get(AUTH_COOKIE_NAME)
    ws_user_chat_ids: set[int] | None = None

    if AUTH_ENABLED:
        if not auth_cookie:
            await websocket.close(code=4001, reason="Unauthorized")
            return
        session = _sessions.get(auth_cookie)
        if not session or time.time() - session.created_at > AUTH_SESSION_SECONDS:
            await websocket.close(code=4001, reason="Session expired")
            return
        user_ctx = UserContext(session.username, session.role, session.allowed_chat_ids)
        ws_user_chat_ids = get_user_chat_ids(user_ctx)

    await ws_manager.connect(websocket, allowed_chat_ids=ws_user_chat_ids)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "subscribe":
                chat_id = data.get("chat_id")
                if chat_id:
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
    await ws_manager.broadcast_to_chat(chat_id, {"type": "new_message", "chat_id": chat_id, "message": message})


async def broadcast_message_edit(chat_id: int, message_id: int, new_text: str, edit_date: str):
    """Broadcast a message edit to subscribed clients."""
    await ws_manager.broadcast_to_chat(
        chat_id,
        {"type": "edit", "chat_id": chat_id, "message_id": message_id, "new_text": new_text, "edit_date": edit_date},
    )


async def broadcast_message_delete(chat_id: int, message_id: int):
    """Broadcast a message deletion to subscribed clients."""
    await ws_manager.broadcast_to_chat(chat_id, {"type": "delete", "chat_id": chat_id, "message_id": message_id})
