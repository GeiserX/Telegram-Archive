"""
Web viewer for Telegram Backup.

FastAPI application providing a web interface to browse backed-up messages.
v3.0: Async database operations with SQLAlchemy.
v5.0: WebSocket support for real-time updates and notifications.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import traceback
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Config
from ..db import DatabaseAdapter, close_database, get_db_manager, init_database
from ..db.adapter import ChatScope, parse_entitlement_column
from ..db.models import DEFAULT_ACCOUNT_ID, account_metadata_key
from ..message_utils import describe_exception, media_display_filename, resolve_sender_display_name
from ..realtime import RealtimeListener, resolve_internal_push_secret
from .media_utils import THUMBNAIL_EXTENSIONS, legacy_folder_alternates

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


@dataclass(frozen=True)
class ConnectionIdentity:
    """A socket's revocation coordinates: how a revoking path finds it again.

    The UserContext a socket carries is a SNAPSHOT of the grant it was admitted
    with, so it can never answer "is this principal still allowed in?". These
    three fields can: the session that admitted the socket, the principal that
    owns it, and the share token that minted the session (token sessions only).
    Proxy-header and anonymous sockets carry a username and nothing else.
    """

    username: str
    session_key: str | None = None
    source_token_id: int | None = None


# The viewer is documented as internet-exposed (behind a reverse proxy). A
# single client must not be able to grow server state without bound: sockets
# are capped globally (a per-IP cap would misfire behind a proxy, where every
# client arrives from the proxy's address) and each socket's subscription set
# is capped (a live SPA follows one or two chats; 16 is generous).
MAX_WS_CONNECTIONS = int(os.environ.get("MAX_WS_CONNECTIONS", "200"))
MAX_WS_SUBSCRIPTIONS_PER_CONNECTION = int(os.environ.get("MAX_WS_SUBSCRIPTIONS_PER_CONNECTION", "16"))


# WebSocket Connection Manager for real-time updates
class ConnectionManager:
    """Manages WebSocket connections for real-time updates.

    Phase 4: subscriptions are keyed by the chat's opaque ref, and each socket
    carries the UserContext it authenticated with. Entitlement for a subscribe
    is enforced by the endpoint through the SAME resolver the HTTP routes use;
    broadcast_to_chat re-checks the chat against the socket's context so a frame
    can never outrun the grant it rode in on.

    A socket also carries its ConnectionIdentity, which is what makes revocation
    reach it: the grant snapshot cannot notice that its principal was logged
    out, disabled, deleted, revoked or expired, so close_for() closes the socket
    from the outside when any of those happen.
    """

    def __init__(self):
        self.active_connections: dict[WebSocket, set[str]] = {}
        self._contexts: dict[WebSocket, UserContext] = {}
        self._identities: dict[WebSocket, ConnectionIdentity] = {}

    async def connect(
        self, websocket: WebSocket, user: UserContext, identity: ConnectionIdentity | None = None
    ) -> bool:
        if len(self.active_connections) >= MAX_WS_CONNECTIONS:
            # 1013 = Try Again Later. Accepted then closed so the client gets a
            # real close code instead of an opaque handshake failure.
            await websocket.accept()
            await websocket.close(code=1013, reason="Too many connections")
            logger.warning(f"WebSocket refused: connection cap reached ({MAX_WS_CONNECTIONS})")
            return False
        # Reserve the slot BEFORE the first await: between the cap check above
        # and these registrations there is no suspension point, so concurrent
        # connection tasks cannot all pass the check and register after
        # accept() suspends — the cap would be advisory otherwise.
        self.active_connections[websocket] = set()
        self._contexts[websocket] = user
        self._identities[websocket] = identity or ConnectionIdentity(username=user.username)
        try:
            await websocket.accept()
        except BaseException:
            self.disconnect(websocket)
            raise
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        self.active_connections.pop(websocket, None)
        self._contexts.pop(websocket, None)
        self._identities.pop(websocket, None)
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def close_for(
        self,
        *,
        username: str | None = None,
        session_keys: Iterable[str] | None = None,
        source_token_id: int | None = None,
    ) -> int:
        """Close every socket held by a revoked principal. Returns how many.

        A socket matches when ANY supplied coordinate matches its identity —
        each caller passes exactly the coordinate it is revoking (one session
        key on logout and on expiry, a username on viewer disable/delete, a
        token id on share-token revoke). 4001 is the same code the upgrade
        path already closes with when a socket cannot be tied to a live
        principal, so a client sees one "you are no longer authenticated"
        signal however the grant ended.
        """
        keys = set(session_keys) if session_keys is not None else set()
        revoked = [
            websocket
            for websocket, identity in self._identities.items()
            if (username is not None and identity.username == username)
            or (identity.session_key is not None and identity.session_key in keys)
            or (source_token_id is not None and identity.source_token_id == source_token_id)
        ]
        for websocket in revoked:
            # Drop the socket's state BEFORE awaiting the close: a broadcast
            # landing in another task while the close frame is in flight must
            # not find a revoked socket still subscribed.
            self.disconnect(websocket)
            try:
                await websocket.close(code=4001, reason="Session revoked")
            except Exception as e:
                # Already-closing sockets raise here; the state is gone either way.
                logger.debug(f"Closing a revoked websocket raised {type(e).__name__}")
        if revoked:
            logger.info(f"Closed {len(revoked)} websocket(s) for a revoked principal")
        return len(revoked)

    def subscribe(self, websocket: WebSocket, chat_ref: str) -> bool:
        """Record a subscription for an already-authorized chat ref.

        Bounded per connection: re-subscribing an existing ref stays free, a
        NEW ref past the cap is refused — otherwise one socket looping over
        distinct refs grows this set (and every broadcast's work) without
        limit.
        """
        subs = self.active_connections.get(websocket)
        if subs is None:
            return False
        if chat_ref not in subs and len(subs) >= MAX_WS_SUBSCRIPTIONS_PER_CONNECTION:
            return False
        subs.add(chat_ref)
        return True

    def unsubscribe(self, websocket: WebSocket, chat_ref: str):
        """Unsubscribe a connection from a specific chat."""
        if websocket in self.active_connections:
            self.active_connections[websocket].discard(chat_ref)

    async def broadcast_to_chat(self, chat: dict, message: dict):
        """Broadcast a message to every connection subscribed AND entitled to ``chat``.

        ``chat`` is the resolved chat row (id, account_id, ref); ``message`` is
        the ref-addressed frame to deliver.
        """
        disconnected = []
        # Snapshot first: send_json suspends, and a connect/disconnect landing in
        # another task during that await mutates active_connections. Iterating it
        # live raises RuntimeError at the `for`, which aborts the whole broadcast
        # and silently drops the event for every client not yet reached.
        for websocket, subscribed_refs in list(self.active_connections.items()):
            if chat["ref"] not in subscribed_refs:
                continue
            user = self._contexts.get(websocket)
            if user is None or not _chat_visible(user, chat):
                continue
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
        # Same snapshot rule as broadcast_to_chat: the dict can change under us.
        for websocket in list(self.active_connections):
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
# Wrap construction so an invalid env value (e.g. a bad DELETION_MODE) surfaces a
# clear message instead of an opaque ASGI import-time traceback / crash-loop.
try:
    config = Config()
except ValueError as e:
    logger.error(f"Configuration error: {e}")
    raise

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
    # The specific ids here are the operator's own config, but this runs in the
    # long-lived viewer whose logs ship off-box, so it reports COUNTS rather than
    # the ids themselves (the chat-id logging rule). "Which id" is answerable from
    # the operator's own DISPLAY_CHAT_IDS setting; the warning only needs to say
    # that some entry did not resolve.
    auto_corrected = 0
    unresolved = 0
    for chat_id in config.display_chat_ids:
        if chat_id in existing_ids:
            # ID exists as-is
            normalized.add(chat_id)
        elif chat_id > 0:
            # Positive ID not found - try -100 prefix (channel/supergroup format)
            marked_id = -1000000000000 - chat_id
            if marked_id in existing_ids:
                auto_corrected += 1
                normalized.add(marked_id)
            else:
                unresolved += 1
                normalized.add(chat_id)  # Keep original, might be backed up later
        else:
            # Negative ID not found
            unresolved += 1
            normalized.add(chat_id)

    if auto_corrected:
        logger.warning(
            f"DISPLAY_CHAT_IDS: auto-corrected {auto_corrected} positive "
            f"entr{'y' if auto_corrected == 1 else 'ies'} to marked (channel/supergroup) format"
        )
    if unresolved:
        logger.warning(
            f"DISPLAY_CHAT_IDS: {unresolved} configured chat(s) not found in the database; "
            "verify the DISPLAY_CHAT_IDS setting"
        )

    config.display_chat_ids = normalized


# Background tasks
stats_task: asyncio.Task | None = None
_session_cleanup_task: asyncio.Task | None = None

# Real-time listener (PostgreSQL LISTEN/NOTIFY)
realtime_listener: RealtimeListener | None = None

# Push notification manager (Web Push API)
push_manager: PushNotificationManager | None = None


# The realtime/push side resolves a writer-side chat id to its row (ref, account,
# title) once per event; a short TTL keeps a busy chat from re-querying per event.
_broadcast_chat_cache: dict[tuple[int | None, int], tuple[float, dict | None]] = {}
_BROADCAST_CHAT_CACHE_TTL_SECONDS = 60


async def _broadcast_chat_row(chat_id: int, account_id: int | None = None) -> dict | None:
    """Chat row for a realtime event's chat id, or None when it cannot be addressed.

    The writer side (listener/backup) speaks chat ids; every outward frame and
    push payload speaks refs. Since 8.0 two accounts can share a chat id, so
    the payload's capturing account (#315) scopes the lookup to the row that
    was actually written. A legacy payload without account_id keeps the old
    guard: an id that resolves to no row — or ambiguously — drops the event
    rather than emit a frame that names a chat id.
    """
    if not db:
        return None
    cache_key = (account_id, chat_id)
    cached = _broadcast_chat_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] <= _BROADCAST_CHAT_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        chat = await db.get_chat_by_id(chat_id, account_id=account_id)
    except Exception as e:
        logger.warning(f"Realtime chat resolution failed ({type(e).__name__}); dropping event")
        return None
    if chat is not None and not chat.get("ref"):
        chat = None
    _broadcast_chat_cache[cache_key] = (time.monotonic(), chat)
    return chat


async def handle_realtime_notification(payload: dict):
    """Handle real-time notifications and broadcast to WebSocket clients + push notifications."""
    notification_type = payload.get("type")
    chat_id = payload.get("chat_id")
    # The capturing account (in the payload since #315). bool is an int
    # subclass, so exclude it explicitly; any non-int falls back to the
    # legacy unscoped lookup and its drop-on-ambiguity guard.
    account_id = payload.get("account_id")
    if not isinstance(account_id, int) or isinstance(account_id, bool):
        account_id = None
    data = payload.get("data", {})

    # Check if this chat is allowed (respects DISPLAY_CHAT_IDS restriction)
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        # This viewer is restricted to specific chats, ignore notifications for other chats
        return

    chat = await _broadcast_chat_row(chat_id, account_id)
    if chat is None:
        return
    chat_ref = chat["ref"]

    if notification_type == "new_message":
        await ws_manager.broadcast_to_chat(
            chat, {"type": "new_message", "chat_ref": chat_ref, "message": data.get("message")}
        )

        # Send Web Push notification for new messages
        if push_manager and push_manager.is_enabled:
            message = data.get("message", {})
            chat_title = chat.get("title") or "Telegram"

            # One precedence rule for "who sent this row" — the shared helper,
            # fed from the payload the listener already enriches with the API
            # row shape (first/last/username), so the common path costs no DB
            # query. The lookup below remains only for payloads that carry no
            # user fields at all.
            sender_name = resolve_sender_display_name(
                message.get("sender_name"),
                message.get("first_name"),
                message.get("last_name"),
                message.get("username"),
            )
            if sender_name is None and message.get("sender_id") and db:
                sender = await db.get_user_by_id(message.get("sender_id"))
                if sender:
                    sender_name = resolve_sender_display_name(
                        None, sender.get("first_name"), sender.get("last_name"), sender.get("username")
                    )
            sender_name = sender_name or ""

            await push_manager.notify_new_message(
                chat_id=chat_id,
                chat_ref=chat_ref,
                chat_title=chat_title,
                sender_name=sender_name,
                message_text=message.get("text", "") or "[Media]",
                message_id=message.get("id", 0),
                account_id=chat.get("account_id"),
            )

    elif notification_type == "edit":
        await ws_manager.broadcast_to_chat(
            chat,
            {
                "type": "edit",
                "chat_ref": chat_ref,
                "message_id": data.get("message_id"),
                "new_text": data.get("new_text"),
                "edit_date": data.get("edit_date"),
            },
        )
    elif notification_type == "delete":
        await ws_manager.broadcast_to_chat(
            chat,
            {
                "type": "delete",
                "chat_ref": chat_ref,
                "message_id": data.get("message_id"),
                "deletion_mode": data.get("deletion_mode", "hard"),
                "deleted_at": data.get("deleted_at"),
            },
        )
    elif notification_type == "pin":
        await ws_manager.broadcast_to_chat(
            chat,
            {
                "type": "pin",
                "chat_ref": chat_ref,
                "message_ids": data.get("message_ids", []),
                "pinned": data.get("pinned", True),
            },
        )
    elif notification_type == "reaction":
        await ws_manager.broadcast_to_chat(
            chat,
            {
                "type": "reaction",
                "chat_ref": chat_ref,
                "message_id": data.get("message_id"),
                "reactions": data.get("reactions", []),
            },
        )


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
                logger.info(f"Cleaned up {len(expired)} expired sessions from cache")
                # An expired session must lose its socket too, or the principal
                # keeps receiving frames from the grant it was admitted with.
                # Closing them HERE rather than re-checking the session on the
                # broadcast path is the choice that cannot regress delivery
                # latency: this runs once per sweep over a list the sweep has
                # already built, while a per-frame check would add a session
                # lookup to every message for every subscribed socket.
                await ws_manager.close_for(session_keys=expired)
            # Also clean DB
            if db:
                try:
                    db_cleaned = await db.cleanup_expired_sessions(AUTH_SESSION_SECONDS)
                    if db_cleaned:
                        logger.info(f"Cleaned up {db_cleaned} expired sessions from database")
                except Exception as e:
                    logger.warning(f"DB session cleanup failed: {e}")
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
                next_run = next_run + timedelta(days=1)

            # Wait until next run
            wait_seconds = (next_run - now).total_seconds()
            logger.info(
                f"Stats calculation scheduled for {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds / 3600:.1f}h from now)"
            )
            await asyncio.sleep(wait_seconds)

            # Run stats calculation
            logger.info("Running scheduled stats calculation...")
            await db.calculate_and_store_statistics(storage_path=config.backup_path)
            logger.info("Stats calculation completed")

        except asyncio.CancelledError:
            logger.info("Stats calculation scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"Error in stats calculation scheduler: {describe_exception(e)}")
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
            await db.calculate_and_store_statistics(storage_path=config.backup_path)
        except Exception as e:
            logger.warning(f"Initial stats calculation failed: {e}")

    # Restore persistent sessions from database
    if AUTH_ENABLED:
        try:
            rows = await db.load_all_sessions()
            now = time.time()
            restored = 0
            for row in rows:
                if now - row["created_at"] > AUTH_SESSION_SECONDS:
                    continue  # skip expired, cleanup task will purge from DB
                # Grants come from the v8.0.0 columns only; an unreadable grant
                # restores as an EMPTY one (sees nothing) rather than being
                # dropped or widened. The legacy allowed_chat_ids is never read.
                allowed_accounts, allowed_chat_refs = _grants_from_row(row)
                _sessions[row["token"]] = SessionData(
                    username=row["username"],
                    role=row["role"],
                    allowed_accounts=allowed_accounts,
                    allowed_chat_refs=allowed_chat_refs,
                    no_download=bool(row.get("no_download", 0)),
                    source_token_id=row.get("source_token_id"),
                    created_at=row["created_at"],
                    last_accessed=row["last_accessed"],
                )
                restored += 1
            if restored:
                logger.info(f"Restored {restored} sessions from database")
        except Exception as e:
            logger.warning(f"Failed to restore sessions from database: {e}")

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

        push_manager = PushNotificationManager(db, config, configured_principals=_configured_principals())
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
        # Every third-party asset is vendored under /static/vendor, so no
        # remote host belongs in this policy: a compromised CDN can no longer
        # ship script into the archive session at all. 'unsafe-inline' and
        # 'unsafe-eval' remain for the inline SPA script and the Tailwind
        # play runtime.
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        # 'self' also covers the same-origin /ws/updates socket: CSP3 matches
        # ws/wss upgrades of the page origin. Scheme-wide ws:/wss: would let
        # injected script exfiltrate to any WebSocket host.
        "connect-src 'self'; "
        "font-src 'self'"
    )
    return response


# ============================================================================
# Multi-User Authentication (v7.0.0)
# ============================================================================


def _sanitize_theme_slug(value: str) -> str:
    """A theme id is a strict slug or nothing.

    The value is baked into the served page inside a JS string (the pre-paint
    boot script), so anything that is not a plain slug is dropped rather than
    escaped. The client additionally checks it against its theme list.
    """
    value = value.strip().lower()
    return value if re.fullmatch(r"[a-z]{3,16}", value) else ""


def _sanitize_chat_background(value: str) -> str:
    """The chat wallpaper's file name under /static, or "" for none.

    A bare name: no separator can appear, so the value cannot address anything
    outside the static directory, and no quote, parenthesis, semicolon or angle
    bracket can appear, so it cannot break out of the CSS declaration it is
    baked into. Anything else is dropped rather than escaped, like the theme
    slug above. A name that matches but does not exist simply 404s and the pane
    keeps its plain background.
    """
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) else ""


def _chat_background_css(name: str) -> str:
    """The declarations that turn the wallpaper on, or "" when there is none.

    Injected at the end of :root so it overrides the defaults declared there.
    The bubbles go opaque with it: the themes make them translucent so the flat
    background shows through, and over a photo that would put a picture behind
    running text. The tint is the theme's own background colour, so one image
    sits correctly under a light and a dark palette instead of only one of them.
    """
    if not name:
        return ""
    return (
        f"--viewer-chat-background: url('/static/{name}');"
        " --viewer-chat-tint: linear-gradient(rgb(var(--tg-bg) / 0.55), rgb(var(--tg-bg) / 0.55));"
        " --tg-bubble-alpha-own: 1; --tg-bubble-alpha-other: 1;"
        " --tg-chip-bg: rgb(var(--tg-sidebar));"
        " --tg-service-bg: rgb(var(--tg-other)); --tg-service-fg: rgb(var(--tg-text));"
        " --tg-pane-note-opacity: 1;"
    )


# Default palette for browsers with no saved choice. A user's picker choice
# (localStorage) always wins over this.
VIEWER_DEFAULT_THEME = _sanitize_theme_slug(os.getenv("VIEWER_DEFAULT_THEME", ""))
VIEWER_CHAT_BACKGROUND = _sanitize_chat_background(os.getenv("VIEWER_CHAT_BACKGROUND", ""))
_configured_chat_background = os.getenv("VIEWER_CHAT_BACKGROUND", "").strip()
if _configured_chat_background and not VIEWER_CHAT_BACKGROUND:
    # A name that cannot be a name: say so, or the pane is just silently plain.
    logger.warning("VIEWER_CHAT_BACKGROUND is not a plain file name and was ignored")
elif VIEWER_CHAT_BACKGROUND and not (Path(__file__).parent / "static" / VIEWER_CHAT_BACKGROUND).is_file():
    logger.warning(
        "VIEWER_CHAT_BACKGROUND names a file the viewer cannot see; mount it into the "
        "container's static directory (see docker-compose.yml)"
    )

VIEWER_USERNAME = os.getenv("VIEWER_USERNAME", "").strip()
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD", "").strip()
AUTH_ENABLED = bool(VIEWER_USERNAME and VIEWER_PASSWORD)
ALLOW_ANONYMOUS_VIEWER = os.getenv("ALLOW_ANONYMOUS_VIEWER", "false").lower() == "true"
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"
AUTH_COOKIE_NAME = "viewer_auth"

# Trusted Proxy Authentication (v7.9.0)
AUTH_PROXY_HEADER = os.getenv("AUTH_PROXY_HEADER", "").strip()
AUTH_PROXY_ADMIN_USERS = {u.strip() for u in os.getenv("AUTH_PROXY_ADMIN_USERS", "").split(",") if u.strip()}
AUTH_PROXY_DEFAULT_ACCESS = os.getenv("AUTH_PROXY_DEFAULT_ACCESS", "none").strip().lower()
_PROXY_AUTH_ENABLED = bool(AUTH_PROXY_HEADER)

AUTH_SESSION_DAYS = int(os.getenv("AUTH_SESSION_DAYS", "30"))
AUTH_SESSION_SECONDS = AUTH_SESSION_DAYS * 24 * 60 * 60
_MAX_SESSIONS_PER_USER = 10
_SESSION_CLEANUP_INTERVAL = 900  # 15 minutes
_LOGIN_RATE_LIMIT = 15  # max attempts
_LOGIN_RATE_WINDOW = 300  # per 5 minutes

if AUTH_ENABLED:
    logger.info(f"Viewer authentication is ENABLED (Master: {VIEWER_USERNAME}, Session: {AUTH_SESSION_DAYS} days)")
elif _PROXY_AUTH_ENABLED:
    logger.info(f"Trusted proxy authentication is ENABLED (Header: {AUTH_PROXY_HEADER})")
elif ALLOW_ANONYMOUS_VIEWER:
    logger.warning("Viewer authentication is DISABLED by explicit ALLOW_ANONYMOUS_VIEWER=true")
else:
    logger.error(
        "Viewer authentication is not configured. Set VIEWER_USERNAME/VIEWER_PASSWORD or ALLOW_ANONYMOUS_VIEWER=true"
    )


@dataclass
class UserContext:
    """The authenticated principal and its v8.0.0 entitlement.

    ``allowed_accounts``/``allowed_chat_refs``: None = unrestricted; a set is
    the grant, and the EMPTY set (what an unparseable stored grant parses to)
    denies everything. The legacy allowed_chat_ids column no longer reaches
    this context — 8.0 code never reads it.
    """

    username: str
    role: str  # "master", "viewer", or "token"
    allowed_accounts: set[int] | None = None  # None = all accounts
    allowed_chat_refs: set[str] | None = None  # None = all chats
    no_download: bool = False  # v7.2.0: restrict file downloads


@dataclass
class SessionData:
    username: str
    role: str
    allowed_accounts: set[int] | None = None
    allowed_chat_refs: set[str] | None = None
    no_download: bool = False
    source_token_id: int | None = None  # v7.2.0: tracks originating share token for revocation
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


_sessions: dict[str, SessionData] = {}
_login_attempts: dict[str, list[float]] = {}  # ip -> list of timestamps


def _grants_from_row(row: dict) -> tuple[set[int] | None, set[str] | None]:
    """(allowed_accounts, allowed_chat_refs) of a viewer/session/token row, fail-closed.

    The grant lives ONLY in the v8.0.0 columns migration 022 populated. Each
    parses independently — NULL means unrestricted, anything unreadable becomes
    the empty set, which denies. The legacy allowed_chat_ids is never a grant
    source; its one permitted use is the deny-only guard below: a row carrying
    a legacy grant while BOTH new columns are NULL is an unconverted 7.x
    restriction (022 converts every such row, so a live one means the migration
    was bypassed), and reading it as unrestricted would be exactly the fail-open
    this design exists to prevent. Such a row gets the empty grant.
    """
    accounts = parse_entitlement_column(row.get("allowed_accounts"), int)
    refs = parse_entitlement_column(row.get("allowed_chat_refs"), str)
    if accounts is None and refs is None and row.get("allowed_chat_ids") is not None:
        logger.warning("Viewer identity carries an unconverted legacy grant; denying all chats")
        return set(), set()
    return accounts, refs


@dataclass(frozen=True)
class ChatContext:
    """One resolved, entitlement-checked chat: what a ref-addressed route works with."""

    account_id: int
    chat_id: int
    ref: str
    type: str | None = None


def _chat_scope(user: UserContext) -> ChatScope:
    """The three visibility rules for ``user``, as one object Python and SQL share.

    This is where the operator's config and the session's grant are combined,
    and the ONLY place that combination is written down. Two different
    meanings of "empty" meet here, so both are spelled out:

    * ``config.display_chat_ids`` is operator config — unset (empty/None) means
      the operator asked for no filter, so it becomes ``ids=None``.
    * ``allowed_accounts`` / ``allowed_chat_refs`` are entitlements — ``None``
      means unrestricted, and the EMPTY set means entitled to nothing and is
      passed through as an empty set so it denies every chat.

    ChatScope.allows() then decides per row (websocket delivery, ref resolver)
    and ChatScope.sql_predicates() decides in the WHERE clause (the chat list),
    from these same three fields.
    """
    return ChatScope.build(
        ids=config.display_chat_ids or None,
        accounts=user.allowed_accounts,
        refs=user.allowed_chat_refs,
    )


def _chat_visible(user: UserContext, chat: dict) -> bool:
    """Whether ``user`` may see ``chat`` (a row dict carrying id, account_id, ref).

    One rule for every surface — HTTP routes, websocket broadcast, and the
    resolver below all decide through here, and the chat list decides through
    the SQL twin of the very same ChatScope. The operator's DISPLAY_CHAT_IDS
    filter binds every role (as get_user_chat_ids did); entitlements bind
    sessions whose grant is a set (masters carry None).
    """
    return _chat_scope(user).allows(chat)


def _user_is_restricted(user: UserContext) -> bool:
    """True when the visible-chat set is narrower than "everything"."""
    return not _chat_scope(user).unrestricted


async def _visible_chat_id_set(user: UserContext) -> set[int] | None:
    """Chat ids the user may see, or None when unrestricted.

    The bridge that lets id-keyed internals (folder counts, cached stats) keep
    working: entitlements are ref-based, so the ids come from the chat rows the
    grant selects — selected BY the grant in SQL and read as bare ids, so a
    viewer entitled to one chat reads one id and no message dates. Single-account caveat: the ids are bare (phase 5 will
    need account-qualified sets once a second account can collide on an id).
    """
    scope = _chat_scope(user)
    if scope.unrestricted:
        return None
    return await db.get_visible_chat_ids(scope)


async def _resolve_chat_ref(chat_ref: str, user: UserContext) -> ChatContext:
    """The ONE resolver: opaque ref -> entitled ChatContext, or 404.

    Unknown, malformed, and forbidden refs are indistinguishable — same status,
    same body, and the same single indexed SELECT for every candidate string,
    so neither the response nor its latency says whether a chat exists. A 503
    (database down) is the only other outcome; it carries no per-chat signal.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        chat = await db.get_chat_by_ref(chat_ref)
    except Exception as e:
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise
    if not chat or not _chat_visible(user, chat):
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatContext(account_id=chat["account_id"], chat_id=chat["id"], ref=chat["ref"], type=chat.get("type"))


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()


def _verify_password(password: str, salt: str, password_hash: str) -> bool:
    return secrets.compare_digest(_hash_password(password, salt), password_hash)


def _hash_token(plaintext_token: str, salt: str) -> str:
    """Share-token twin of _hash_password: token salts are hex, password salts are raw.

    600k rounds take ~50ms, so every caller must run this via asyncio.to_thread —
    inline it would stall the event loop for the duration (matches the login/
    create-viewer/update-viewer hashing sites).
    """
    return hashlib.pbkdf2_hmac("sha256", plaintext_token.encode(), bytes.fromhex(salt), 600_000).hex()


def _check_rate_limit(ip: str) -> bool:
    """Returns True if the request is within rate limits."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_RATE_LIMIT


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


def _get_client_ip(request: Request) -> str:
    """Return the rate-limit/audit IP, only trusting proxy headers when explicitly enabled."""
    direct_ip = request.client.host if request.client else "unknown"
    if not TRUST_PROXY_HEADERS:
        return direct_ip

    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or request.headers.get("x-real-ip", "") or direct_ip


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Allow same-origin WebSockets and explicitly configured CORS origins."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    parsed = urlparse(origin)
    origin_host = parsed.netloc
    host = websocket.headers.get("host", "")
    if origin_host and origin_host == host:
        return True

    allowed_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
    return origin in allowed_origins


try:
    from asyncpg import CannotConnectNowError, PostgresConnectionError, TooManyConnectionsError

    _ASYNCPG_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
        PostgresConnectionError,
        TooManyConnectionsError,
        CannotConnectNowError,
    )
except ImportError:  # asyncpg may be absent when running SQLite-only installs
    _ASYNCPG_CONNECTION_ERRORS = ()


def _is_db_connection_error(exc: Exception) -> bool:
    """Check if an exception indicates the database is unreachable (vs. e.g. a constraint violation)."""
    current: BaseException | None = exc
    for _ in range(10):
        if current is None:
            break
        # Only connection-shaped OS errors count. Bare OSError is the base of
        # every filesystem fault (NotADirectoryError, PermissionError, ENOSPC),
        # and matching it here sent operators to debug the database while the
        # media volume or thumbnail cache was the broken part. DB connect and
        # DNS failures on database paths arrive wrapped in OperationalError,
        # which the next branch already catches.
        if isinstance(current, ConnectionError | TimeoutError):
            return True
        if isinstance(current, OperationalError):
            return True
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        if _ASYNCPG_CONNECTION_ERRORS and isinstance(current, _ASYNCPG_CONNECTION_ERRORS):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


async def _create_session(
    username: str,
    role: str,
    allowed_accounts: set[int] | None = None,
    allowed_chat_refs: set[str] | None = None,
    no_download: bool = False,
    source_token_id: int | None = None,
) -> str:
    """Create a new session, evicting oldest if user exceeds max sessions."""
    user_sessions = [(k, v) for k, v in _sessions.items() if v.username == username]
    if len(user_sessions) >= _MAX_SESSIONS_PER_USER:
        user_sessions.sort(key=lambda x: x[1].created_at)
        evicted = [token for token, _ in user_sessions[: len(user_sessions) - _MAX_SESSIONS_PER_USER + 1]]
        for token in evicted:
            _sessions.pop(token, None)
            if db:
                try:
                    await db.delete_session(token)
                except Exception:
                    pass
        # Eviction deletes a session like any other revocation, so it closes
        # that session's sockets too.
        await ws_manager.close_for(session_keys=evicted)

    now = time.time()
    token = secrets.token_urlsafe(32)
    _sessions[token] = SessionData(
        username=username,
        role=role,
        allowed_accounts=allowed_accounts,
        allowed_chat_refs=allowed_chat_refs,
        no_download=no_download,
        source_token_id=source_token_id,
        created_at=now,
        last_accessed=now,
    )

    # Persist to database
    if db:
        try:
            accounts_json = json.dumps(sorted(allowed_accounts)) if allowed_accounts is not None else None
            refs_json = json.dumps(sorted(allowed_chat_refs)) if allowed_chat_refs is not None else None
            restricted = accounts_json is not None or refs_json is not None
            await db.save_session(
                token=token,
                username=username,
                role=role,
                # Rollback tombstone: a 7.x binary reads allowed_chat_ids, and
                # NULL there means "everything" — so a restricted 8.0 session
                # writes the empty grant, which denies under rollback too.
                allowed_chat_ids="[]" if restricted else None,
                allowed_accounts=accounts_json,
                allowed_chat_refs=refs_json,
                created_at=now,
                last_accessed=now,
                no_download=1 if no_download else 0,
                source_token_id=source_token_id,
            )
        except Exception as e:
            logger.warning(f"Failed to persist session to database: {e}")

    return token


async def _purge_push_subscriptions(username: str) -> None:
    """Delete every stored push channel owned by ``username``.

    A push subscription outlives every session: the push service delivers to
    the browser with no cookie and no socket involved, so revoking sessions
    alone leaves a revoked principal still being notified. Best-effort by
    design — a failure here must not abort the rest of a revocation, and
    PushNotificationManager.get_subscriptions re-checks owner liveness at send
    time as the backstop for exactly that case.

    Deletion is per USERNAME, not per browser: the server cannot tell which
    endpoint belongs to which session, so logging out of one browser drops the
    user's other browsers' subscriptions too. They re-subscribe on their next
    load; the reverse (leaving a revoked channel armed) is the unsafe half.
    """
    if not db:
        return
    try:
        deleted = await db.delete_push_subscriptions_for_username(username=username)
    except Exception as e:
        logger.warning(f"Failed to delete push subscriptions ({type(e).__name__})")
        return
    if deleted:
        logger.info(f"Deleted {deleted} push subscription(s) for a revoked principal")


async def _invalidate_user_sessions(username: str) -> None:
    """Revoke everything a username holds: sessions, open sockets, push channels.

    The single choke point for viewer update/disable/delete. Sessions are only
    the credential; the socket and the push subscription are live delivery
    channels that keep working after the session row is gone, so all three end
    here.
    """
    to_remove = [k for k, v in _sessions.items() if v.username == username]
    for k in to_remove:
        _sessions.pop(k, None)
    if db:
        try:
            await db.delete_user_sessions(username)
        except Exception as e:
            logger.warning(f"Failed to delete DB sessions for {username}: {e}")
    await ws_manager.close_for(username=username)
    await _purge_push_subscriptions(username)


async def _invalidate_token_sessions(token_id: int) -> None:
    """Revoke everything a share token holds (on revoke/delete/update).

    The token's push channels are stored under the session username the token
    minted, so they are purged for each username the revoked sessions carried.
    A token whose sessions are all already gone leaves no username to purge
    here — get_subscriptions' owner-liveness check is what silences that row.
    """
    to_remove = [(k, v.username) for k, v in _sessions.items() if v.source_token_id == token_id]
    for k, _ in to_remove:
        _sessions.pop(k, None)
    if db:
        try:
            await db.delete_sessions_by_source_token_id(token_id)
        except Exception as e:
            logger.warning(f"Failed to delete token sessions for token_id={token_id}: {e}")
    await ws_manager.close_for(source_token_id=token_id)
    for username in {username for _, username in to_remove}:
        await _purge_push_subscriptions(username)


def _configured_principals() -> set[str]:
    """Usernames that exist by CONFIGURATION rather than by a viewer_accounts row.

    The env master, the trusted-proxy admins and the anonymous viewer never
    have an account row, so a liveness check against viewer_accounts would read
    them as deleted and silence their push notifications. The database cannot
    prove a configured principal dead; only the operator's config can.
    """
    names = {VIEWER_USERNAME, *AUTH_PROXY_ADMIN_USERS}
    if ALLOW_ANONYMOUS_VIEWER:
        names.add("anonymous")
    return {name for name in names if name}


def _get_secure_cookies(request: Request) -> bool:
    secure_env = os.getenv("SECURE_COOKIES", "").strip().lower()
    if secure_env == "true":
        return True
    if secure_env == "false":
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto == "https" or str(request.url.scheme) == "https"


async def _resolve_session(auth_cookie: str) -> SessionData | None:
    """Look up session from in-memory cache, falling back to DB if needed."""
    session = _sessions.get(auth_cookie)
    if session:
        return session

    if not db:
        return None

    try:
        row = await db.get_session(auth_cookie)
    except Exception:
        return None

    if not row or time.time() - row["created_at"] > AUTH_SESSION_SECONDS:
        return None

    # v8.0.0 grant columns only; unreadable parses to the empty grant (denies).
    allowed_accounts, allowed_chat_refs = _grants_from_row(row)

    session = SessionData(
        username=row["username"],
        role=row["role"],
        allowed_accounts=allowed_accounts,
        allowed_chat_refs=allowed_chat_refs,
        no_download=bool(row.get("no_download", 0)),
        source_token_id=row.get("source_token_id"),
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
    )
    _sessions[auth_cookie] = session
    return session


async def _resolve_proxy_user(proxy_username: str) -> UserContext:
    """Resolve a trusted proxy-authenticated user to a UserContext.

    Admin users (in AUTH_PROXY_ADMIN_USERS) get master role with full access.
    Other users are auto-created as viewer accounts with access determined by
    AUTH_PROXY_DEFAULT_ACCESS (none = no chats until admin grants, all = full access).
    """
    if proxy_username in AUTH_PROXY_ADMIN_USERS:
        return UserContext(username=proxy_username, role="master")

    # Look up or auto-create viewer account
    if db:
        viewer = await db.get_viewer_by_username(proxy_username)
        if viewer:
            if not viewer["is_active"]:
                raise HTTPException(status_code=403, detail="Account disabled")
            allowed_accounts, allowed_chat_refs = _grants_from_row(viewer)
            return UserContext(
                username=proxy_username,
                role="viewer",
                allowed_accounts=allowed_accounts,
                allowed_chat_refs=allowed_chat_refs,
                no_download=bool(viewer.get("no_download", 0)),
            )

        # Auto-create with configured default access. The grant lives in
        # allowed_chat_refs; allowed_chat_ids carries the matching rollback
        # tombstone ("[]" = nothing under 7.x too, never fail-open NULL).
        refs_json = None  # None = all chats
        if AUTH_PROXY_DEFAULT_ACCESS != "all":
            refs_json = "[]"  # Empty = no chats until admin grants access
        await db.create_viewer_account(
            username=proxy_username,
            password_hash="",
            salt="proxy-auth",
            allowed_chat_ids=refs_json,
            allowed_chat_refs=refs_json,
            created_by="proxy-auth",
            is_active=1,
        )
        logger.info(f"Auto-created proxy-authenticated viewer account: {proxy_username}")

        refs_set: set[str] | None = None if AUTH_PROXY_DEFAULT_ACCESS == "all" else set()
        return UserContext(username=proxy_username, role="viewer", allowed_chat_refs=refs_set)

    # No DB — proxy admin users are the only ones that work without DB
    raise HTTPException(status_code=503, detail="Database required for proxy authentication")


async def _resolve_user_context(proxy_header_value: str | None, auth_cookie: str | None) -> UserContext:
    """Resolve the authenticated principal from a proxy header value and a session cookie.

    One resolver for every transport. The proxy header is tried first and, when
    it is absent, resolution FALLS THROUGH to the session cookie — enabling
    AUTH_PROXY_HEADER never turns the cookie check off. The WebSocket upgrade
    used to make that decision on its own and skipped the cookie branch whenever
    proxy auth was configured, which admitted credential-less sockets and gave
    authenticated viewers a socket with no chat ACL.

    Raises HTTPException when the caller cannot be tied to a principal.
    """
    if not AUTH_ENABLED and not _PROXY_AUTH_ENABLED:
        if ALLOW_ANONYMOUS_VIEWER:
            # Read-only viewer, not master: anonymous internet users must never get
            # admin capabilities (create viewers, mint tokens, read audit log, settings).
            return UserContext(username="anonymous", role="viewer", no_download=False)
        raise HTTPException(status_code=503, detail="Viewer authentication is not configured")

    # Trusted proxy header authentication (v7.9.0)
    if _PROXY_AUTH_ENABLED:
        proxy_user = (proxy_header_value or "").strip()
        if proxy_user:
            return await _resolve_proxy_user(proxy_user)

    if not AUTH_ENABLED:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not auth_cookie:
        raise HTTPException(status_code=401, detail="Unauthorized")

    session = await _resolve_session(auth_cookie)
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if time.time() - session.created_at > AUTH_SESSION_SECONDS:
        # This request expires the session ahead of the sweep, which then never
        # sees it — so the socket it admitted has to be closed from here, or it
        # outlives every path that could have closed it.
        _sessions.pop(auth_cookie, None)
        await ws_manager.close_for(session_keys=(auth_cookie,))
        raise HTTPException(status_code=401, detail="Session expired")

    session.last_accessed = time.time()
    return UserContext(
        username=session.username,
        role=session.role,
        allowed_accounts=session.allowed_accounts,
        allowed_chat_refs=session.allowed_chat_refs,
        no_download=session.no_download,
    )


def _socket_identity(user: UserContext, auth_cookie: str | None) -> ConnectionIdentity:
    """The revocation coordinates to file a new socket under.

    Cookie-authenticated sockets are filed under their session key and, for a
    share-token session, the token that minted it — the two coordinates the
    revoking paths know. Proxy-header and anonymous sockets have no session, so
    the username is all there is; revoking such a principal is a viewer
    disable/delete, which matches on the username anyway. A cookie that
    resolved to a DIFFERENT principal than the one admitted (proxy auth won,
    with a stale cookie still attached) is not this socket's key and is
    ignored.
    """
    session = _sessions.get(auth_cookie) if auth_cookie else None
    if session is None or session.username != user.username:
        return ConnectionIdentity(username=user.username)
    return ConnectionIdentity(username=user.username, session_key=auth_cookie, source_token_id=session.source_token_id)


async def require_auth(
    request: Request, auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)
) -> UserContext:
    """Dependency that enforces session-based auth. Returns UserContext."""
    proxy_header_value = request.headers.get(AUTH_PROXY_HEADER) if _PROXY_AUTH_ENABLED else None
    return await _resolve_user_context(proxy_header_value, auth_cookie)


def require_master(request: Request, user: UserContext = Depends(require_auth)) -> UserContext:
    """Dependency that requires master role. Blocked when X-Viewer-Only header is set."""
    if user.role != "master":
        raise HTTPException(status_code=403, detail="Admin access required")
    if request.headers.get("x-viewer-only", "").lower() == "true":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_chat(chat_ref: str, user: UserContext = Depends(require_auth)) -> ChatContext:
    """Dependency behind every {chat_ref} route: resolve + entitle, or a uniform 404.

    This single dependency replaces the per-route ACL preambles: by the time a
    handler body runs, the chat exists AND the caller may see it. FastAPI's
    per-request dependency cache shares the require_auth resolution with the
    handler's own ``user`` parameter.
    """
    return await _resolve_chat_ref(chat_ref, user)


def _strip_original_media_paths(messages: list[dict]) -> None:
    """Remove original media file paths and URLs from API responses for no-download sessions."""
    for message in messages:
        media = message.get("media")
        if isinstance(media, dict):
            media["file_path"] = None
            media["url"] = None
            media["downloaded"] = False
            media["no_download"] = True
        media_items = message.get("media_items")
        if isinstance(media_items, list):
            for item in media_items:
                if isinstance(item, dict):
                    item["file_path"] = None
                    item["url"] = None
                    item["downloaded"] = False
                    item["no_download"] = True


def _export_chat_metadata(chat: dict) -> dict:
    """Return the minimal chat metadata needed by JSON exports.

    The export FILE keeps the chat id — it is the user's own data leaving the
    system, not a URL — and gains the ref so an export can be correlated with
    the viewer's addressing.
    """
    return {key: chat.get(key) for key in ("id", "ref", "type", "title", "username")}


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

# Thumbnail cache lives outside media root so it works with read-only media volumes
_thumb_cache_dir: Path | None = None


def _checked_media_path(path: str) -> str:
    """The traversal predicate every media-file path must pass before a read.

    Historically the folder segment WAS the authorization key (the pre-8.0
    path ACL read parts[0] as the chat id), and a traversal segment let the
    ACL read one folder while the filesystem read another: the ASGI server
    percent-decodes before routing, so ``%2e%2e`` reaches a route as a real
    ``..``. Authorization is row-mediated now, but the predicate still bounds
    every path that reaches the disk — avatar files funnel through here, and
    _media_relative_path applies the same rule to media rows' file_path.
    """
    if path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=403, detail="Access denied")
    return path


# Types the viewer renders inline. Anything else is served as a download, so an
# archived file can never become an active document on the viewer's own origin.
_INLINE_MEDIA_FAMILIES = frozenset({"image", "video", "audio"})
_INLINE_MEDIA_EXTRA = frozenset({"application/pdf"})
_INLINE_MEDIA_BLOCKED = frozenset({"image/svg+xml"})


def _inline_media_type(filename: str) -> str | None:
    """Content type to serve a media file with inline, or None when it must download.

    The stored name is chosen by whoever sent the file: sanitize_media_filename
    strips separators but keeps the extension verbatim, so a contact can archive
    ``report.html``. Serving that with its guessed type makes it a same-origin
    document holding the viewer's session — stored XSS with a plain attachment.
    Only the families the viewer actually renders inline (<img>, <video>,
    <audio>, plus PDF) get a real type; everything else, including SVG (an SVG
    navigated to directly executes its script), becomes an octet-stream
    attachment.
    """
    guessed, _ = mimetypes.guess_type(filename)
    if not guessed or guessed in _INLINE_MEDIA_BLOCKED:
        return None
    if guessed in _INLINE_MEDIA_EXTRA or guessed.split("/", 1)[0] in _INLINE_MEDIA_FAMILIES:
        return guessed
    return None


def _parse_media_key(media_key: str) -> tuple[int, str] | None:
    """Split the URL's ``{message_id}_{type}`` media key, or None when malformed.

    The type may itself contain underscores (``video_note``), so the split is
    on the FIRST separator only — mirroring how the storage key
    ``{chat_id}_{message_id}_{type}`` is built by the writers.
    """
    message_part, _, type_part = media_key.partition("_")
    if not type_part:
        return None
    try:
        message_id = int(message_part)
    except ValueError:
        return None
    return message_id, type_part


def _url_media_key(message_id: object, media_type: object) -> str | None:
    """The chat-free URL key ``{message_id}_{type}`` for a media row, or None.

    Built from the two values the key actually names, never by slicing the
    storage id. The sweep spells that id ``{chat}_{msg}_{type}`` so slicing
    happened to work; the Telegram Desktop importer spells it
    ``import_{chat}_{msg}``, so slicing produced no key at all and the media
    silently lost its URL (#423). The row carries ``message_id`` and ``type``
    whatever it is filed under, so this is correct for every ingest path —
    including any future one.
    """
    if message_id is None or not media_type or not isinstance(media_type, str):
        return None
    return f"{message_id}_{media_type}"


def _media_relative_path(file_path: str | None) -> str | None:
    """Normalize a media row's file_path to a media-root-relative path, or None.

    Same rules the gallery has always applied before building URLs: absolute
    paths must live under the media root (older archives stored them absolute),
    and the result must pass the traversal predicate _checked_media_path
    enforces — a row whose path cannot be proven to stay inside the root serves
    nothing.
    """
    if not file_path:
        return None
    path = file_path
    if path.startswith("/"):
        if not _media_root:
            return None
        media_root_str = str(_media_root) + "/"
        if not path.startswith(media_root_str):
            return None
        path = path[len(media_root_str) :]
    if path.startswith("/") or ".." in path.split("/"):
        return None
    return path


def _resolve_media_file(relative_path: str):
    """Resolve a root-relative media path to a real file inside the media root.

    Same containment contract as the pre-8.0 serve path: resolve(strict=True),
    the legacy positive/negative folder fallback for pre-v4.0.5 archives, and
    the is_relative_to bound that keeps a symlink from escaping the root.
    Returns the resolved Path or None.
    """
    candidate = _media_root / relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError, ValueError:
        # Legacy fallback: pre-v4.0.5 paths used positive IDs, disk uses negative marked IDs.
        # Try alternate folder names: X→-X (basic group), X→-100X (channel/supergroup)
        parts = relative_path.split("/", 1)
        resolved = None
        if len(parts) == 2:
            folder, rest = parts
            for alt in legacy_folder_alternates(folder):
                try:
                    resolved = (_media_root / alt / rest).resolve(strict=True)
                    logger.debug("Legacy fallback: served media via alternate folder resolution")
                    break
                except OSError, ValueError, RuntimeError:
                    continue
        if resolved is None:
            return None
    if not resolved.is_relative_to(_media_root):
        return None
    if not resolved.is_file():
        return None
    return resolved


async def _entitled_media_row(chat: ChatContext, media_key: str) -> dict:
    """Media row for an already-entitled chat + URL key, or the uniform 404.

    The row lookup IS the authorization for the bytes: the chat id comes from
    the resolved chat and rides into SQL as a predicate, so a key can only ever
    select media belonging to the chat the ref named. A malformed key and a
    missing row answer identically.

    This used to REBUILD the storage id (``{chat_id}_{message_id}_{type}``) and
    query by it, which made the chat bound a property of one f-string. It also
    found nothing for a Telegram Desktop import, whose rows are filed under
    ``import_{chat}_{msg}`` — the file was on disk and the viewer said "Media
    not found" (#423). Asking by column fixes both: the bound is explicit, and
    the row is found whatever its id spells.
    """
    parsed = _parse_media_key(media_key)
    row = None
    if parsed is not None:
        message_id, media_type = parsed
        row = await db.get_media_for_message(chat.chat_id, message_id, media_type, account_id=chat.account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return row


def _avatar_file_response(avatar_path: str):
    """Serve an avatars/ file with the containment and caching avatars always had."""
    checked = _checked_media_path(avatar_path)
    try:
        resolved = (_media_root / checked).resolve(strict=True)
    except OSError, ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_relative_to(_media_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Avatars are content-addressed (…_{photo_id}.jpg) and change rarely; keep
    # the long private TTL so avatar URLs are not refetched on every page.
    return FileResponse(resolved, headers={"Cache-Control": "private, max-age=86400"})


# Sender resolution for /media/avatar/{chat_ref}/{message_id}: one indexed
# message lookup per (chat, message), cached briefly so a page of avatars does
# not re-query per request.
_sender_lookup_cache: dict[tuple[int, int, int], tuple[float, int | None]] = {}
_SENDER_LOOKUP_CACHE_TTL_SECONDS = 300


async def _message_sender_id(chat: ChatContext, message_id: int) -> int | None:
    key = (chat.account_id, chat.chat_id, message_id)
    cached = _sender_lookup_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] <= _SENDER_LOOKUP_CACHE_TTL_SECONDS:
        return cached[1]
    sender_id = await db.get_message_sender_id(chat.chat_id, message_id, account_id=chat.account_id)
    _sender_lookup_cache[key] = (time.monotonic(), sender_id)
    return sender_id


# Route order matters only for /media/avatar/{chat_ref}: it shares the
# two-segment shape with /media/{chat_ref}/{media_key}, and registration order
# is what makes the literal win. A real ref can never collide with the literal
# — token_urlsafe(16) is always 22 characters.
@app.get("/media/thumb/{size}/{chat_ref}/{media_key}")
async def serve_thumbnail(
    size: int,
    media_key: str,
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
):
    """Serve on-demand generated thumbnails, addressed by chat ref + media key."""
    if not _media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    if user.no_download:
        raise HTTPException(status_code=403, detail="Downloads disabled for this account")

    row = await _entitled_media_row(chat, media_key)
    relative = _media_relative_path(row.get("file_path"))
    if relative is None:
        raise HTTPException(status_code=404, detail="Thumbnail not available")
    folder, _, filename = relative.rpartition("/")
    if not folder:
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    from .thumbnails import ensure_thumbnail, resolve_cache_dir

    global _thumb_cache_dir
    if _thumb_cache_dir is None:
        _thumb_cache_dir = resolve_cache_dir(_media_root)

    # ensure_thumbnail resolves the path and bounds it at the media root, so a
    # symlink cannot take the read outside it; authorization is the media row.
    result = await ensure_thumbnail(_media_root, size, folder, filename, cache_dir=_thumb_cache_dir)
    if not result:
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    thumb_path, _resolved_folder = result
    # Access-controlled bytes: private, so a shared proxy cache can never hand
    # one viewer's thumbnail to another.
    return FileResponse(thumb_path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/media/avatar/{chat_ref}/{message_id}")
async def serve_sender_avatar(message_id: int, chat: ChatContext = Depends(require_chat)):
    """Serve the avatar of the sender of one message in an entitled chat.

    Addressed by (chat ref, message id) so no user id appears in the URL — for
    a private chat the peer's user id IS the chat id. Being entitled to the
    chat is the membership proof: the sender is resolved from the message row,
    which replaces the old visible-membership probe. Available for no-download
    accounts like every avatar (UI chrome, not archive content).
    """
    if not _media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    sender_id = await _message_sender_id(chat, message_id)
    if not sender_id or sender_id <= 0:
        raise HTTPException(status_code=404, detail="File not found")
    avatar_path = _get_cached_avatar_path(sender_id, "private")
    if not avatar_path:
        raise HTTPException(status_code=404, detail="File not found")
    return _avatar_file_response(avatar_path)


@app.get("/media/avatar/{chat_ref}")
async def serve_chat_avatar(chat: ChatContext = Depends(require_chat)):
    """Serve a chat's avatar, addressed by its ref alone."""
    if not _media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    avatar_path = _get_cached_avatar_path(chat.chat_id, chat.type or "private")
    if not avatar_path:
        raise HTTPException(status_code=404, detail="File not found")
    return _avatar_file_response(avatar_path)


@app.get("/media/{chat_ref}/{media_key}")
async def serve_media(
    media_key: str,
    download: int = Query(0),
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
):
    """Serve original media bytes, addressed by chat ref + ``{message_id}_{type}`` key.

    The bytes are selected THROUGH the media row (its file_path is the storage
    location; nothing moved on disk), and the resolved path still passes the
    same traversal/symlink containment the path-addressed route enforced.
    """
    if not _media_root:
        raise HTTPException(status_code=404, detail="Media directory not configured")

    # Server-side download restriction. Original media bytes are not served to
    # no-download users because a direct GET is indistinguishable from browser
    # inline rendering once the URL is known. Avatars have their own routes.
    if user.no_download:
        raise HTTPException(status_code=403, detail="Downloads disabled for this account")

    row = await _entitled_media_row(chat, media_key)
    relative = _media_relative_path(row.get("file_path"))
    if relative is None:
        raise HTTPException(status_code=404, detail="File not found")
    resolved = _resolve_media_file(relative)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found")

    # ?download=1 forces a save instead of inline rendering. The saved name is the
    # DISPLAY name, not the storage name: on disk every file carries a uniqueness
    # prefix (``<file_id>_holiday.jpg``), and Media.file_name holds that same
    # prefixed basename, so a download otherwise landed as ``12345678_holiday.jpg``
    # while the gallery labelled it ``holiday.jpg``. media_display_filename is the
    # server-side twin of the viewer's getMediaDisplayName, so the saved name and
    # the gallery label agree. The viewer's download anchor also carries a
    # ``download="<display name>"`` attribute, but per the HTML download algorithm a
    # Content-Disposition: attachment filename takes precedence over it, so THIS
    # name is the one the browser writes.
    # The filename is attacker-influenced (it is the Telegram document name), so it
    # is percent-quoted and falls back to RFC 5987 whenever quoting changes the
    # string — every header-dangerous byte (CR, LF, ", ;, space) is escaped by that
    # quote(), so only unreserved characters survive verbatim. This mirrors
    # Starlette's own FileResponse header construction; it is written out here
    # rather than passed as filename= so the FileResponse call keeps the plain
    # FileResponse(resolved) shape. Passing a user-derived filename= makes CodeQL
    # model the call as a filesystem sink and raise py/path-injection on
    # `resolved`, which is a false positive: containment is already enforced above
    # (reject ../absolute, resolve(strict=True), is_relative_to(_media_root)).
    # Default (no download param) stays inline for the types the viewer renders
    # inline; everything else is handed over as a download (see _inline_media_type).
    inline_type = _inline_media_type(resolved.name)
    response = FileResponse(resolved, media_type=inline_type or "application/octet-stream")
    if download or inline_type is None:
        download_name = media_display_filename(row.get("file_name") or resolved.name)
        quoted = quote(download_name)
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=utf-8''{quoted}"
            if quoted != download_name
            else f'attachment; filename="{download_name}"'
        )
    # Every byte this route serves is access-controlled, so no shared cache may
    # store it — never a proxy that skips the entitlement.
    response.headers["Cache-Control"] = "private"
    return response


@app.get("/api/search/messages")
async def search_messages(
    q: str = Query(..., min_length=1, max_length=500),
    user: UserContext = Depends(require_auth),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=5000),
):
    """Message text across every chat the caller may see — the sidebar's Messages section.

    Word-prefix matching over the full-text index, newest first. Restricted
    viewers are filtered in SQL through the same ChatScope as the chat list
    and the tag view, so this route can never widen what a viewer sees. Each
    row names its chat the way the chat list does (title, or first/last name
    for a private chat) and addresses the jump by ref, never by id.
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")
    try:
        payload = await db.search_messages_global(q, scope=_chat_scope(user), limit=limit, offset=offset)
    except Exception as e:
        # Type name only: SQLAlchemy exception text can echo statement
        # parameters — the search text and the viewer's scope grants.
        logger.error(f"Error searching messages: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")

    results = []
    for row in payload["results"]:
        results.append(
            {
                "id": row["id"],
                "date": row["date"],
                "text": row["text"],
                "sender_name": row["sender_name"],
                "is_deleted": row["is_deleted"],
                "topic_title": row["topic_title"],
                "chat": {
                    "ref": row["chat_ref"],
                    "title": row["chat_title"],
                    "first_name": row["chat_first_name"],
                    "last_name": row["chat_last_name"],
                    "username": row["chat_username"],
                    "type": row["chat_type"],
                    "is_forum": row["chat_is_forum"],
                    "avatar_url": _chat_avatar_url(row["chat_id"], row["chat_type"], row["chat_ref"]),
                },
            }
        )
    return {
        "query": q,
        "limit": limit,
        "offset": offset,
        "has_more": payload["has_more"],
        "indexed": payload["indexed"],
        "results": results,
    }


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page.

    The default theme is baked in at serve time (a placeholder inside the
    boot script) because it must be known before first paint - fetching it
    from an API would flash the built-in palette first.
    """
    html = (templates_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace("__VIEWER_DEFAULT_THEME__", VIEWER_DEFAULT_THEME)
    html = html.replace("__VIEWER_CHAT_BACKGROUND__", _chat_background_css(VIEWER_CHAT_BACKGROUND))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _redacted_error_response(route_template: str, exc: Exception) -> JSONResponse:
    """Log an unhandled exception under the PII-redaction rule and build its response.

    Shared by RedactingErrorMiddleware (the normal path) and the FastAPI
    exception handler below (a defence-in-depth fallback) so BOTH emit the exact
    same redacted log line and the exact same 500/503 JSON body — the 503 split
    for DB-connection errors, 500 for everything else.

    Never the concrete path: a media URL is /media/<chat_id>/<file_id>_<the
    sender's document name>, so logging it logs a chat id and a person's file
    name. The route template is what an operator needs to find the endpoint,
    and it carries no identifiers. describe_exception keeps the same rule for
    the exception text (OSError stringifies with the offending path).
    """
    if _is_db_connection_error(exc):
        logger.error(f"Database connection error on {route_template}: {describe_exception(exc)}")
        return JSONResponse(status_code=503, content={"detail": "Database temporarily unavailable"})
    # exc_info is banned on this branch too: the log formatter ends a traceback
    # with the exception's own str() (and its __cause__/__context__ chain), and
    # non-OSError exceptions carry paths there — subprocess.TimeoutExpired /
    # CalledProcessError stringify with the full ffmpeg argv, which contains a
    # media path (a chat id and the sender's file name). describe_exception
    # already refuses those messages, so log only the frame list (file, line,
    # function, source text — never a runtime value): the crash site stays
    # diagnosable while nothing an exception smuggled in can reach the log.
    frames = "".join(traceback.format_tb(exc.__traceback__)).rstrip()
    detail = f"Unhandled error on {route_template}: {describe_exception(exc)}"
    logger.error(f"{detail}\n{frames}" if frames else detail)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


class RedactingErrorMiddleware:
    """Handle unhandled exceptions at the ASGI layer, one level inside ServerErrorMiddleware.

    The exception handler below already logs a redacted line, but that alone is
    not enough. Starlette's ServerErrorMiddleware re-raises the exception after
    the handler returns (errors.py ends its except block with ``raise exc``), and
    uvicorn's ``run_asgi`` then logs "Exception in ASGI application" with exc_info
    UNCONDITIONALLY. That traceback ends with the exception's own str() — and a
    thumbnail failure raises subprocess.TimeoutExpired whose argv is the ffmpeg
    command, i.e. a media path carrying the chat id and the sender's file name.
    So the redaction has to happen where the exception can be STOPPED, not merely
    logged.

    Registered via app.add_middleware after every other middleware, this becomes
    the outermost user middleware: it wraps the whole app yet still sits inside
    ServerErrorMiddleware (Starlette forces that one outermost). It catches the
    unhandled exception first, logs the identical redacted line, sends the
    identical 500/503 JSON response, and does NOT re-raise. ServerErrorMiddleware
    and uvicorn therefore never see the exception, so no traceback can leak.

    A failure after the response has started (a streaming FileResponse that dies
    mid-body) cannot be answered with a clean JSON body, so it is re-raised and
    behaves exactly as before; the assigned exploit raises before the response
    starts and is fully handled here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if response_started:
                raise
            route_template = getattr(scope.get("route"), "path", None) or "unrouted request"
            response = _redacted_error_response(route_template, exc)
            await response(scope, receive, send)


# Added last, so it is the OUTERMOST user middleware: it wraps CORS and the
# security-headers middleware and sits just inside ServerErrorMiddleware, which
# lets it catch and answer an unhandled exception before ServerErrorMiddleware
# can re-raise it into uvicorn (where the traceback would leak the media path).
app.add_middleware(RedactingErrorMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Fallback 500/503 handler for any path that bypasses the middleware.

    RedactingErrorMiddleware is what catches in the normal path (so uvicorn never
    logs the traceback); this handler stays registered so the same redacted-log +
    503-for-DB / 500-for-everything-else contract still holds for any code that
    reaches ServerErrorMiddleware directly.
    """
    route_template = getattr(request.scope.get("route"), "path", None) or "unrouted request"
    return _redacted_error_response(route_template, exc)


@app.get("/api/health")
async def health_check():
    """Health check endpoint for monitoring and Docker healthchecks."""
    result = {"status": "ok"}
    if db:
        try:
            await db.get_chat_count()
            result["database"] = "connected"
        except Exception:
            result["database"] = "unreachable"
            result["status"] = "degraded"
            return JSONResponse(status_code=503, content=result)
    return result


@app.get("/api/auth/check")
async def check_auth(request: Request, auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Check current authentication status. Returns role and username if authenticated."""
    # Trusted proxy header — if header present, user is authenticated by the proxy
    if _PROXY_AUTH_ENABLED:
        proxy_user = request.headers.get(AUTH_PROXY_HEADER, "").strip()
        if proxy_user:
            try:
                user_ctx = await _resolve_proxy_user(proxy_user)
                return {
                    "authenticated": True,
                    "auth_required": True,
                    "role": user_ctx.role,
                    "username": user_ctx.username,
                    "no_download": user_ctx.no_download,
                    "proxy_auth": True,
                }
            except HTTPException:
                return {"authenticated": False, "auth_required": True}

    if not AUTH_ENABLED and not _PROXY_AUTH_ENABLED:
        if ALLOW_ANONYMOUS_VIEWER:
            return {
                "authenticated": True,
                "auth_required": False,
                "role": "viewer",
                "username": "anonymous",
                "no_download": False,
            }
        return {"authenticated": False, "auth_required": True, "setup_required": True}

    if not auth_cookie:
        return {"authenticated": False, "auth_required": True}

    session = await _resolve_session(auth_cookie)
    if not session:
        return {"authenticated": False, "auth_required": True}
    if time.time() - session.created_at > AUTH_SESSION_SECONDS:
        # Same rule as _resolve_user_context: whoever expires the session owns
        # closing the sockets it admitted.
        _sessions.pop(auth_cookie, None)
        await ws_manager.close_for(session_keys=(auth_cookie,))
        return {"authenticated": False, "auth_required": True}

    return {
        "authenticated": True,
        "auth_required": True,
        "role": session.role,
        "username": session.username,
        "no_download": session.no_download,
    }


@app.post("/api/login")
async def login(request: Request):
    """Authenticate user (master via env vars or viewer via DB accounts)."""
    if not AUTH_ENABLED:
        if ALLOW_ANONYMOUS_VIEWER:
            return JSONResponse({"success": True, "message": "Auth disabled by explicit opt-in"})
        raise HTTPException(status_code=503, detail="Viewer authentication is not configured")

    client_ip = _get_client_ip(request)

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
    _db_reachable = True
    if db:
        try:
            viewer = await db.get_viewer_by_username(username)
        except Exception as e:
            logger.warning(f"Database unavailable during login, falling back to env credentials: {e}")
            _db_reachable = False
            viewer = None

        if viewer and viewer["is_active"]:
            # 600k-round PBKDF2: off the event loop, or one login attempt stalls
            # every other request, WebSocket frame and health check on the way.
            if await asyncio.to_thread(_verify_password, password, viewer["salt"], viewer["password_hash"]):
                # v8.0.0 grant columns only; an unreadable grant logs the viewer
                # in with the EMPTY grant (sees nothing) — fail-closed without
                # turning a data problem into a login oracle.
                allowed_accounts, allowed_chat_refs = _grants_from_row(viewer)

                viewer_no_download = bool(viewer.get("no_download", 0))
                token = await _create_session(
                    username,
                    "viewer",
                    allowed_accounts=allowed_accounts,
                    allowed_chat_refs=allowed_chat_refs,
                    no_download=viewer_no_download,
                )
                response = JSONResponse({"success": True, "role": "viewer", "username": username})
                response.set_cookie(
                    key=AUTH_COOKIE_NAME,
                    value=token,
                    httponly=True,
                    secure=_get_secure_cookies(request),
                    samesite="lax",
                    max_age=AUTH_SESSION_SECONDS,
                )

                try:
                    await db.create_audit_log(
                        username=username,
                        role="viewer",
                        action="login_success",
                        endpoint="/api/login",
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
                except Exception:
                    logger.warning(f"Failed to write audit log for viewer login: {username}")
                return response

    # 2. Fall back to master env var credentials
    viewer_only = request.headers.get("x-viewer-only", "").lower() == "true"
    if secrets.compare_digest(username, VIEWER_USERNAME) and secrets.compare_digest(password, VIEWER_PASSWORD):
        if viewer_only:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = await _create_session(username, "master")
        response = JSONResponse({"success": True, "role": "master", "username": username})
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=_get_secure_cookies(request),
            samesite="lax",
            max_age=AUTH_SESSION_SECONDS,
        )

        try:
            if db:
                await db.create_audit_log(
                    username=username,
                    role="master",
                    action="login_success",
                    endpoint="/api/login",
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
        except Exception:
            logger.warning(f"Failed to write audit log for master login: {username}")
        return response

    # Failed login — if DB was unreachable, viewer accounts couldn't be checked
    if not _db_reachable:
        raise HTTPException(status_code=503, detail="Database temporarily unavailable, please try again later")

    try:
        if db:
            await db.create_audit_log(
                username=username or "(empty)",
                role="unknown",
                action="login_failed",
                endpoint="/api/login",
                ip_address=client_ip,
                user_agent=user_agent,
            )
    except Exception:
        logger.warning(f"Failed to write audit log for failed login: {username}")
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/logout")
async def logout(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
):
    """Invalidate current session and clear cookie.

    Logout revokes the BROWSER's channels, not the account's: this session's
    sockets are closed and this user's push subscriptions are deleted, while
    the user's other sessions stay live.
    """
    if auth_cookie:
        session = _sessions.pop(auth_cookie, None)
        await ws_manager.close_for(session_keys=(auth_cookie,))
        if db:
            # Always attempt DB delete (session may exist in DB but not in memory cache)
            try:
                if not session:
                    row = await db.get_session(auth_cookie)
                    if row:
                        session = SessionData(username=row["username"], role=row["role"])
                await db.delete_session(auth_cookie)
            except Exception:
                pass
            if session:
                await db.create_audit_log(
                    username=session.username,
                    role=session.role,
                    action="logout",
                    endpoint="/api/logout",
                    ip_address=request.client.host if request.client else None,
                )
        if session:
            await _purge_push_subscriptions(session.username)

    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


# ============================================================================
# Share Token Authentication (v7.2.0)
# ============================================================================


@app.post("/auth/token")
async def auth_via_token(request: Request):
    """Authenticate using a share token. Creates a session scoped to the token's allowed chats."""
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    client_ip = _get_client_ip(request)

    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    try:
        data = await request.json()
        plaintext_token = data.get("token", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    if not plaintext_token:
        raise HTTPException(status_code=400, detail="Token required")

    _record_login_attempt(client_ip)

    token_record = await db.verify_viewer_token(plaintext_token)
    if not token_record:
        await db.create_audit_log(
            username="(token)",
            role="token",
            action="token_auth_failed",
            endpoint="/auth/token",
            ip_address=client_ip,
        )
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # v8.0.0 grant columns only; an unreadable grant authenticates into the
    # EMPTY grant (sees nothing). The legacy allowed_chat_ids is never read.
    allowed_accounts, allowed_chat_refs = _grants_from_row(token_record)

    token_no_download = bool(token_record.get("no_download", 0))
    token_label = token_record.get("label") or f"token:{token_record['id']}"
    session_token = await _create_session(
        username=f"token:{token_label}",
        role="token",
        allowed_accounts=allowed_accounts,
        allowed_chat_refs=allowed_chat_refs,
        no_download=token_no_download,
        source_token_id=token_record["id"],
    )

    response = JSONResponse(
        {
            "success": True,
            "role": "token",
            "username": f"token:{token_label}",
            "no_download": token_no_download,
        }
    )
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=_get_secure_cookies(request),
        samesite="lax",
        max_age=AUTH_SESSION_SECONDS,
    )

    await db.create_audit_log(
        username=f"token:{token_label}",
        role="token",
        action="token_auth_success",
        endpoint="/auth/token",
        ip_address=client_ip,
    )

    return response


# One directory read per avatars/ subfolder, reused until that folder changes:
# {directory: (directory mtime, {id: [filenames]})}. A glob per id scandirs the
# whole folder every time, so a page of 50 senders cost 50 full directory scans
# on the event loop; a lookup now costs one stat of the folder.
_avatar_dir_index: dict[str, tuple[int, dict[int, list[str]]]] = {}


def _avatar_dir_listing(avatar_dir: str, force: bool = False) -> dict[int, list[str]]:
    """Map {id: [avatar filenames]} for one avatars/ subfolder.

    Cached against the directory's own mtime, which changes whenever a file is
    added or removed there — so a newly downloaded avatar is picked up on the
    next request without a timer, and nothing can serve a listing for a folder
    that has since changed.
    """
    try:
        stamp = os.stat(avatar_dir).st_mtime_ns
    except OSError:
        _avatar_dir_index.pop(avatar_dir, None)
        return {}

    cached = _avatar_dir_index.get(avatar_dir)
    if cached is not None and cached[0] == stamp and not force:
        return cached[1]

    listing: dict[int, list[str]] = {}
    try:
        with os.scandir(avatar_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".jpg"):
                    continue
                # "{id}_{photo_id}.jpg" and the legacy "{id}.jpg" both key on id.
                try:
                    avatar_id = int(entry.name[:-4].split("_", 1)[0])
                except ValueError:
                    continue
                listing.setdefault(avatar_id, []).append(entry.name)
    except OSError:
        return {}

    _avatar_dir_index[avatar_dir] = (stamp, listing)
    return listing


def _newest_avatar_file(avatar_dir: str, names: list[str] | None) -> str | None:
    """Most recently modified of the candidate files that are still on disk."""
    newest_name: str | None = None
    newest_mtime: float | None = None
    for name in names or ():
        try:
            mtime = os.path.getmtime(os.path.join(avatar_dir, name))
        except OSError:
            continue  # deleted since the folder was read
        if newest_mtime is None or mtime > newest_mtime:
            newest_name, newest_mtime = name, mtime
    return newest_name


def _find_avatar_path(chat_id: int, chat_type: str) -> str | None:
    """Find avatar file path for a chat.

    Avatar files are stored as: {chat_id}_{photo_id}.jpg
    For groups/channels, chat_id is negative (marked ID format).
    """
    # Determine folder: 'chats' for groups/channels, 'users' for private
    avatar_folder = "users" if chat_type == "private" else "chats"
    avatar_dir = os.path.join(config.media_path, "avatars", avatar_folder)

    candidates = _avatar_dir_listing(avatar_dir).get(chat_id)
    avatar_file = _newest_avatar_file(avatar_dir, candidates)
    if avatar_file is None and candidates:
        # Every candidate has vanished — re-read the folder once and retry, so a
        # deletion is never served from a listing that outlived it.
        avatar_file = _newest_avatar_file(avatar_dir, _avatar_dir_listing(avatar_dir, force=True).get(chat_id))

    if avatar_file:
        return f"avatars/{avatar_folder}/{avatar_file}"

    return None


# Cache avatar paths to avoid repeated filesystem lookups
_avatar_cache: dict[int, str | None] = {}
_avatar_cache_time: datetime | None = None
AVATAR_CACHE_TTL_SECONDS = 300  # 5 minutes


def _encode_media_key(media_key: str) -> str:
    """Percent-encode a URL media key. The key is ``{message_id}_{type}`` — both
    server-minted — but the single rule keeps every /media/ URL builder safe
    even if a type ever grows a reserved character."""
    return quote(media_key, safe="")


def _chat_avatar_url(chat_id: int | None, chat_type: str | None, ref: str | None) -> str | None:
    """Ref-addressed avatar URL when an avatar is cached, else None.

    The avatar bytes route re-resolves at serve time; this only decides whether
    the viewer renders an <img> at all. Any lookup failure reads as "no avatar"
    rather than failing the row it decorates.
    """
    if chat_id is None or not ref:
        return None
    try:
        return f"/media/avatar/{ref}" if _get_cached_avatar_path(chat_id, chat_type or "private") else None
    except Exception as e:
        # Type name only: an OSError's text carries the chat-derived path.
        logger.error(f"Error finding avatar for a chat: {type(e).__name__}")
        return None


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


def _attach_message_payload_urls(messages: list, chat: ChatContext) -> None:
    """Give message payloads their ref-addressed URLs; no chat id survives in any URL.

    - ``media.id`` is rewritten from the storage key to the chat-free URL key
      ``{message_id}_{type}`` (the ref in the route scopes it), and ``media.url``
      is added — the viewer must build no /media/ URL from file_path anymore.
    - ``sender_avatar_url`` becomes ``/media/avatar/{ref}/{message_id}``, present
      only when the sender's avatar file is already on disk. A member avatar
      exists only when a prior backup happened to download it (proactive
      download stays deferred — flood-sensitive); the initials circle is the
      always-available render, a served file is a bonus.
    """
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender_id = message.get("sender_id")
        message["sender_avatar_url"] = (
            f"/media/avatar/{chat.ref}/{message.get('id')}"
            if sender_id and sender_id > 0 and _get_cached_avatar_path(sender_id, "private")
            else None
        )
        media = message.get("media")
        if not isinstance(media, dict):
            continue
        # Media.type is nullable, so a key is not always constructible. Blank the
        # id rather than leaving the storage key: passing it through is what put
        # the chat id in front of the browser (and back in a cursor query string),
        # which the promise at the top of this docstring says never happens.
        media_key = _url_media_key(message.get("id"), media.get("type"))
        media["id"] = media_key
        if media_key and _media_relative_path(media.get("file_path")):
            media["url"] = f"/media/{chat.ref}/{_encode_media_key(media_key)}"
        else:
            media["url"] = None


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
        # ONE path for every principal. The entitlement rides into SQL as WHERE
        # predicates, so limit/offset/COUNT all describe the same visible row
        # set and a restricted viewer touches only the rows it may see.
        #
        # This used to fork: the restricted branch called get_all_chats() with
        # NO limit, filtered in Python, then sliced. Every chat row in the
        # archive was materialised — each carrying the correlated MAX(date)
        # subquery — to render one page, so /api/chats went from slow to
        # unusable as the archive grew (4,784 chats / ~2.7M messages: >120s for
        # a viewer entitled to a single chat).
        scope = _chat_scope(user)
        chats = await db.get_all_chats(
            limit=limit, offset=offset, search=search, archived=archived, folder_id=folder_id, scope=scope
        )
        total = await db.get_chat_count(search=search, archived=archived, folder_id=folder_id, scope=scope)

        # Ref-addressed avatar URLs; the avatar bytes route re-resolves at serve
        # time, this only decides whether the viewer renders an <img> at all.
        for chat in chats:
            chat["avatar_url"] = _chat_avatar_url(chat.get("id"), chat.get("type"), chat.get("ref"))

        return {
            "chats": chats,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(chats) < total,
        }
    except Exception as e:
        logger.error(f"Error fetching chats: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}")
async def get_chat(chat: ChatContext = Depends(require_chat)):
    """One chat by its opaque ref, shaped like a chat-list row.

    Deep links (push notifications, shared message links, global search hits)
    can point at any entitled chat, not just the page the sidebar has loaded;
    this is how the viewer resolves the rest without paging the whole list.
    """
    try:
        row = await db.get_chat_by_ref(chat.ref, account_id=chat.account_id)
    except Exception as e:
        logger.error(f"Error fetching chat: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")
    if not row:
        raise HTTPException(status_code=404, detail="Chat not found")
    row["avatar_url"] = _chat_avatar_url(row["id"], row.get("type"), row["ref"])
    return row


@app.get("/api/chats/{chat_ref}/messages")
async def get_messages(
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    before_date: str | None = None,
    before_id: int | None = None,
    after_id: int | None = None,
    topic_id: int | None = None,
):
    """
    Get messages for a specific chat with user and media info.

    Supports two pagination modes:
    - Offset-based: ?offset=100 (slower for large offsets)
    - Cursor-based: ?before_date=2026-01-15T12:00:00&before_id=12345 (O(1) performance)
      A lone ?before_id=N returns rows with id < N (jump-to-message window),
      and ?after_id=N returns rows with id > N (jump-to-message after-context).

    v6.2.0: Added topic_id filter for forum topic messages.

    Cursor-based pagination is preferred for infinite scroll.
    """
    # Parse before_date if provided
    parsed_before_date = None
    if before_date:
        try:
            parsed_before_date = datetime.fromisoformat(before_date.replace("Z", "+00:00"))
            # Message.date is naive UTC — convert the instant, never just relabel it
            if parsed_before_date.tzinfo:
                parsed_before_date = parsed_before_date.astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid before_date format. Use ISO 8601.")

    try:
        messages = await db.get_messages_paginated(
            chat_id=chat.chat_id,
            limit=limit,
            offset=offset,
            search=search,
            before_date=parsed_before_date,
            before_id=before_id,
            after_id=after_id,
            topic_id=topic_id,
            account_id=chat.account_id,
        )
        # get_messages_paginated returns a list of message dicts; guard so an
        # unexpected shape can never turn a read into a 500.
        if isinstance(messages, list):
            _attach_message_payload_urls(messages, chat)
        if user.no_download:
            _strip_original_media_paths(messages)
        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


# A tag is a #hashtag (word chars, at least one non-digit — Telegram excludes
# pure numbers) or a $CASHTAG (1-8 latin letters, the official shape).
_TAG_PATTERN = re.compile(r"^#(?!\d+$)\w{1,64}$|^\$[A-Z]{1,8}$")


@app.get("/api/tags/{tag}")
async def search_tag(
    tag: str,
    user: UserContext = Depends(require_auth),
    scope: str = Query("all", pattern="^(chat|mine|all)$"),
    chat_ref: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Messages using a #hashtag or $cashtag, newest first — the tag view.

    Mirrors the official clients' tag tabs mapped onto an archive:
    ``scope=chat`` is This Chat (requires ``chat_ref``, entitlement-enforced by
    the same resolver as every chat route), ``scope=mine`` is My Messages (the
    archive's outgoing side), ``scope=all`` is the whole entitled archive.
    Restricted viewers are filtered in SQL via the same ChatScope as the chat
    list, so this route can never widen what a viewer sees.
    """
    if not _TAG_PATTERN.match(tag):
        raise HTTPException(status_code=400, detail="Not a recognizable #hashtag or $cashtag")
    kwargs: dict[str, Any] = {"scope": _chat_scope(user), "limit": limit, "offset": offset}
    if scope == "chat":
        if not chat_ref:
            raise HTTPException(status_code=400, detail="scope=chat requires chat_ref")
        chat = await _resolve_chat_ref(chat_ref, user)
        kwargs.update(chat_id=chat.chat_id, account_id=chat.account_id)
    elif scope == "mine":
        kwargs["outgoing_only"] = True
    try:
        payload = await db.search_messages_by_tag(tag, **kwargs)
    except Exception as e:
        # Type name only: SQLAlchemy exception text can echo statement
        # parameters — the tag and the viewer's scope ids.
        logger.error(f"Error searching tag: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")
    payload["tag"] = tag
    return payload


def _parse_changes_bound(value: str, param: str) -> datetime:
    """ISO bound to naive UTC — the #365 conversion contract (convert, never relabel)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {param} date. Use ISO 8601.") from None
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@app.get("/api/changes")
async def get_recent_changes(
    user: UserContext = Depends(require_auth),
    since: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """What changed: deletions and edits the archive captured, newest first.

    ``since`` bounds the window's start (inclusive); ``before`` is the keyset
    cursor — pass the last row's ``date`` to page older. Entitlements are the
    chat list's own compiled scope, so a restricted viewer sees only their
    chats' changes.
    """
    parsed_since = _parse_changes_bound(since, "since") if since else None
    parsed_before = _parse_changes_bound(before, "before") if before else None
    try:
        changes = await db.get_recent_changes(
            since=parsed_since, before=parsed_before, limit=limit, scope=_chat_scope(user)
        )
        next_cursor = changes[-1]["date"] if len(changes) == limit else None
        return JSONResponse(
            {"changes": changes, "next_before": next_cursor},
            headers={"Cache-Control": "private, no-store"},
        )
    except HTTPException:
        raise
    except Exception as e:
        # Counts/types only — feed rows carry chat titles and message text.
        logger.error(f"Error building the changes feed: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/messages/{message_id}/versions")
async def get_message_versions(
    message_id: int,
    chat: ChatContext = Depends(require_chat),
    limit: int = Query(100, ge=1, le=500),
):
    """Get preserved previous versions for a message."""
    try:
        return await db.get_message_versions(
            chat_id=chat.chat_id, message_id=message_id, limit=limit, account_id=chat.account_id
        )
    except Exception as e:
        logger.error(f"Error fetching message versions: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/pinned")
async def get_pinned_messages(chat: ChatContext = Depends(require_chat), user: UserContext = Depends(require_auth)):
    """Get all pinned messages for a chat, ordered by date descending (newest first)."""
    try:
        pinned_messages = await db.get_pinned_messages(chat.chat_id, account_id=chat.account_id)
        # Same renderer as the message list, so the same ref-addressed URLs.
        if isinstance(pinned_messages, list):
            _attach_message_payload_urls(pinned_messages, chat)
        if user.no_download:
            _strip_original_media_paths(pinned_messages)
        return pinned_messages  # Returns empty list if no pinned messages
    except Exception as e:
        logger.error(f"Error fetching pinned messages: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/media")
async def get_chat_media(
    types: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: str = Query(default=""),
    after_id: str = Query(default=""),
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
):
    """Get paginated media items for a chat, with optional type filtering.

    ``before_id`` pages BACKWARD (older) and ``after_id`` pages FORWARD (newer);
    both are the CHAT-FREE ``{message_id}_{type}`` key this endpoint returns as
    each item's ``id`` — the chat id no longer rides in the query string. They
    are mutually exclusive — asking for a page both before X and after Y has no
    meaning here, so it is rejected instead of silently honouring one. An
    unresolvable or foreign token yields an empty page in either direction (#266).
    """
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    if before_id and after_id:
        raise HTTPException(status_code=400, detail="before_id and after_id are mutually exclusive")

    media_types = [t.strip() for t in types.split(",") if t.strip()] or None

    # The URL token is the chat-free ``{message_id}_{type}`` key; the adapter
    # resolves it against the row's own columns. It used to be turned back into
    # a storage id by prepending the chat, which no imported row carries, so
    # paging past the first imported item resolved no cursor and the gallery
    # dead-ended (#423).
    #
    # A token that resolves to no row yields an empty page, and so must one that
    # does not even parse. Handing the adapter None for a cursor the caller DID
    # supply would mean "no cursor at all" and serve the first page again,
    # restarting pagination instead of ending it (#266 semantics).
    before_key = _parse_media_key(before_id) if before_id else None
    after_key = _parse_media_key(after_id) if after_id else None
    if (before_id and before_key is None) or (after_id and after_key is None):
        return {"items": [], "has_more": False}

    try:
        result = await db.get_media_paginated(
            chat.chat_id,
            media_types=media_types,
            limit=limit,
            before_key=before_key,
            after_key=after_key,
            account_id=chat.account_id,
        )
        for item in result["items"]:
            media_key = _url_media_key(item.get("message_id"), item.get("type"))
            item["id"] = media_key

            relative = _media_relative_path(item.get("file_path", "") or "")
            if relative is None or media_key is None:
                item["thumb_url"] = None
                item.pop("file_path", None)
                continue

            filename = relative.rsplit("/", 1)[-1]
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext in THUMBNAIL_EXTENSIONS:
                item["thumb_url"] = f"/media/thumb/200/{chat.ref}/{_encode_media_key(media_key)}"
            else:
                item["thumb_url"] = None

            if user.no_download:
                item.pop("file_path", None)
                # serve_thumbnail refuses derived bytes for these accounts, so a
                # thumb_url here would only render as a broken image; dropping it
                # lights up the gallery's own placeholder instead.
                item["thumb_url"] = None
            else:
                item["media_url"] = f"/media/{chat.ref}/{_encode_media_key(media_key)}"

        return result
    except Exception as e:
        logger.error(f"Error fetching chat media: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/media/counts")
async def get_chat_media_counts(chat: ChatContext = Depends(require_chat)):
    """Get media type counts for a chat."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        counts = await db.get_media_counts(chat.chat_id, account_id=chat.account_id)
        return counts
    except Exception as e:
        logger.error(f"Error fetching media counts: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/folders")
async def get_folders(user: UserContext = Depends(require_auth)):
    """Get all chat folders with their chat counts.

    v6.2.0: Returns user-created Telegram folders (dialog filters).
    """
    try:
        visible_chat_ids = await _visible_chat_id_set(user)
        folders = await db.get_all_folders(allowed_chat_ids=visible_chat_ids)
        return {"folders": folders}
    except Exception as e:
        logger.error(f"Error fetching folders: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/topics")
async def get_chat_topics(chat: ChatContext = Depends(require_chat)):
    """Get forum topics for a chat.

    v6.2.0: Returns topic list with message counts for forum-enabled chats.
    """
    try:
        topics = await db.get_forum_topics(chat.chat_id, account_id=chat.account_id)
        return {"topics": topics}
    except Exception as e:
        logger.error(f"Error fetching topics: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/archived/count")
async def get_archived_count(user: UserContext = Depends(require_auth)):
    """Get the number of archived chats.

    v6.2.0: Used by the viewer to display the archived section badge.
    Respects DISPLAY_CHAT_IDS so restricted viewers only see relevant archived chats.
    """
    try:
        scope = _chat_scope(user)
        if scope.unrestricted:
            count = await db.get_archived_chat_count()
        else:
            # Counted in SQL under the same scope the chat list uses, rather
            # than by loading every archived chat and filtering in Python.
            count = await db.get_chat_count(archived=True, scope=scope)
        return {"count": count}
    except Exception as e:
        logger.error(f"Error fetching archived count: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/stats")
async def get_stats(user: UserContext = Depends(require_auth)):
    """Get cached backup statistics (fast, calculated daily)."""
    try:
        stats = await db.get_cached_statistics()

        # Filter per-chat stats to only chats the user can access
        user_chat_ids = await _visible_chat_id_set(user)
        per_chat = stats.get("per_chat_message_counts", {})
        if not isinstance(per_chat, dict):
            # A malformed cached blob (null, a list) must scope to zeros like
            # an absent map — not crash the restricted request with a 500.
            per_chat = {}
        if user_chat_ids is not None:
            # ACL-driven, never data-driven: an absent or empty per-chat map
            # (startup calculation failed, or a pre-8.0 cached blob) must scope
            # a restricted viewer to zeros, not fail open to the archive-wide
            # numbers this block exists to hide.
            # JSON keys are strings after json.loads(), user_chat_ids are ints
            stats["per_chat_message_counts"] = {k: v for k, v in per_chat.items() if int(k) in user_chat_ids}
            # Recompute aggregates from visible chats only
            visible = stats["per_chat_message_counts"]
            stats["chats"] = len(visible)
            stats["messages"] = sum(visible.values())
            # Remove global media/size stats — no per-chat breakdown available
            stats.pop("media_files", None)
            stats.pop("total_size_mb", None)

        stats["timezone"] = config.viewer_timezone
        stats["stats_calculation_hour"] = config.stats_calculation_hour
        stats["show_stats"] = config.show_stats  # Whether to show stats UI

        # Check if real-time listener is active (written by backup container)
        # Per-account keys since 8.1 (#313): active = any account's listener is
        # up; "since" = the earliest active one. Account 1 uses the legacy key.
        active_times = []
        try:
            account_ids = list(await db.get_account_ids())
        except Exception:
            # Advisory UI status only — degrade to the legacy single-account key.
            account_ids = [DEFAULT_ACCOUNT_ID]
        for account_id in account_ids:
            value = await db.get_metadata(account_metadata_key("listener_active_since", account_id))
            if value:
                active_times.append(value)
        stats["listener_active"] = bool(active_times)
        stats["listener_active_since"] = min(active_times) if active_times else None

        # Check if a backup is currently in progress (written by backup engine)
        backup_in_progress = await db.get_metadata("backup_in_progress")
        stats["backup_in_progress"] = backup_in_progress == "1"

        # Notifications config
        stats["push_notifications"] = config.push_notifications  # off, basic, full
        stats["push_enabled"] = push_manager is not None and push_manager.is_enabled

        # Notifications enabled if ENABLE_NOTIFICATIONS=true OR PUSH_NOTIFICATIONS is basic/full
        stats["enable_notifications"] = config.enable_notifications or config.push_notifications in ("basic", "full")

        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/status")
async def get_operator_status(user: UserContext = Depends(require_master)):
    """One page's worth of "is my archive healthy right now" (master-only).

    Aggregates signals the system already writes: last backup run and
    in-progress flag, per-account listener liveness, the media pipeline's
    pending/exhausted split, stats freshness and database size. Counts and
    timestamps only — never ids, titles or content.
    """
    try:
        payload: dict = {
            "backup": {
                "last_run": await db.get_metadata("last_backup_time"),
                "in_progress": (await db.get_metadata("backup_in_progress")) == "1",
            },
            "stats_calculated_at": await db.get_metadata("stats_calculated_at"),
        }
        try:
            account_ids = list(await db.get_account_ids())
        except Exception:
            account_ids = [DEFAULT_ACCOUNT_ID]
        listeners = []
        for account_id in account_ids:
            since = await db.get_metadata(account_metadata_key("listener_active_since", account_id))
            listeners.append({"account_id": account_id, "active": bool(since), "active_since": since})
        payload["listeners"] = listeners
        payload["media"] = await db.get_operator_status_counts(max_attempts=config.max_media_download_attempts)
        payload["database"] = {
            "backend": "sqlite" if db.db_manager._is_sqlite else "postgresql",
            "size_bytes": await db.get_database_size_bytes(),
        }
        return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})
    except Exception as e:
        logger.error(f"Error building operator status: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/stats/refresh")
async def refresh_stats(user: UserContext = Depends(require_master)):
    """Manually trigger stats recalculation (expensive, use sparingly)."""
    try:
        stats = await db.calculate_and_store_statistics(storage_path=config.backup_path)
        stats["timezone"] = config.viewer_timezone
        return stats
    except Exception as e:
        logger.error(f"Error calculating stats: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
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
    - chat_ref: Optional chat ref for chat-specific subscriptions
    """
    if not push_manager or not push_manager.is_enabled:
        raise HTTPException(status_code=400, detail="Push notifications not enabled. Set PUSH_NOTIFICATIONS=full")

    try:
        data = await request.json()

        endpoint = data.get("endpoint")
        keys = data.get("keys", {})
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        chat_ref = data.get("chat_ref")

        if not endpoint or not p256dh or not auth:
            raise HTTPException(status_code=400, detail="Missing required subscription data")

        # The 7.x field is refused rather than ignored: silently dropping a
        # scoping request would store a GLOBAL subscription — a widening.
        if data.get("chat_id") is not None:
            raise HTTPException(status_code=400, detail="chat_id is no longer accepted; send chat_ref")

        from .push import validate_push_endpoint

        # validate_push_endpoint resolves the hostname (blocking DNS lookup), so it
        # runs off the event loop. This endpoint is reachable without auth when
        # ALLOW_ANONYMOUS_VIEWER is set, so bound the resolution: a hostile host that
        # stalls DNS must fail fast (reject) rather than tie up an executor worker.
        try:
            endpoint_ok = await asyncio.wait_for(asyncio.to_thread(validate_push_endpoint, endpoint), timeout=5.0)
        except TimeoutError:
            endpoint_ok = False
        if not endpoint_ok:
            logger.warning("Rejected push subscription with invalid endpoint")
            raise HTTPException(status_code=400, detail="Invalid subscription endpoint")

        # A chat-scoped subscription resolves + entitles through the SAME
        # resolver as every route: forbidden/unknown/malformed refs 404 alike.
        chat_ctx: ChatContext | None = None
        if chat_ref:
            chat_ctx = await _resolve_chat_ref(str(chat_ref), user)

        user_agent = request.headers.get("user-agent", "")[:500]

        success = await push_manager.subscribe(
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            chat_id=chat_ctx.chat_id if chat_ctx else None,
            user_agent=user_agent,
            username=user.username,
            allowed_accounts=sorted(user.allowed_accounts) if user.allowed_accounts is not None else None,
            allowed_chat_refs=sorted(user.allowed_chat_refs) if user.allowed_chat_refs is not None else None,
        )

        if success:
            return {"status": "subscribed", "chat_ref": chat_ctx.ref if chat_ctx else None}
        else:
            raise HTTPException(status_code=500, detail="Failed to store subscription")

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push subscribe error: {e}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
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

        success = await push_manager.unsubscribe(endpoint, username=user.username)
        return {"status": "unsubscribed" if success else "not_found"}

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Push unsubscribe error: {e}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/internal/push")
async def internal_push(request: Request):
    """
    Internal endpoint for SQLite real-time push notifications.

    The backup/listener container POSTs to this endpoint when using SQLite,
    and this broadcasts to connected WebSocket clients.

    For PostgreSQL, use LISTEN/NOTIFY instead (auto-detected).

    Access is restricted to loopback and private (RFC1918/Docker) IPs.
    Split-container SQLite setups use VIEWER_HOST/VIEWER_PORT to push
    from the backup container to the viewer container over Docker networks.

    If INTERNAL_PUSH_SECRET is set, it must be provided as a bearer token.
    This prevents co-tenant containers from spoofing live events.
    """
    import ipaddress

    client_host = request.client.host if request.client else None

    # Accept from loopback + private IPs (Docker internal, RFC1918)
    is_allowed = False
    is_loopback = False
    if client_host:
        try:
            ip = ipaddress.ip_address(client_host)
            is_loopback = ip.is_loopback
            is_allowed = is_loopback or ip.is_private
        except ValueError:
            pass

    if not is_allowed:
        logger.warning(f"Rejected /internal/push from non-private IP: {client_host}")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Require a shared secret for non-loopback private/Docker networks. Loopback
    # stays usable for single-container/local setups.
    push_secret = resolve_internal_push_secret(getattr(db.db_manager, "database_url", None) if db else None)
    if not is_loopback and not push_secret:
        logger.warning(f"Rejected /internal/push from {client_host}: INTERNAL_PUSH_SECRET is required")
        raise HTTPException(status_code=403, detail="INTERNAL_PUSH_SECRET required")
    if push_secret:
        auth_header = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth_header, f"Bearer {push_secret}"):
            logger.warning(f"Rejected /internal/push: invalid or missing secret from {client_host}")
            raise HTTPException(status_code=403, detail="Forbidden")

    try:
        payload = await request.json()
        if realtime_listener:
            await realtime_listener.handle_http_push(payload)
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Error handling internal push: {e}")
        return {"status": "error", "detail": "Internal push processing failed"}


# Cache chat stats to avoid re-running 3 aggregate queries on every chat open.
# Keyed by (account_id, chat_id): a chat id alone repeats across accounts.
_chat_stats_cache: dict[tuple[int, int], tuple[float, dict]] = {}
CHAT_STATS_CACHE_TTL_SECONDS = 60


def _get_cached_chat_stats(key: tuple[int, int]) -> dict | None:
    """Return cached stats for (account_id, chat_id) if still fresh, else None."""
    entry = _chat_stats_cache.get(key)
    if entry is None:
        return None
    cached_at, stats = entry
    if time.monotonic() - cached_at > CHAT_STATS_CACHE_TTL_SECONDS:
        _chat_stats_cache.pop(key, None)
        return None
    return stats


def _set_cached_chat_stats(key: tuple[int, int], stats: dict) -> None:
    _chat_stats_cache[key] = (time.monotonic(), stats)


@app.get("/api/chats/{chat_ref}/stats")
async def get_chat_stats(chat: ChatContext = Depends(require_chat)):
    """Get statistics for a specific chat (message count, media files, size).

    Resolution + entitlement run in the dependency, before any cache lookup, so
    a cache hit never bypasses access control.
    """
    cache_key = (chat.account_id, chat.chat_id)
    cached = _get_cached_chat_stats(cache_key)
    if cached is not None:
        return cached

    try:
        stats = await db.get_chat_stats(chat.chat_id, account_id=chat.account_id)
        _set_cached_chat_stats(cache_key, stats)
        return stats
    except Exception as e:
        logger.error(f"Error getting chat stats: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/messages/by-date")
async def get_message_by_date(
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(None, description="Timezone for date interpretation (e.g., 'Europe/Madrid')"),
    topic_id: int | None = None,
):
    """
    Find the first message on or after a specific date for navigation.
    Used by the date picker to jump to a specific date.
    """
    if timezone is not None:
        if len(timezone) > 255:
            raise HTTPException(status_code=400, detail="Invalid timezone")
        try:
            user_tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError, ValueError:
            raise HTTPException(status_code=400, detail="Invalid timezone")
    else:
        try:
            user_tz = ZoneInfo(config.viewer_timezone or "UTC")
        except ZoneInfoNotFoundError, ValueError:
            user_tz = UTC

    try:
        # Parse date string (YYYY-MM-DD) as a date in the user's timezone
        naive_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    if not 2 <= naive_date.year <= 9998:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        # Create timezone-aware datetime at start of day in user's timezone
        local_start_of_day = naive_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=user_tz)
        # Convert to UTC for database query
        target_date = local_start_of_day.astimezone(UTC).replace(tzinfo=None)
    except OverflowError, ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    try:
        message = await db.find_message_by_date_with_joins(
            chat.chat_id, target_date, topic_id, account_id=chat.account_id
        )

        if not message:
            raise HTTPException(status_code=404, detail="No messages found for this date")

        _attach_message_payload_urls([message], chat)
        if user.no_download:
            _strip_original_media_paths([message])
        return message
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error finding message by date: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/chats/{chat_ref}/messages/dates")
async def get_message_dates(
    chat: ChatContext = Depends(require_chat),
    month: str = Query(..., description="Month in YYYY-MM format"),
    timezone: str = Query(..., description="IANA timezone"),
    topic_id: int | None = None,
):
    """Return the local calendar dates containing messages in a month."""
    try:
        month_start = datetime.strptime(month, "%Y-%m")
    except OverflowError, ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM")
    if f"{month_start.year:04d}-{month_start.month:02d}" != month or not 2 <= month_start.year <= 9998:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM")

    if len(timezone) > 255:
        raise HTTPException(status_code=400, detail="Invalid timezone")
    try:
        user_tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    try:
        if month_start.month == 12:
            next_month = datetime(month_start.year + 1, 1, 1)
        else:
            next_month = datetime(month_start.year, month_start.month + 1, 1)

        day_ranges: list[tuple[str, datetime, datetime]] = []
        current_day = month_start
        while current_day < next_month:
            following_day = current_day + timedelta(days=1)
            local_start = datetime(
                current_day.year,
                current_day.month,
                current_day.day,
                tzinfo=user_tz,
            )
            local_end = datetime(
                following_day.year,
                following_day.month,
                following_day.day,
                tzinfo=user_tz,
            )
            day_ranges.append(
                (
                    f"{current_day.year:04d}-{current_day.month:02d}-{current_day.day:02d}",
                    local_start.astimezone(UTC).replace(tzinfo=None),
                    local_end.astimezone(UTC).replace(tzinfo=None),
                )
            )
            current_day = following_day
    except OverflowError, ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM")

    try:
        dates = await db.get_message_dates(chat.chat_id, day_ranges, topic_id, account_id=chat.account_id)
        return JSONResponse(
            content={
                "month": month,
                "timezone": timezone,
                "topic_id": topic_id,
                "dates": dates,
            },
            headers={"Cache-Control": "private, no-store"},
        )
    except Exception as e:
        logger.error("Error fetching message dates (%s)", type(e).__name__)
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


def _parse_export_bound(value: str, param: str, *, exclusive_end: bool) -> datetime:
    """Parse an export date bound to naive UTC (the #365 conversion contract).

    A bare date (``2026-01-31``) as the end bound means "through that whole
    day", so it becomes the NEXT midnight and the query compares with ``<``.
    Offset-bearing datetimes are converted to UTC and stripped — Message.date
    is naive UTC, so relabelling would shift the window by the client offset.
    """
    date_only = len(value) == 10
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {param} date. Use ISO 8601.") from None
    if parsed.tzinfo:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    if exclusive_end and date_only:
        try:
            parsed += timedelta(days=1)
        except OverflowError:
            # to=9999-12-31 means "no upper bound" — clamp instead of 500ing
            # on a valid ISO date.
            parsed = datetime.max
    return parsed


@app.get("/api/chats/{chat_ref}/export")
async def export_chat(
    chat: ChatContext = Depends(require_chat),
    user: UserContext = Depends(require_auth),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    """Export chat history to JSON, optionally windowed by date.

    ``from`` is inclusive; ``to`` is inclusive of the named day when given as
    a bare date (internally an exclusive next-midnight bound) and exclusive
    when given as a full timestamp. Both accept ISO 8601, tz-aware or naive.
    """
    if user.no_download:
        raise HTTPException(status_code=403, detail="Downloads disabled for this account")

    parsed_from = _parse_export_bound(from_date, "from", exclusive_end=False) if from_date else None
    parsed_to = _parse_export_bound(to_date, "to", exclusive_end=True) if to_date else None
    if parsed_from and parsed_to and parsed_from >= parsed_to:
        raise HTTPException(status_code=400, detail="'from' must be before 'to'")

    try:
        chat_row = await db.get_chat_by_id(chat.chat_id, account_id=chat.account_id)
        if not chat_row:
            raise HTTPException(status_code=404, detail="Chat not found")

        # The download filename never falls back to the chat id: the saved name
        # lands in filesystem listings, which are the same surface as a URL.
        chat_name = chat_row.get("title") or chat_row.get("username") or chat.ref
        # Sanitize filename
        safe_name = "".join(c for c in chat_name if c.isalnum() or c in (" ", "-", "_")).strip()
        filename = f"{safe_name}_export.json"

        async def iter_json():
            yield "{\n"
            yield f'  "chat": {json.dumps(_export_chat_metadata(chat_row), ensure_ascii=False, default=str)},\n'
            if parsed_from or parsed_to:
                # Record what this file contains — a windowed export must not
                # masquerade as the full history.
                window = {"from": from_date, "to": to_date}
                yield f'  "filters": {json.dumps(window, ensure_ascii=False)},\n'
            yield '  "messages": [\n'
            first = True
            async for msg in db.get_messages_for_export(
                chat.chat_id, account_id=chat.account_id, from_date=parsed_from, to_date=parsed_to
            ):
                if not first:
                    yield ",\n"
                first = False
                # Ensure UTF-8 encoding for non-Latin characters
                yield "    " + json.dumps(msg, ensure_ascii=False, default=str)
            yield "\n  ],\n"
            # Stream versions like messages: a chat's edit history can be large,
            # so it must never be materialized into a single list/dumps here.
            yield '  "message_versions": [\n'
            first_version = True
            async for version in db.iter_message_versions_for_export(
                chat.chat_id, account_id=chat.account_id, from_date=parsed_from, to_date=parsed_to
            ):
                if not first_version:
                    yield ",\n"
                first_version = False
                yield "    " + json.dumps(version, ensure_ascii=False, default=str)
            yield "\n  ]\n"
            yield "}"

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
        logger.error(f"Error exporting chat: {type(e).__name__}")
        if _is_db_connection_error(e):
            raise HTTPException(status_code=503, detail="Database temporarily unavailable")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Admin Endpoints (v7.0.0) — Master-only viewer account management
# ============================================================================


def _reject_legacy_grant_key(data: dict) -> None:
    """Refuse a 7.x ``allowed_chat_ids`` in an admin write, loudly.

    Ignoring it would create/leave the identity UNRESTRICTED — a widening the
    caller did not ask for. The error names the replacement so an old client
    fails toward the fix, not toward full access.
    """
    if "allowed_chat_ids" in data:
        raise HTTPException(status_code=400, detail="allowed_chat_ids is no longer accepted; send allowed_chat_refs")


def _refs_grant_json(value) -> str | None:
    """Validate an ``allowed_chat_refs`` payload: None, or a list of ref strings."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(ref, str) and 0 < len(ref) <= 64 for ref in value):
        raise HTTPException(status_code=400, detail="Invalid chat ref format")
    return json.dumps(sorted(set(value)))


def _accounts_grant_json(value) -> str | None:
    """Validate an ``allowed_accounts`` payload: None, or a list of account ids."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="Invalid account id format")
    try:
        accounts = sorted({int(account) for account in value})
    except TypeError, ValueError:
        raise HTTPException(status_code=400, detail="Invalid account id format")
    return json.dumps(accounts)


def _grant_list(raw: str | None, element_type: type) -> list | None:
    """Render a stored grant column for an admin response (unreadable → [], as enforced)."""
    parsed = parse_entitlement_column(raw, element_type)
    return sorted(parsed) if parsed is not None else None


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
                "allowed_accounts": _grant_list(v.get("allowed_accounts"), int),
                "allowed_chat_refs": _grant_list(v.get("allowed_chat_refs"), str),
                "is_active": v["is_active"],
                "no_download": v.get("no_download", 0),
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

    _reject_legacy_grant_key(data)
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    is_active = 1 if data.get("is_active", 1) else 0
    viewer_no_download = 1 if data.get("no_download", 0) else 0

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
    password_hash = await asyncio.to_thread(_hash_password, password, salt)

    accounts_json = _accounts_grant_json(data.get("allowed_accounts"))
    refs_json = _refs_grant_json(data.get("allowed_chat_refs"))
    restricted = accounts_json is not None or refs_json is not None

    account = await db.create_viewer_account(
        username=username,
        password_hash=password_hash,
        salt=salt,
        # Rollback tombstone: "[]" denies under a 7.x binary too; never NULL
        # for a restricted viewer. 8.0 code does not read this column.
        allowed_chat_ids="[]" if restricted else None,
        allowed_accounts=accounts_json,
        allowed_chat_refs=refs_json,
        created_by=user.username,
        is_active=is_active,
        no_download=viewer_no_download,
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
        "allowed_accounts": _grant_list(accounts_json, int),
        "allowed_chat_refs": _grant_list(refs_json, str),
        "is_active": account["is_active"],
        "no_download": account["no_download"],
    }


@app.put("/api/admin/viewers/{viewer_id}")
async def update_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Update a viewer account. Invalidates their existing sessions."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    _reject_legacy_grant_key(data)
    existing = await db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    updates = {}
    if "password" in data and data["password"]:
        pwd = data["password"].strip()
        if len(pwd) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        salt = secrets.token_hex(32)
        updates["password_hash"] = await asyncio.to_thread(_hash_password, pwd, salt)
        updates["salt"] = salt

    if "allowed_accounts" in data:
        updates["allowed_accounts"] = _accounts_grant_json(data["allowed_accounts"])
    if "allowed_chat_refs" in data:
        updates["allowed_chat_refs"] = _refs_grant_json(data["allowed_chat_refs"])
    if "allowed_accounts" in data or "allowed_chat_refs" in data:
        # Keep the rollback tombstone in step with the FINAL grant state, so a
        # viewer widened to unrestricted loses the "[]" and a narrowed one
        # gains it (checking the reverse of the create path).
        final_accounts = updates.get("allowed_accounts", existing.get("allowed_accounts"))
        final_refs = updates.get("allowed_chat_refs", existing.get("allowed_chat_refs"))
        updates["allowed_chat_ids"] = "[]" if (final_accounts is not None or final_refs is not None) else None

    if "is_active" in data:
        updates["is_active"] = 1 if data["is_active"] else 0

    if "no_download" in data:
        updates["no_download"] = 1 if data["no_download"] else 0

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    account = await db.update_viewer_account(viewer_id, **updates)
    await _invalidate_user_sessions(existing["username"])

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
        "allowed_accounts": _grant_list(account.get("allowed_accounts"), int),
        "allowed_chat_refs": _grant_list(account.get("allowed_chat_refs"), str),
        "is_active": account["is_active"],
    }


@app.delete("/api/admin/viewers/{viewer_id}")
async def delete_viewer(viewer_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Delete a viewer account and invalidate their sessions."""
    existing = await db.get_viewer_account(viewer_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Viewer not found")

    await _invalidate_user_sessions(existing["username"])
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
    """List all chats for the admin chat picker (includes user metadata for display)."""
    chats = await db.get_all_chats()
    result = []
    for c in chats:
        title = c.get("title")
        if not title:
            parts = [c.get("first_name", ""), c.get("last_name", "")]
            title = " ".join(p for p in parts if p) or c.get("username") or str(c["id"])
        result.append(
            {
                "id": c["id"],
                "ref": c.get("ref"),
                "account_id": c.get("account_id"),
                "title": title,
                "type": c.get("type"),
                "username": c.get("username"),
                "first_name": c.get("first_name"),
                "last_name": c.get("last_name"),
            }
        )
    return {"chats": result}


@app.get("/api/admin/audit")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    username: str | None = Query(None),
    action: str | None = Query(None),
    user: UserContext = Depends(require_master),
):
    """Get paginated audit log entries with optional username and action filters."""
    logs = await db.get_audit_logs(limit=limit, offset=offset, username=username, action=action)
    return {"logs": logs, "limit": limit, "offset": offset}


# ============================================================================
# Share Token Admin Endpoints (v7.2.0) — Master-only token management
# ============================================================================


@app.get("/api/admin/tokens")
async def list_tokens(user: UserContext = Depends(require_master)):
    """List all share tokens."""
    tokens = await db.get_all_viewer_tokens()
    safe = []
    for t in tokens:
        safe.append(
            {
                "id": t["id"],
                "label": t["label"],
                "created_by": t["created_by"],
                "allowed_accounts": _grant_list(t.get("allowed_accounts"), int),
                "allowed_chat_refs": _grant_list(t.get("allowed_chat_refs"), str),
                "is_revoked": t["is_revoked"],
                "no_download": t["no_download"],
                "expires_at": t["expires_at"],
                "last_used_at": t["last_used_at"],
                "use_count": t["use_count"],
                "created_at": t["created_at"],
            }
        )
    return {"tokens": safe}


@app.post("/api/admin/tokens")
async def create_token(request: Request, user: UserContext = Depends(require_master)):
    """Create a new share token. Returns the plaintext token only once."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    _reject_legacy_grant_key(data)
    label = (data.get("label") or "").strip() or None
    allowed_chat_refs = data.get("allowed_chat_refs")
    no_download = 1 if data.get("no_download") else 0
    expires_at = None
    if data.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid expires_at format. Use ISO 8601.")
        # Expiry is compared against naive UTC — convert the instant, never just relabel it
        if expires_at.tzinfo:
            expires_at = expires_at.astimezone(UTC)
        expires_at = expires_at.replace(tzinfo=None)

    # A share token is ALWAYS scoped: no refs, no token.
    if not allowed_chat_refs or not isinstance(allowed_chat_refs, list):
        raise HTTPException(status_code=400, detail="allowed_chat_refs is required (list of chat refs)")
    refs_json = _refs_grant_json(allowed_chat_refs)

    # Generate token: 32 bytes = 64 hex chars
    plaintext_token = secrets.token_hex(32)
    salt = secrets.token_hex(32)
    token_hash = await asyncio.to_thread(_hash_token, plaintext_token, salt)

    token_record = await db.create_viewer_token(
        label=label,
        token_hash=token_hash,
        token_salt=salt,
        created_by=user.username,
        # NOT NULL rollback tombstone: under a 7.x binary this token grants nothing.
        allowed_chat_ids="[]",
        allowed_chat_refs=refs_json,
        no_download=no_download,
        expires_at=expires_at,
    )

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_created:{token_record['id']}",
        endpoint="/api/admin/tokens",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": token_record["id"],
        "label": token_record["label"],
        "token": plaintext_token,  # Only returned once at creation time
        "allowed_chat_refs": _grant_list(refs_json, str),
        "no_download": token_record["no_download"],
        "expires_at": token_record["expires_at"],
        "created_at": token_record["created_at"],
    }


@app.put("/api/admin/tokens/{token_id}")
async def update_token(token_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Update a share token (label, allowed_chat_refs, is_revoked, no_download)."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    _reject_legacy_grant_key(data)
    updates = {}
    if "label" in data:
        updates["label"] = (data["label"] or "").strip() or None
    if "allowed_chat_refs" in data:
        allowed = data["allowed_chat_refs"]
        if allowed is None or not isinstance(allowed, list) or not allowed:
            raise HTTPException(status_code=400, detail="allowed_chat_refs must be a non-empty list")
        updates["allowed_chat_refs"] = _refs_grant_json(allowed)
        updates["allowed_chat_ids"] = "[]"  # keep the rollback tombstone denying
    if "is_revoked" in data:
        updates["is_revoked"] = 1 if data["is_revoked"] else 0
    if "no_download" in data:
        updates["no_download"] = 1 if data["no_download"] else 0

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated = await db.update_viewer_token(token_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Token not found")

    # Invalidate all active sessions from this token when scope/access changes
    scope_changed = any(k in updates for k in ("is_revoked", "allowed_chat_refs", "no_download"))
    if scope_changed:
        await _invalidate_token_sessions(token_id)

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_updated:{token_id}",
        endpoint=f"/api/admin/tokens/{token_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {
        "id": updated["id"],
        "label": updated["label"],
        "allowed_chat_refs": _grant_list(updated.get("allowed_chat_refs"), str),
        "is_revoked": updated["is_revoked"],
        "no_download": updated["no_download"],
        "expires_at": updated["expires_at"],
    }


@app.delete("/api/admin/tokens/{token_id}")
async def delete_token(token_id: int, request: Request, user: UserContext = Depends(require_master)):
    """Delete a share token permanently and invalidate all its active sessions."""
    await _invalidate_token_sessions(token_id)
    deleted = await db.delete_viewer_token(token_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Token not found")

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"token_deleted:{token_id}",
        endpoint=f"/api/admin/tokens/{token_id}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True}


# ============================================================================
# App Settings Endpoints (v7.2.0) — Master-only key-value configuration
# ============================================================================


@app.get("/api/admin/settings")
async def get_settings(user: UserContext = Depends(require_master)):
    """Get all app settings."""
    settings = await db.get_all_settings()
    return {"settings": settings}


@app.put("/api/admin/settings/{key}")
async def set_setting(key: str, request: Request, user: UserContext = Depends(require_master)):
    """Set an app setting value."""
    if not key or len(key) > 255:
        raise HTTPException(status_code=400, detail="Invalid key")

    try:
        data = await request.json()
        value = data.get("value")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if value is None:
        raise HTTPException(status_code=400, detail="value is required")

    await db.set_setting(key, str(value))

    await db.create_audit_log(
        username=user.username,
        role="master",
        action=f"setting_updated:{key}",
        endpoint=f"/api/admin/settings/{key}",
        ip_address=request.client.host if request.client else None,
    )

    return {"key": key, "value": str(value)}


# ============================================================================
# Real-time WebSocket Endpoints (v5.0)
# ============================================================================


@app.get("/api/notifications/settings")
async def get_notification_settings(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Get notification settings for the viewer."""
    if AUTH_ENABLED:
        session = (await _resolve_session(auth_cookie)) if auth_cookie else None
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
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=4003, reason="Forbidden origin")
        return

    # Validate auth before accepting, through the SAME resolver the HTTP routes
    # use: proxy header first, then the session cookie, then the anonymous
    # fallback. A socket that resolves to no principal is closed rather than
    # connected, and the ACL it carries is that principal's ACL.
    auth_cookie = websocket.cookies.get(AUTH_COOKIE_NAME)
    try:
        user_ctx = await _resolve_user_context(
            websocket.headers.get(AUTH_PROXY_HEADER) if _PROXY_AUTH_ENABLED else None,
            auth_cookie,
        )
    except HTTPException as exc:
        await websocket.close(code=4001, reason=exc.detail)
        return

    if not await ws_manager.connect(websocket, user_ctx, _socket_identity(user_ctx, auth_cookie)):
        return

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "subscribe":
                chat_ref = data.get("chat_ref")
                if isinstance(chat_ref, str) and chat_ref:
                    # The SAME resolver + entitlement as every HTTP route (the
                    # parity requirement): unknown, malformed and forbidden refs
                    # are denied identically, and a DB outage denies too.
                    try:
                        await _resolve_chat_ref(chat_ref, user_ctx)
                    except HTTPException:
                        await websocket.send_json({"type": "subscribe_denied", "chat_ref": chat_ref})
                    else:
                        if ws_manager.subscribe(websocket, chat_ref):
                            await websocket.send_json({"type": "subscribed", "chat_ref": chat_ref})
                        else:
                            # Entitled but over the per-connection cap.
                            await websocket.send_json(
                                {"type": "subscribe_denied", "chat_ref": chat_ref, "reason": "subscription_limit"}
                            )

            elif action == "unsubscribe":
                chat_ref = data.get("chat_ref")
                if isinstance(chat_ref, str) and chat_ref:
                    ws_manager.unsubscribe(websocket, chat_ref)
                    await websocket.send_json({"type": "unsubscribed", "chat_ref": chat_ref})

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


async def broadcast_new_message(chat_id: int, message: dict, account_id: int | None = None) -> None:
    """Broadcast a new message to subscribed clients (frames are ref-addressed)."""
    chat = await _broadcast_chat_row(chat_id, account_id)
    if chat is None:
        return
    await ws_manager.broadcast_to_chat(chat, {"type": "new_message", "chat_ref": chat["ref"], "message": message})


async def broadcast_message_edit(
    chat_id: int, message_id: int, new_text: str, edit_date: str, account_id: int | None = None
) -> None:
    """Broadcast a message edit to subscribed clients (frames are ref-addressed)."""
    chat = await _broadcast_chat_row(chat_id, account_id)
    if chat is None:
        return
    await ws_manager.broadcast_to_chat(
        chat,
        {
            "type": "edit",
            "chat_ref": chat["ref"],
            "message_id": message_id,
            "new_text": new_text,
            "edit_date": edit_date,
        },
    )


async def broadcast_message_delete(
    chat_id: int,
    message_id: int,
    deletion_mode: str = "hard",
    deleted_at: str | None = None,
    account_id: int | None = None,
) -> None:
    """Broadcast a message deletion to subscribed clients (frames are ref-addressed)."""
    chat = await _broadcast_chat_row(chat_id, account_id)
    if chat is None:
        return
    await ws_manager.broadcast_to_chat(
        chat,
        {
            "type": "delete",
            "chat_ref": chat["ref"],
            "message_id": message_id,
            "deletion_mode": deletion_mode,
            "deleted_at": deleted_at,
        },
    )
