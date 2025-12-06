from fastapi import FastAPI, Request, HTTPException, Query, Depends, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
import logging
import glob
import time
import sqlite3
from typing import Optional, List
from pathlib import Path
import hashlib
import json

from ..config import Config
from ..database import Database

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Telegram Backup Viewer")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize config and database
config = Config()
db = Database(config.database_path)

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

@app.get("/api/auth/status")
def auth_status(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """
    Return whether auth is required and if the current client is authenticated.
    Used by the frontend to decide whether to show the login form.
    """
    if not AUTH_ENABLED:
        return {"auth_required": False, "authenticated": True}

    is_auth = bool(auth_cookie and auth_cookie == AUTH_TOKEN)
    return {"auth_required": True, "authenticated": is_auth}


@app.post("/api/login")
def login(payload: dict, request: Request):
    """Simple username/password login; sets an auth cookie on success."""
    if not AUTH_ENABLED:
        # If auth is disabled, always "succeed"
        return JSONResponse({"success": True, "auth_required": False})

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()

    if username != VIEWER_USERNAME or password != VIEWER_PASSWORD:
        logger.warning(f"Login failed for user '{username}'. Expected len: {len(VIEWER_USERNAME)}, Got len: {len(username)}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    response = JSONResponse({"success": True, "auth_required": True})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        AUTH_TOKEN,
        httponly=True,
        samesite="lax",
        secure=False,  # Set to True if using HTTPS
        max_age=30 * 24 * 60 * 60,  # 30 days
        path="/",
    )
    return response


@app.post("/api/logout")
def logout():
    """Clear the auth cookie."""
    if not AUTH_ENABLED:
        return JSONResponse({"success": True})

    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


def _find_avatar_path(chat_id: int, chat_type: str) -> Optional[str]:
    """
    Find the most recent avatar file for a chat or user.
    
    Returns the path relative to media_path, or None if no avatar found.
    """
    if chat_type == 'private':
        avatar_dir = os.path.join(config.media_path, "avatars", "users")
    else:
        avatar_dir = os.path.join(config.media_path, "avatars", "chats")
    
    if not os.path.exists(avatar_dir):
        return None
    
    # Look for files matching {chat_id}_*.jpg
    pattern = os.path.join(avatar_dir, f"{chat_id}_*.jpg")
    matches = glob.glob(pattern)
    
    if not matches:
        return None
    
    # Return the most recent file (by modification time)
    most_recent = max(matches, key=os.path.getmtime)
    # Return path relative to media_path for URL construction
    rel_path = os.path.relpath(most_recent, config.media_path)
    return rel_path.replace('\\', '/')  # Normalize for URLs

@app.get("/api/chats", dependencies=[Depends(require_auth)])
def get_chats():
    """Get all chats with metadata, including avatar URLs."""
    max_retries = 3
    retry_delay = 1.0  # seconds
    
    for attempt in range(max_retries):
        try:
            chats = db.get_all_chats()
            
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
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                logger.warning(f"Database locked, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            # Last attempt or different error
            logger.error(f"Database locked after {max_retries} attempts")
            raise HTTPException(
                status_code=503,
                detail="Database is currently busy (backup in progress). Please try again in a few moments."
            )
        except Exception as e:
            logger.error(f"Error fetching chats: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chats/{chat_id}/messages", dependencies=[Depends(require_auth)])
def get_messages(
    chat_id: int,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
):
    """
    Get messages for a specific chat.

    We join with the media table so the web UI can show better previews
    (e.g. original filenames for documents and thumbnails for image documents).
    """
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    cursor = db.conn.cursor()

    query = """
        SELECT 
            m.*,
            u.first_name,
            u.last_name,
            u.username,
            md.file_name AS media_file_name,
            md.mime_type AS media_mime_type
        FROM messages m
        LEFT JOIN users u ON m.sender_id = u.id
        LEFT JOIN media md ON md.id = m.media_id
        WHERE m.chat_id = ?
    """
    params: List[object] = [chat_id]

    if search:
        query += " AND m.text LIKE ?"
        params.append(f"%{search}%")

    query += " ORDER BY m.date DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    messages = [dict(row) for row in cursor.fetchall()]

    # Populate reply_to_text from database if missing
    for msg in messages:
        if msg.get('reply_to_msg_id') and not msg.get('reply_to_text'):
            cursor.execute(
                "SELECT text FROM messages WHERE chat_id = ? AND id = ?",
                (chat_id, msg['reply_to_msg_id'])
            )
            reply_row = cursor.fetchone()
            if reply_row and reply_row['text']:
                # Truncate to 100 chars like Telegram does
                msg['reply_to_text'] = reply_row['text'][:100]

    return messages

@app.get("/api/stats", dependencies=[Depends(require_auth)])
def get_stats():
    """Get backup statistics."""
    return db.get_statistics()

@app.get("/api/chats/{chat_id}/export", dependencies=[Depends(require_auth)])
def export_chat(chat_id: int):
    """Export chat history to JSON."""
    # Restrict access in display mode
    if config.display_chat_ids and chat_id not in config.display_chat_ids:
        raise HTTPException(status_code=403, detail="Access denied")
    
    cursor = db.conn.cursor()
    
    # Get chat info for filename
    cursor.execute("SELECT * FROM chats WHERE id = ?", (chat_id,))
    chat = cursor.fetchone()
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
        
    chat_name = chat['title'] or chat['username'] or str(chat_id)
    # Sanitize filename
    safe_name = "".join(c for c in chat_name if c.isalnum() or c in (' ', '-', '_')).strip()
    filename = f"{safe_name}_export.json"
    
    # Get messages
    cursor.execute("""
        SELECT 
            m.id, m.date, m.text, m.is_outgoing,
            u.first_name, u.last_name, u.username,
            m.reply_to_msg_id
        FROM messages m
        LEFT JOIN users u ON m.sender_id = u.id
        WHERE m.chat_id = ?
        ORDER BY m.date ASC
    """, (chat_id,))
    
    def iter_json():
        yield '[\n'
        first = True
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                if not first:
                    yield ',\n'
                first = False
                
                msg = dict(row)
                # Format for export
                export_msg = {
                    'id': msg['id'],
                    'date': msg['date'],
                    'sender': {
                        'name': f"{msg['first_name'] or ''} {msg['last_name'] or ''}".strip() or msg['username'] or "Unknown",
                        'username': msg['username']
                    },
                    'text': msg['text'],
                    'is_outgoing': bool(msg['is_outgoing']),
                    'reply_to': msg['reply_to_msg_id']
                }
                yield json.dumps(export_msg)
        yield '\n]'

    return StreamingResponse(
        iter_json(),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
