"""
Database operations for Telegram Backup Automation.
Manages SQLite database schema and CRUD operations for messages, chats, and media.
"""

import sqlite3
import logging
import json
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager for Telegram backup data."""
    
    def __init__(self, db_path: str):
        """
        Initialize database connection and create schema if needed.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        self._create_schema()
        logger.info(f"Database initialized at {db_path}")
    
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
        
        # Media files table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY,
                message_id INTEGER,
                chat_id INTEGER,
                type TEXT NOT NULL,
                file_name TEXT,
                file_path TEXT,
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
        
        # Sync status table (track backup progress)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_status (
                chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                last_sync_date TIMESTAMP NOT NULL,
                message_count INTEGER DEFAULT 0,
                FOREIGN KEY (chat_id) REFERENCES chats(id)
            )
        ''')
        
        # Create indexes for better query performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_media_message ON media(message_id, chat_id)')
        
        self.conn.commit()
        logger.debug("Database schema created/verified")
    
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
                media_path, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(m.get('raw_data', {}))
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
    
    def get_all_chats(self):
        """
        Get all chats with their last message date.
        
        Returns:
            List of chat dictionaries sorted by last message date (newest first)
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                c.*,
                MAX(m.date) as last_message_date
            FROM chats c
            LEFT JOIN messages m ON c.id = m.chat_id
            GROUP BY c.id
            ORDER BY last_message_date DESC NULLS LAST
        ''')
        return [dict(row) for row in cursor.fetchall()]
    
    def delete_chat_and_related_data(self, chat_id: int):
        """
        Delete a chat and all related data from the database.
        
        This includes:
        - Chat record
        - All messages in the chat
        - All media references for messages in the chat
        - Sync status for the chat
        
        Args:
            chat_id: Chat ID to delete
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
