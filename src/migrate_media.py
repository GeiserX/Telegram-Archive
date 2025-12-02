"""
Media file migration script.
Renames existing media files to include message_id prefix for uniqueness.
Updates database media_path accordingly.
"""

import os
import sqlite3
import logging
from pathlib import Path
from typing import List, Tuple

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_database_path() -> str:
    """Get database path from environment or use default."""
    backup_path = os.getenv('BACKUP_PATH', '/data/backups')
    return os.path.join(backup_path, 'telegram_backup.db')


def get_media_path() -> str:
    """Get media path from environment or use default."""
    backup_path = os.getenv('BACKUP_PATH', '/data/backups')
    return os.path.join(backup_path, 'media')


def migrate_media_files(db_path: str, media_path: str, dry_run: bool = False):
    """
    Migrate media files to new naming convention.
    
    Args:
        db_path: Path to SQLite database
        media_path: Path to media directory
        dry_run: If True, only simulate changes without modifying files
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all messages with media
    cursor.execute('''
        SELECT 
            m.id as message_id,
            m.chat_id,
            m.media_path,
            m.media_type
        FROM messages m
        WHERE m.media_path IS NOT NULL
        ORDER BY m.chat_id, m.id
    ''')
    
    messages = cursor.fetchall()
    total = len(messages)
    logger.info(f"Found {total} messages with media")
    
    migrated = 0
    skipped = 0
    errors = 0
    
    for idx, msg in enumerate(messages, 1):
        message_id = msg['message_id']
        chat_id = msg['chat_id']
        old_path = msg['media_path']
        media_type = msg['media_type']
        
        if idx % 100 == 0:
            logger.info(f"Progress: {idx}/{total} ({(idx/total)*100:.1f}%)")
        
        # Check if file exists
        if not os.path.exists(old_path):
            logger.debug(f"File not found (already deleted or never downloaded): {old_path}")
            skipped += 1
            continue
        
        # Extract filename from path
        old_filename = os.path.basename(old_path)
        
        # Check if already migrated (starts with message_id_)
        if old_filename.startswith(f"{message_id}_"):
            logger.debug(f"Already migrated: {old_filename}")
            skipped += 1
            continue
        
        # Generate new filename: {message_id}_{original_filename}
        new_filename = f"{message_id}_{old_filename}"
        new_path = os.path.join(os.path.dirname(old_path), new_filename)
        
        # Rename file
        try:
            if dry_run:
                logger.info(f"[DRY RUN] Would rename: {old_filename} -> {new_filename}")
            else:
                os.rename(old_path, new_path)
                
                # Update database
                cursor.execute('''
                    UPDATE messages 
                    SET media_path = ? 
                    WHERE id = ? AND chat_id = ?
                ''', (new_path, message_id, chat_id))
                
                logger.debug(f"Migrated: {old_filename} -> {new_filename}")
            
            migrated += 1
            
        except Exception as e:
            logger.error(f"Error migrating {old_filename}: {e}")
            errors += 1
    
    if not dry_run:
        conn.commit()
    
    conn.close()
    
    # Summary
    logger.info("=" * 60)
    logger.info("Migration Summary:")
    logger.info(f"  Total messages with media: {total}")
    logger.info(f"  Migrated: {migrated}")
    logger.info(f"  Skipped (already migrated or missing): {skipped}")
    logger.info(f"  Errors: {errors}")
    logger.info("=" * 60)
    
    if dry_run:
        logger.info("DRY RUN COMPLETE - No changes were made")
        logger.info("Run without --dry-run to apply changes")
    else:
        logger.info("Migration complete!")


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Migrate media files to new naming convention')
    parser.add_argument('--dry-run', action='store_true', help='Simulate migration without making changes')
    parser.add_argument('--db-path', help='Path to database file (overrides BACKUP_PATH env)')
    parser.add_argument('--media-path', help='Path to media directory (overrides BACKUP_PATH env)')
    
    args = parser.parse_args()
    
    db_path = args.db_path or get_database_path()
    media_path = args.media_path or get_media_path()
    
    logger.info(f"Database: {db_path}")
    logger.info(f"Media path: {media_path}")
    
    if not os.path.exists(db_path):
        logger.error(f"Database not found: {db_path}")
        exit(1)
    
    if not os.path.exists(media_path):
        logger.error(f"Media directory not found: {media_path}")
        exit(1)
    
    if args.dry_run:
        logger.info("Running in DRY RUN mode - no changes will be made")
    
    migrate_media_files(db_path, media_path, dry_run=args.dry_run)
