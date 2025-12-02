from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
import logging
from typing import Optional, List
from pathlib import Path

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

# Setup paths
templates_dir = Path(__file__).parent / "templates"

# Mount media directory
if os.path.exists(config.media_path):
    app.mount("/media", StaticFiles(directory=config.media_path), name="media")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main application page."""
    return FileResponse(templates_dir / "index.html")

@app.get("/api/chats")
def get_chats():
    """Get all chats with metadata."""
    chats = db.get_all_chats()
    return chats

@app.get("/api/chats/{chat_id}/messages")
def get_messages(
    chat_id: int, 
    limit: int = 50, 
    offset: int = 0,
    search: Optional[str] = None
):
    """Get messages for a specific chat."""
    # We need to implement pagination in database.py or do it here
    # For now, let's add a method to database.py for paginated messages
    
    # This is a temporary direct query until we update database.py
    cursor = db.conn.cursor()
    
    query = """
        SELECT m.*, u.first_name, u.last_name, u.username 
        FROM messages m
        LEFT JOIN users u ON m.sender_id = u.id
        WHERE m.chat_id = ?
    """
    params = [chat_id]
    
    if search:
        query += " AND m.text LIKE ?"
        params.append(f"%{search}%")
        
    query += " ORDER BY m.date DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    cursor.execute(query, params)
    messages = [dict(row) for row in cursor.fetchall()]
    
    # Reverse to show oldest first in the view (if we were doing infinite scroll up)
    # But for initial load usually we want latest. 
    # Let's keep DESC for API and let frontend handle display order.
    
    return messages

@app.get("/api/stats")
def get_stats():
    """Get backup statistics."""
    return db.get_statistics()
