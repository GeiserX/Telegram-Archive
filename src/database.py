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
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager for Telegram backup data."""
    
    def __init__(self, db_path: str, timeout: float = 30.0):
        """
        Initialize database connection and create schema if needed.
        
        Args:
            db_path: Path to SQLite database file
            timeout: Connection timeout in seconds (default 30s)
        """
        self.db_path = db_path
        # timeout: wait up to N seconds if database is locked
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=timeout)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        
        # Enable WAL mode for better concurrent read/write performance
        # WAL allows readers to proceed while a writer is active
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=30000')  # 30 second busy timeout
        
        self._create_schema()
        logger.info(f"Database initialized at {db_path} (WAL mode enabled)")
    
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message_id INTEGER,
                file_id TEXT,
                file_unique_id TEXT,
                file_name TEXT,
                file_size INTEGER,
                mime_type TEXT,
                file_path TEXT,
                downloaded INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(id),
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        ''')
        
        # Sync status table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_status (
                chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER DEFAULT 0,
                last_sync_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                messages_count INTEGER DEFAULT 0,
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
           json.dumps(message_data.get('raw_data', {}))
        ))
        self.conn.commit()
    
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
                json.dumps(m.get('raw_data', {})),
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
        
        return {
            'chats': chat_count,
            'messages': message_count,
            'media_files': media_count,
            'total_size_mb': round(total_size / (1024 * 1024), 2)
        }
    
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
