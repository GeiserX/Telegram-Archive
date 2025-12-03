from fastapi import FastAPI, Request, HTTPException, Query, Depends, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
import logging
import glob
from typing import Optional, List
from pathlib import Path
import hashlib

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
VIEWER_USERNAME = os.getenv("VIEWER_USERNAME")
VIEWER_PASSWORD = os.getenv("VIEWER_PASSWORD")
AUTH_ENABLED = bool(VIEWER_USERNAME and VIEWER_PASSWORD)
AUTH_COOKIE_NAME = "viewer_auth"
AUTH_TOKEN = None

if AUTH_ENABLED:
    AUTH_TOKEN = hashlib.sha256(
        f"{VIEWER_USERNAME}:{VIEWER_PASSWORD}".encode("utf-8")
    ).hexdigest()
    logger.info("Viewer authentication is ENABLED")
else:
    logger.info("Viewer authentication is DISABLED (no VIEWER_USERNAME / VIEWER_PASSWORD set)")


def require_auth(auth_cookie: str | None = Cookie(default=None)):
    """Dependency that enforces cookie-based viewer auth when enabled."""
    if not AUTH_ENABLED:
        return

    if not auth_cookie or auth_cookie != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# Setup paths
templates_dir = Path(__file__).parent / "templates"

# Mount media directory (includes avatars)
if os.path.exists(config.media_path):
    app.mount("/media", StaticFiles(directory=config.media_path), name="media")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page."""
    return FileResponse(templates_dir / "index.html")

@app.get("/api/auth/status")
def auth_status(auth_cookie: str | None = Cookie(default=None)):
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

    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))

    if username != VIEWER_USERNAME or password != VIEWER_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    response = JSONResponse({"success": True, "auth_required": True})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        AUTH_TOKEN,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60,  # 30 days
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
    chats = db.get_all_chats()
    
    # Add avatar URLs to each chat
    for chat in chats:
        avatar_path = _find_avatar_path(chat['id'], chat.get('type', 'private'))
        if avatar_path:
            chat['avatar_url'] = f"/media/{avatar_path}"
        else:
            chat['avatar_url'] = None
    
    return chats

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

    return messages

@app.get("/api/stats", dependencies=[Depends(require_auth)])
def get_stats():
    """Get backup statistics."""
    return db.get_statistics()
