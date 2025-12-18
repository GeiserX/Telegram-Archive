"""
Database operations for Telegram Backup Automation.
Manages SQLite database schema and CRUD operations for messages, chats, and media.
"""

import sqlite3
import logging
import json
import os
import shutil
import glob
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple, Callable
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)


def retry_on_locked(max_retries: int = 5, initial_delay: float = 0.1, max_delay: float = 2.0, backoff_factor: float = 2.0):
    """
    Decorator to retry database operations on OperationalError (database locked).
    
    Args:
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds before first retry
        max_delay: Maximum delay between retries in seconds
        backoff_factor: Multiplier for exponential backoff
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(self, *args, **kwargs)
                except sqlite3.OperationalError as e:
                    if 'locked' not in str(e).lower():
                        # Not a lock error, re-raise immediately
                        raise
                    
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"Database locked on {func.__name__}, attempt {attempt + 1}/{max_retries + 1}. "
                            f"Retrying in {delay:.2f}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                    else:
                        logger.error(
                            f"Database locked on {func.__name__} after {max_retries + 1} attempts. "
                            f"Giving up."
                        )
                        raise
            
            # Should never reach here, but just in case
            if last_exception:
                raise last_exception
                
        return wrapper
    return decorator


class Database:
    """SQLite database manager for Telegram backup data."""
    
    def __init__(self, db_path: str, timeout: float = 60.0):
        """
        Initialize database connection and create schema if needed.
        
        Args:
            db_path: Path to SQLite database file
            timeout: Connection timeout in seconds (default 60s, increased for better resilience)
        """
        self.db_path = db_path
        # timeout: wait up to N seconds if database is locked
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=timeout)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        
        # Enable WAL mode for better concurrent read/write performance
        # WAL allows readers to proceed while a writer is active
        self.conn.execute('PRAGMA journal_mode=WAL')
        # Increase busy_timeout to 60 seconds (60000ms) for better resilience
        busy_timeout_ms = int(timeout * 1000)
        self.conn.execute(f'PRAGMA busy_timeout={busy_timeout_ms}')
        
        # Additional optimizations for concurrent access
        self.conn.execute('PRAGMA synchronous=NORMAL')  # Faster than FULL, still safe with WAL
        self.conn.execute('PRAGMA cache_size=-64000')  # 64MB cache for better performance
        
        self._create_schema()
        logger.info(f"Database initialized at {db_path} (WAL mode enabled, timeout={timeout}s, busy_timeout={busy_timeout_ms}ms)")
    
    def _create_schema(self):
        """Create database schema if it doesn't exist."""
        cursor = self.conn.cursor()
        
        # Chats table (users, groups, channels)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                description TEXT,
                participants_count INTEGER,
                last_synced_message_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER,
                date TIMESTAMP NOT NULL,
                text TEXT,
                reply_to_msg_id INTEGER,
                reply_to_text TEXT,
                forward_from_id INTEGER,
                edit_date TIMESTAMP,
                media_type TEXT,
                media_id TEXT,
                media_path TEXT,
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_outgoing INTEGER DEFAULT 0,
                PRIMARY KEY (id, chat_id),
                FOREIGN KEY (chat_id) REFERENCES chats(id)
            )
        ''')
        
        # Users table (message senders)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                is_bot INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Media table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY,
                message_id INTEGER,
                chat_id INTEGER,
                type TEXT,
                file_path TEXT,
                file_name TEXT,
                file_size INTEGER,
                mime_type TEXT,
                width INTEGER,
                height INTEGER,
                duration INTEGER,
                downloaded INTEGER DEFAULT 0,
                download_date TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (message_id, chat_id) REFERENCES messages(id, chat_id)
            )
        ''')

        # Reactions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                user_id INTEGER,
                count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id, chat_id, emoji, user_id),
                FOREIGN KEY (message_id, chat_id) REFERENCES messages(id, chat_id)
            )
        ''')
        
        # Sync status table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_status (
                chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER DEFAULT 0,
                last_sync_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_count INTEGER DEFAULT 0,
                FOREIGN KEY (chat_id) REFERENCES chats(id)
            )
        ''')
        
        # Metadata table (key-value store for app settings/state)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_message ON media(message_id, chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id, chat_id)')
        
        self.conn.commit()
        logger.debug("Database schema created/verified")

    def set_metadata(self, key: str, value: str):
        """Set a metadata key-value pair."""
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)', (key, value))
        self.conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        """Get a metadata value by key."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row['value'] if row else None

    def backfill_is_outgoing(self, owner_id: int):
        """
        Backfill is_outgoing flag for messages sent by the owner.
        
        Args:
            owner_id: The user ID of the account owner
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE messages 
            SET is_outgoing = 1 
            WHERE sender_id = ? AND (is_outgoing = 0 OR is_outgoing IS NULL)
        ''', (owner_id,))
        if cursor.rowcount > 0:
            logger.info(f"Backfilled is_outgoing=1 for {cursor.rowcount} messages from owner {owner_id}")
            self.conn.commit()
    
    @retry_on_locked(max_retries=5, initial_delay=0.1, max_delay=2.0)
    def upsert_chat(self, chat_data: Dict[str, Any]) -> int:
        """
        Insert or update a chat record.
        
        Args:
            chat_data: Dictionary with chat information
            
        Returns:
            Chat ID
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO chats (
                id, type, title, username, first_name, last_name, 
                phone, description, participants_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                title = excluded.title,
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                phone = excluded.phone,
                description = excluded.description,
                participants_count = excluded.participants_count,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            chat_data['id'],
            chat_data.get('type', 'unknown'),
            chat_data.get('title'),
            chat_data.get('username'),
            chat_data.get('first_name'),
            chat_data.get('last_name'),
            chat_data.get('phone'),
            chat_data.get('description'),
            chat_data.get('participants_count')
        ))
        self.conn.commit()
        return chat_data['id']
    
    def upsert_user(self, user_data: Dict[str, Any]):
        """
        Insert or update a user record.
        
        Args:
            user_data: Dictionary with user information
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO users (
                id, username, first_name, last_name, phone, is_bot, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                phone = excluded.phone,
                is_bot = excluded.is_bot,
                updated_at = CURRENT_TIMESTAMP
        ''', (
            user_data['id'],
            user_data.get('username'),
            user_data.get('first_name'),
            user_data.get('last_name'),
            user_data.get('phone'),
            1 if user_data.get('is_bot') else 0
        ))
        self.conn.commit()
    
    def _serialize_raw_data(self, raw_data: Any) -> str:
        """
        Safely serialize raw_data to JSON, converting non-serializable objects to strings.
        
        Args:
            raw_data: Data to serialize (dict, list, or other)
            
        Returns:
            JSON string representation
        """
        if not raw_data:
            return '{}'
        
        try:
            return json.dumps(raw_data)
        except (TypeError, ValueError) as e:
            # If serialization fails, try to convert objects to strings
            logger.warning(f"Failed to serialize raw_data directly: {e}. Attempting to convert objects...")
            try:
                # Recursively convert non-serializable objects to strings
                def convert_to_serializable(obj):
                    if isinstance(obj, dict):
                        return {k: convert_to_serializable(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [convert_to_serializable(item) for item in obj]
                    elif isinstance(obj, (str, int, float, bool, type(None))):
                        return obj
                    else:
                        # Convert any other object to string
                        return str(obj)
                
                serializable_data = convert_to_serializable(raw_data)
                return json.dumps(serializable_data)
            except Exception as e2:
                logger.error(f"Failed to serialize raw_data even after conversion: {e2}. Storing empty dict.")
                return '{}'
    
    def insert_message(self, message_data: Dict[str, Any]):
        """
        Insert a message record.
        
        Args:
            message_data: Dictionary with message information
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO messages (
                id, chat_id, sender_id, date, text, reply_to_msg_id, reply_to_text,
                forward_from_id, edit_date, media_type, media_id, 
                media_path, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            message_data['id'],
            message_data['chat_id'],
            message_data.get('sender_id'),
            message_data['date'],
            message_data.get('text'),
            message_data.get('reply_to_msg_id'),
            message_data.get('reply_to_text'),
            message_data.get('forward_from_id'),
            message_data.get('edit_date'),
            message_data.get('media_type'),
            message_data.get('media_id'),
            message_data.get('media_path'),
            self._serialize_raw_data(message_data.get('raw_data', {}))
        ))
        self.conn.commit()

    @retry_on_locked(max_retries=5, initial_delay=0.1, max_delay=2.0)
    def insert_messages_batch(self, messages_data: List[Dict[str, Any]]):
        """
        Insert multiple message records in a single transaction.
        
        Args:
            messages_data: List of dictionaries with message information
        """
        if not messages_data:
            return
            
        cursor = self.conn.cursor()
        cursor.executemany('''
            INSERT OR REPLACE INTO messages (
                id, chat_id, sender_id, date, text, reply_to_msg_id, reply_to_text,
                forward_from_id, edit_date, media_type, media_id, 
                media_path, raw_data, is_outgoing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', [
            (
                m['id'],
                m['chat_id'],
                m.get('sender_id'),
                m['date'],
                m.get('text'),
                m.get('reply_to_msg_id'),
                m.get('reply_to_text'),
                m.get('forward_from_id'),
                m.get('edit_date'),
                m.get('media_type'),
                m.get('media_id'),
                m.get('media_path'),
                self._serialize_raw_data(m.get('raw_data', {})),
                m.get('is_outgoing', 0)
            ) for m in messages_data
        ])
        self.conn.commit()
    
    def insert_media(self, media_data: Dict[str, Any]):
        """
        Insert a media file record.
        
        Args:
            media_data: Dictionary with media information
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO media (
                id, message_id, chat_id, type, file_name, file_path,
                file_size, mime_type, width, height, duration,
                downloaded, download_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            media_data['id'],
            media_data.get('message_id'),
            media_data.get('chat_id'),
            media_data['type'],
            media_data.get('file_name'),
            media_data.get('file_path'),
            media_data.get('file_size'),
            media_data.get('mime_type'),
            media_data.get('width'),
            media_data.get('height'),
            media_data.get('duration'),
            1 if media_data.get('downloaded') else 0,
            media_data.get('download_date')
        ))
        self.conn.commit()
    
    @retry_on_locked(max_retries=5, initial_delay=0.1, max_delay=2.0)
    def insert_reactions(self, message_id: int, chat_id: int, reactions: List[Dict[str, Any]]):
        """
        Insert reactions for a message.
        
        Args:
            message_id: Message ID
            chat_id: Chat ID
            reactions: List of reaction dictionaries with 'emoji' and optionally 'user_id' and 'count'
        """
        if not reactions:
            return
        
        cursor = self.conn.cursor()
        # Delete existing reactions for this message first
        cursor.execute('DELETE FROM reactions WHERE message_id = ? AND chat_id = ?', (message_id, chat_id))
        
        # Insert new reactions
        for reaction in reactions:
            cursor.execute('''
                INSERT INTO reactions (message_id, chat_id, emoji, user_id, count)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                message_id,
                chat_id,
                reaction['emoji'],
                reaction.get('user_id'),
                reaction.get('count', 1)
            ))
        self.conn.commit()
    
    def get_reactions(self, message_id: int, chat_id: int) -> List[Dict[str, Any]]:
        """
        Get all reactions for a message.
        
        Args:
            message_id: Message ID
            chat_id: Chat ID
            
        Returns:
            List of reaction dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT emoji, user_id, count
            FROM reactions
            WHERE message_id = ? AND chat_id = ?
            ORDER BY emoji
        ''', (message_id, chat_id))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_last_message_id(self, chat_id: int) -> int:
        """
        Get the last synced message ID for a chat.
        
        Args:
            chat_id: Chat identifier
            
        Returns:
            Last message ID, or 0 if no messages synced yet
        """
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT last_message_id FROM sync_status WHERE chat_id = ?',
            (chat_id,)
        )
        row = cursor.fetchone()
        return row['last_message_id'] if row else 0
    
    @retry_on_locked(max_retries=5, initial_delay=0.1, max_delay=2.0)
    def update_sync_status(self, chat_id: int, last_message_id: int, message_count: int):
        """
        Update sync status for a chat.
        
        Args:
            chat_id: Chat identifier
            last_message_id: ID of the last synced message
            message_count: Number of messages synced in this batch
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sync_status (chat_id, last_message_id, last_sync_date, message_count)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                last_sync_date = CURRENT_TIMESTAMP,
                message_count = sync_status.message_count + excluded.message_count
        ''', (chat_id, last_message_id, message_count))
        self.conn.commit()
    
    def get_all_chats(self) -> List[Dict[str, Any]]:
        """
        Get all chats from database.
        
        Returns:
            List of chat dictionaries
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM chats ORDER BY updated_at DESC')
        return [dict(row) for row in cursor.fetchall()]
    
    def get_messages_by_date_range(
        self, 
        chat_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Get messages within a date range (for point-in-time recovery).
        
        Args:
            chat_id: Optional chat ID to filter by
            start_date: Optional start date
            end_date: Optional end date
            
        Returns:
            List of message dictionaries
        """
        cursor = self.conn.cursor()
        query = 'SELECT * FROM messages WHERE 1=1'
        params = []
        
        if chat_id:
            query += ' AND chat_id = ?'
            params.append(chat_id)
        if start_date:
            query += ' AND date >= ?'
            params.append(start_date)
        if end_date:
            query += ' AND date <= ?'
            params.append(end_date)
        
        query += ' ORDER BY date ASC'
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    
    def find_message_by_date(self, chat_id: int, target_date: datetime) -> Optional[Dict[str, Any]]:
        """
        Find the first message on or after a specific date for a chat.
        Used for date picker navigation.
        
        Args:
            chat_id: Chat ID to search in
            target_date: Target date to find message for
            
        Returns:
            Message dictionary or None if no message found
        """
        cursor = self.conn.cursor()
        # Find first message on or after the target date
        cursor.execute('''
            SELECT * FROM messages 
            WHERE chat_id = ? AND date >= ?
            ORDER BY date ASC
            LIMIT 1
        ''', (chat_id, target_date))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get backup statistics.
        
        Returns:
            Dictionary with statistics
        """
        cursor = self.conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM chats')
        chat_count = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM messages')
        message_count = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM media WHERE downloaded = 1')
        media_count = cursor.fetchone()['count']
        
        cursor.execute('SELECT SUM(file_size) as total FROM media WHERE downloaded = 1')
        total_size = cursor.fetchone()['total'] or 0
        
        # Get last backup time - prefer metadata, fallback to most recent sync_date
        last_backup_time = self.get_metadata('last_backup_time')
        timezone_source = 'metadata'  # Track where the time came from
        
        # If no metadata, get the most recent sync_date from sync_status
        if not last_backup_time:
            cursor.execute('SELECT MAX(last_sync_date) as last_sync FROM sync_status')
            row = cursor.fetchone()
            if row and row['last_sync']:
                last_backup_time = row['last_sync']
                timezone_source = 'sync_status'  # This is in server local time, not UTC
        
        stats = {
            'chats': chat_count,
            'messages': message_count,
            'media_files': media_count,
            'total_size_mb': round(total_size / (1024 * 1024), 2)
        }
        
        if last_backup_time:
            stats['last_backup_time'] = last_backup_time
            stats['last_backup_time_source'] = timezone_source  # 'metadata' (UTC) or 'sync_status' (server local)
        
        return stats
    
    def get_messages_sync_data(self, chat_id: int) -> Dict[int, Optional[str]]:
        """
        Get message IDs and their edit dates for a chat (for sync checking).
        
        Args:
            chat_id: Chat ID to get data for
            
        Returns:
            Dictionary mapping message ID to edit_date (or None)
        """
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, edit_date FROM messages WHERE chat_id = ?', (chat_id,))
        return {row['id']: row['edit_date'] for row in cursor.fetchall()}
    
    def delete_message(self, chat_id: int, message_id: int):
        """
        Delete a specific message from the database.
        
        Args:
            chat_id: Chat ID containing the message
            message_id: Message ID to delete
        """
        cursor = self.conn.cursor()
        
        # Delete associated media
        cursor.execute('''
            DELETE FROM media 
            WHERE chat_id = ? AND message_id = ?
        ''', (chat_id, message_id))
        
        # Delete the message
        cursor.execute('''
            DELETE FROM messages 
            WHERE chat_id = ? AND id = ?
        ''', (chat_id, message_id))
        
        self.conn.commit()
        logger.debug(f"Deleted message {message_id} from chat {chat_id}")
    
    def update_message_text(self, chat_id: int, message_id: int, new_text: str, edit_date: str):
        """
        Update a message's text (for syncing edits).
        
        Args:
            chat_id: Chat ID containing the message
            message_id: Message ID to update
            new_text: New message text
            edit_date: Timestamp of the edit
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE messages 
            SET text = ?, edit_date = ?
            WHERE chat_id = ? AND id = ?
        ''', (new_text, edit_date, chat_id, message_id))
        self.conn.commit()
        logger.debug(f"Updated message {message_id} in chat {chat_id}")
    
    def get_all_chats(self):
        """Get all chats with their last message date.

        Returns:
            List of chat dictionaries sorted by last message date (newest first),
            with chats that have no messages listed last.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                c.*,
                MAX(m.date) AS last_message_date
            FROM chats c
            LEFT JOIN messages m ON c.id = m.chat_id
            GROUP BY c.id
            ORDER BY (last_message_date IS NULL) ASC, last_message_date DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]
    
    def delete_chat_and_related_data(self, chat_id: int, media_base_path: str = None):
        """
        Delete a chat and all related data from the database AND filesystem.
        
        This includes:
        - Chat record
        - All messages in the chat
        - All media references for messages in the chat
        - Sync status for the chat
        - Physical media folder (data/media/{chat_id}/)
        - Avatar files for this chat
        
        Args:
            chat_id: Chat ID to delete
            media_base_path: Base path to media directory (e.g., 'data/media')
        """
        cursor = self.conn.cursor()
        
        # Delete media records for messages in this chat
        cursor.execute('''
            DELETE FROM media 
            WHERE chat_id = ?
        ''', (chat_id,))
        
        # Delete messages
        cursor.execute('''
            DELETE FROM messages 
            WHERE chat_id = ?
        ''', (chat_id,))
        
        # Delete sync status
        cursor.execute('''
            DELETE FROM sync_status 
            WHERE chat_id = ?
        ''', (chat_id,))
        
        # Delete chat
        cursor.execute('''
            DELETE FROM chats 
            WHERE id = ?
        ''', (chat_id,))
        
        self.conn.commit()
        logger.info(f"Deleted chat {chat_id} and all related data from database")
        
        # Delete physical media files if media_base_path is provided
        if media_base_path and os.path.exists(media_base_path):
            # Delete chat media folder
            chat_media_dir = os.path.join(media_base_path, str(chat_id))
            if os.path.exists(chat_media_dir):
                try:
                    shutil.rmtree(chat_media_dir)
                    logger.info(f"Deleted media folder: {chat_media_dir}")
                except Exception as e:
                    logger.error(f"Failed to delete media folder {chat_media_dir}: {e}")
            
            # Delete avatar files for this chat
            # Avatars are stored as: avatars/chats/{chat_id}_*.jpg or avatars/users/{chat_id}_*.jpg
            for avatar_type in ['chats', 'users']:
                avatar_pattern = os.path.join(media_base_path, 'avatars', avatar_type, f'{chat_id}_*.jpg')
                avatar_files = glob.glob(avatar_pattern)
                for avatar_file in avatar_files:
                    try:
                        os.remove(avatar_file)
                        logger.info(f"Deleted avatar file: {avatar_file}")
                    except Exception as e:
                        logger.error(f"Failed to delete avatar {avatar_file}: {e}")
    
    def close(self):
        """Close database connection."""
        self.conn.close()
        logger.info("Database connection closed")


if __name__ == '__main__':
    # Test database operations
    logging.basicConfig(level=logging.DEBUG)
    db = Database('test_backup.db')
    
    # Test chat insertion
    db.upsert_chat({
        'id': 12345,
        'type': 'private',
        'first_name': 'Test',
        'last_name': 'User',
        'username': 'testuser'
    })
    
    # Get statistics
    stats = db.get_statistics()
    print(f"Statistics: {stats}")
    
    db.close()
