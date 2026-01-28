"""v6.0.0 Schema normalization - remove media duplication, add FKs.

This migration:
1. Migrates any missing media data from messages to media table
2. Removes media_type, media_id, media_path from messages (normalized to media table)
3. Adds foreign key constraints for data integrity
4. Adds performance indexes

BREAKING CHANGE: Applications must now use the media table for media metadata.

Revision ID: 005
Revises: 004
Create Date: 2026-01-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Normalize schema: move media data to media table, add FKs and indexes."""
    
    # Get connection and dialect
    conn = op.get_bind()
    dialect = conn.dialect.name
    
    # =========================================================================
    # STEP 1: Data Migration - Ensure all media data exists in media table
    # =========================================================================
    
    # Insert missing media records from messages table
    # This handles cases where messages have media_id but no corresponding media record
    # Use ON CONFLICT DO NOTHING for PostgreSQL to handle duplicate keys gracefully
    if dialect == 'postgresql':
        conn.execute(text("""
            INSERT INTO media (id, message_id, chat_id, type, file_path, downloaded, created_at)
            SELECT 
                m.media_id,
                m.id,
                m.chat_id,
                m.media_type,
                m.media_path,
                CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN true ELSE false END,
                m.created_at
            FROM messages m
            WHERE m.media_id IS NOT NULL 
              AND m.media_id != ''
            ON CONFLICT (id) DO NOTHING
        """))
    else:
        # SQLite: Use INSERT OR IGNORE
        conn.execute(text("""
            INSERT OR IGNORE INTO media (id, message_id, chat_id, type, file_path, downloaded, created_at)
            SELECT 
                m.media_id,
                m.id,
                m.chat_id,
                m.media_type,
                m.media_path,
                CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN 1 ELSE 0 END,
                m.created_at
            FROM messages m
            WHERE m.media_id IS NOT NULL 
              AND m.media_id != ''
        """))
    
    # Update existing media records that might be missing message_id/chat_id
    conn.execute(text("""
        UPDATE media
        SET message_id = (
            SELECT m.id FROM messages m WHERE m.media_id = media.id LIMIT 1
        ),
        chat_id = (
            SELECT m.chat_id FROM messages m WHERE m.media_id = media.id LIMIT 1
        )
        WHERE message_id IS NULL
          AND EXISTS (SELECT 1 FROM messages m WHERE m.media_id = media.id)
    """))
    
    # =========================================================================
    # STEP 2: Create backup table for rollback (stores dropped columns)
    # =========================================================================
    
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS _messages_media_backup AS
        SELECT id, chat_id, media_type, media_id, media_path
        FROM messages
        WHERE media_id IS NOT NULL AND media_id != ''
    """))
    
    # =========================================================================
    # STEP 3: Drop the media columns from messages table
    # =========================================================================
    
    # SQLite doesn't support DROP COLUMN directly in older versions
    # We need to handle this differently based on dialect
    if dialect == 'sqlite':
        # SQLite: Recreate table without the columns
        # First, create new table structure
        # NOTE: sender_id FK is NOT enforced because sender_id can be channel/group IDs
        # that aren't in the users table. The relationship is maintained at ORM level.
        op.execute(text("""
            CREATE TABLE messages_new (
                id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER,
                date DATETIME NOT NULL,
                text TEXT,
                reply_to_msg_id INTEGER,
                reply_to_text TEXT,
                forward_from_id INTEGER,
                edit_date DATETIME,
                raw_data TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_outgoing INTEGER DEFAULT 0 NOT NULL,
                is_pinned INTEGER DEFAULT 0 NOT NULL,
                PRIMARY KEY (id, chat_id),
                FOREIGN KEY(chat_id) REFERENCES chats (id)
            )
        """))
        
        # Copy data
        op.execute(text("""
            INSERT INTO messages_new (
                id, chat_id, sender_id, date, text, reply_to_msg_id, 
                reply_to_text, forward_from_id, edit_date, raw_data, 
                created_at, is_outgoing, is_pinned
            )
            SELECT 
                id, chat_id, sender_id, date, text, reply_to_msg_id,
                reply_to_text, forward_from_id, edit_date, raw_data,
                created_at, is_outgoing, is_pinned
            FROM messages
        """))
        
        # Drop old table and rename new
        op.execute(text("DROP TABLE messages"))
        op.execute(text("ALTER TABLE messages_new RENAME TO messages"))
        
        # Recreate indexes
        op.create_index('idx_messages_chat_id', 'messages', ['chat_id'])
        op.create_index('idx_messages_date', 'messages', ['date'])
        op.create_index('idx_messages_sender_id', 'messages', ['sender_id'])
        op.create_index('idx_messages_chat_date_desc', 'messages', ['chat_id', sa.text('date DESC')])
        op.create_index('idx_messages_chat_pinned', 'messages', ['chat_id', 'is_pinned'])
    else:
        # PostgreSQL: Direct column drops
        # NOTE: sender_id FK is NOT added because sender_id can be channel/group IDs
        # that aren't in the users table. The relationship is maintained at ORM level.
        op.drop_column('messages', 'media_type')
        op.drop_column('messages', 'media_id')
        op.drop_column('messages', 'media_path')
    
    # =========================================================================
    # STEP 4: Clean up orphan data before adding FK constraints
    # =========================================================================
    
    # Delete orphan media records (where message doesn't exist)
    conn.execute(text("""
        DELETE FROM media 
        WHERE message_id IS NOT NULL 
          AND chat_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages 
              WHERE messages.id = media.message_id 
                AND messages.chat_id = media.chat_id
          )
    """))
    
    # Set user_id to NULL for orphan reactions (where user doesn't exist)
    # This preserves the reaction counts while removing invalid FK references
    conn.execute(text("""
        UPDATE reactions 
        SET user_id = NULL
        WHERE user_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM users WHERE users.id = reactions.user_id)
    """))
    
    # =========================================================================
    # STEP 5: Add FK constraint for media -> messages
    # =========================================================================
    
    if dialect == 'sqlite':
        # SQLite: Recreate media table with FK
        op.execute(text("""
            CREATE TABLE media_new (
                id VARCHAR(255) NOT NULL PRIMARY KEY,
                message_id INTEGER,
                chat_id INTEGER,
                type VARCHAR(50),
                file_path TEXT,
                file_name VARCHAR(255),
                file_size INTEGER,
                mime_type VARCHAR(100),
                width INTEGER,
                height INTEGER,
                duration INTEGER,
                downloaded INTEGER DEFAULT 0 NOT NULL,
                download_date DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(message_id, chat_id) REFERENCES messages (id, chat_id) ON DELETE CASCADE
            )
        """))
        
        op.execute(text("""
            INSERT INTO media_new 
            SELECT * FROM media
        """))
        
        op.execute(text("DROP TABLE media"))
        op.execute(text("ALTER TABLE media_new RENAME TO media"))
        
        # Recreate media indexes
        op.create_index('idx_media_message', 'media', ['message_id', 'chat_id'])
    else:
        # PostgreSQL: Add FK directly
        op.create_foreign_key(
            'fk_media_message',
            'media', 'messages',
            ['message_id', 'chat_id'], ['id', 'chat_id'],
            ondelete='CASCADE'
        )
    
    # =========================================================================
    # STEP 6: Add FK for reactions.user_id -> users.id
    # =========================================================================
    
    if dialect != 'sqlite':
        op.create_foreign_key(
            'fk_reactions_user',
            'reactions', 'users',
            ['user_id'], ['id'],
            ondelete='SET NULL'
        )
    
    # =========================================================================
    # STEP 7: Add new performance indexes
    # =========================================================================
    
    # Index for reply lookups
    op.create_index('idx_messages_reply_to', 'messages', ['chat_id', 'reply_to_msg_id'])
    
    # Index for finding undownloaded media
    op.create_index('idx_media_downloaded', 'media', ['chat_id', 'downloaded'])
    
    # Index for filtering by media type
    op.create_index('idx_media_type', 'media', ['type'])
    
    # Index for user reaction queries
    op.create_index('idx_reactions_user', 'reactions', ['user_id'])
    
    # Index for chat username lookups
    op.create_index('idx_chats_username', 'chats', ['username'])
    
    # Index for user username lookups
    op.create_index('idx_users_username', 'users', ['username'])


def downgrade() -> None:
    """Restore media columns to messages table from backup."""
    
    conn = op.get_bind()
    dialect = conn.dialect.name
    
    # =========================================================================
    # STEP 1: Drop new indexes
    # =========================================================================
    
    op.drop_index('idx_users_username', table_name='users')
    op.drop_index('idx_chats_username', table_name='chats')
    op.drop_index('idx_reactions_user', table_name='reactions')
    op.drop_index('idx_media_type', table_name='media')
    op.drop_index('idx_media_downloaded', table_name='media')
    op.drop_index('idx_messages_reply_to', table_name='messages')
    
    # =========================================================================
    # STEP 2: Drop foreign keys (PostgreSQL only)
    # =========================================================================
    
    if dialect != 'sqlite':
        op.drop_constraint('fk_reactions_user', 'reactions', type_='foreignkey')
        op.drop_constraint('fk_media_message', 'media', type_='foreignkey')
        # NOTE: fk_messages_sender was never created (sender_id can be channel/group IDs)
    
    # =========================================================================
    # STEP 3: Restore media columns to messages
    # =========================================================================
    
    if dialect == 'sqlite':
        # SQLite: Recreate messages table with media columns
        op.execute(text("""
            CREATE TABLE messages_new (
                id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER,
                date DATETIME NOT NULL,
                text TEXT,
                reply_to_msg_id INTEGER,
                reply_to_text TEXT,
                forward_from_id INTEGER,
                edit_date DATETIME,
                media_type VARCHAR(50),
                media_id VARCHAR(255),
                media_path VARCHAR(500),
                raw_data TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_outgoing INTEGER DEFAULT 0 NOT NULL,
                is_pinned INTEGER DEFAULT 0 NOT NULL,
                PRIMARY KEY (id, chat_id),
                FOREIGN KEY(chat_id) REFERENCES chats (id)
            )
        """))
        
        op.execute(text("""
            INSERT INTO messages_new (
                id, chat_id, sender_id, date, text, reply_to_msg_id,
                reply_to_text, forward_from_id, edit_date, raw_data,
                created_at, is_outgoing, is_pinned
            )
            SELECT 
                id, chat_id, sender_id, date, text, reply_to_msg_id,
                reply_to_text, forward_from_id, edit_date, raw_data,
                created_at, is_outgoing, is_pinned
            FROM messages
        """))
        
        op.execute(text("DROP TABLE messages"))
        op.execute(text("ALTER TABLE messages_new RENAME TO messages"))
        
        # Recreate indexes
        op.create_index('idx_messages_chat_id', 'messages', ['chat_id'])
        op.create_index('idx_messages_date', 'messages', ['date'])
        op.create_index('idx_messages_sender_id', 'messages', ['sender_id'])
        op.create_index('idx_messages_chat_date_desc', 'messages', ['chat_id', sa.text('date DESC')])
        op.create_index('idx_messages_chat_pinned', 'messages', ['chat_id', 'is_pinned'])
        
        # Recreate media table without FK
        op.execute(text("""
            CREATE TABLE media_new (
                id VARCHAR(255) NOT NULL PRIMARY KEY,
                message_id INTEGER,
                chat_id INTEGER,
                type VARCHAR(50),
                file_path VARCHAR(500),
                file_name VARCHAR(255),
                file_size INTEGER,
                mime_type VARCHAR(100),
                width INTEGER,
                height INTEGER,
                duration INTEGER,
                downloaded INTEGER DEFAULT 0 NOT NULL,
                download_date DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        op.execute(text("INSERT INTO media_new SELECT * FROM media"))
        op.execute(text("DROP TABLE media"))
        op.execute(text("ALTER TABLE media_new RENAME TO media"))
        op.create_index('idx_media_message', 'media', ['message_id', 'chat_id'])
    else:
        # PostgreSQL: Add columns back
        op.add_column('messages', sa.Column('media_type', sa.String(50)))
        op.add_column('messages', sa.Column('media_id', sa.String(255)))
        op.add_column('messages', sa.Column('media_path', sa.String(500)))
    
    # =========================================================================
    # STEP 4: Restore data from backup table
    # =========================================================================
    
    conn.execute(text("""
        UPDATE messages
        SET 
            media_type = (SELECT media_type FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id),
            media_id = (SELECT media_id FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id),
            media_path = (SELECT media_path FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id)
        WHERE EXISTS (
            SELECT 1 FROM _messages_media_backup b 
            WHERE b.id = messages.id AND b.chat_id = messages.chat_id
        )
    """))
    
    # =========================================================================
    # STEP 5: Drop backup table
    # =========================================================================
    
    op.execute(text("DROP TABLE IF EXISTS _messages_media_backup"))
