"""
Web viewer for Telegram Backup.

FastAPI application providing a web interface to browse backed-up messages.
v3.0: Async database operations with SQLAlchemy.
"""

from fastapi import FastAPI, Request, HTTPException, Query, Depends, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import logging
import glob
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, AsyncGenerator
from pathlib import Path
import hashlib
import json

from ..config import Config
from ..db import DatabaseAdapter, init_database, close_database

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifecycle - initialize and cleanup database."""
    global db
    logger.info("Initializing database connection...")
    db_manager = await init_database()
    db = DatabaseAdapter(db_manager)
    logger.info("Database connection established")
    
    # Normalize display chat IDs (auto-correct missing -100 prefix)
    await _normalize_display_chat_ids()
    
    yield
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
AUTH_COOKIE_NAME = "viewer_auth"
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
            response.set_cookie(
                key=AUTH_COOKIE_NAME,
                value=AUTH_TOKEN,
                httponly=True,
                samesite="lax",
                max_age=30 * 24 * 60 * 60,  # 30 days
            )
            return response
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=400, detail="Invalid request")


def _find_avatar_path(chat_id: int, chat_type: str) -> Optional[str]:
    """Find avatar file path for a chat."""
    # Determine folder: 'chats' for groups/channels, 'users' for private
    avatar_folder = 'users' if chat_type == 'private' else 'chats'
    avatar_dir = os.path.join(config.media_path, 'avatars', avatar_folder)
    
    if not os.path.exists(avatar_dir):
        return None
    
    # Look for avatar file matching chat_id (they're stored as {chat_id}_{photo_id}.jpg)
    pattern = os.path.join(avatar_dir, f'{chat_id}_*.jpg')
    matches = glob.glob(pattern)
    
    if matches:
        # Return the most recently modified avatar (newest profile photo)
        newest_avatar = max(matches, key=os.path.getmtime)
        avatar_file = os.path.basename(newest_avatar)
        return f"avatars/{avatar_folder}/{avatar_file}"
    
    return None


@app.get("/api/chats", dependencies=[Depends(require_auth)])
async def get_chats():
    """Get all chats with metadata, including avatar URLs."""
    try:
        chats = await db.get_all_chats()
        
        # Filter to display chats if configured
        if config.display_chat_ids:
            chats = [c for c in chats if c['id'] in config.display_chat_ids]
        
        # Add avatar URLs to each chat
        for chat in chats:
            try:
                avatar_path = _find_avatar_path(chat['id'], chat.get('type', 'private'))
                if avatar_path:
                    chat['avatar_url'] = f"/media/{avatar_path}"
                else:
                    chat['avatar_url'] = None
            except Exception as e:
                logger.error(f"Error finding avatar for chat {chat.get('id')}: {e}")
                chat['avatar_url'] = None
        
        return chats
    except Exception as e:
        logger.error(f"Error fetching chats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats/{chat_id}/messages", dependencies=[Depends(require_auth)])
async def get_messages(
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
):
    """Get messages for a specific chat with user and media info."""
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    try:
        messages = await db.get_messages_paginated(
            chat_id=chat_id,
            limit=limit,
            offset=offset,
            search=search
        )
        return messages
    except Exception as e:
        logger.error(f"Error fetching messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def get_stats():
    """Get backup statistics."""
    try:
        stats = await db.get_statistics()
        stats['timezone'] = config.viewer_timezone
        return stats
    except Exception as e:
        logger.error(f"Error fetching stats: {e}", exc_info=True)
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
