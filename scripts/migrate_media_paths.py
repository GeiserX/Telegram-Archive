#!/usr/bin/env python3
"""
Migration Script: Normalize media folder paths to use marked IDs (negative for groups/channels)

This script migrates media folders and database paths from the old format (positive IDs)
to the new consistent format (negative IDs for groups/channels/supergroups).

WHAT IT DOES:
1. Finds all chats that are groups/channels/supergroups (negative IDs in DB)
2. Checks if media exists in old-style folder (positive ID, e.g., "35258041")
3. Renames folder to new-style (negative ID, e.g., "-35258041")
4. Updates media_path in database to match

WHEN TO RUN:
- Required when upgrading to v5.0.0 if you have existing data
- Safe to run multiple times (idempotent)

USAGE:
    # Dry run (preview changes):
    python scripts/migrate_media_paths.py --dry-run

    # Actually migrate:
    python scripts/migrate_media_paths.py

    # With custom paths:
    python scripts/migrate_media_paths.py --media-path /path/to/media --db-url postgresql://...

BACKUP FIRST!
    - Backup your media folder
    - Backup your database
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def get_group_channel_chats(session) -> list:
    """Get all chats that are groups/channels/supergroups (have negative IDs)."""
    result = await session.execute(
        text("SELECT id, type, title FROM chats WHERE id < 0 ORDER BY id")
    )
    return [{"id": row[0], "type": row[1], "title": row[2]} for row in result.fetchall()]


async def get_media_paths_for_chat(session, chat_id: int, old_folder: str) -> list:
    """Get all media paths that use the old (positive) folder name."""
    # Look for paths containing the positive folder name
    pattern = f"%/media/{old_folder}/%"
    result = await session.execute(
        text("SELECT id, media_path FROM messages WHERE chat_id = :chat_id AND media_path LIKE :pattern"),
        {"chat_id": chat_id, "pattern": pattern}
    )
    return [{"id": row[0], "media_path": row[1]} for row in result.fetchall()]


async def update_media_path(session, message_id: int, old_path: str, new_path: str):
    """Update a single media_path in the database."""
    await session.execute(
        text("UPDATE messages SET media_path = :new_path WHERE id = :id AND media_path = :old_path"),
        {"id": message_id, "new_path": new_path, "old_path": old_path}
    )


async def migrate_avatars(media_path: str, dry_run: bool) -> dict:
    """Migrate avatar files from positive to negative IDs."""
    stats = {"renamed": 0, "skipped": 0, "errors": 0}
    
    chats_avatar_dir = os.path.join(media_path, "avatars", "chats")
    if not os.path.exists(chats_avatar_dir):
        logger.info("No avatars/chats directory found, skipping avatar migration")
        return stats
    
    # Pattern: positive_id_photoid.jpg (e.g., 11482744_49777919797605248.jpg)
    # We need to rename to: -11482744_49777919797605248.jpg
    for filename in os.listdir(chats_avatar_dir):
        if not filename.endswith('.jpg'):
            continue
        
        # Check if it starts with a positive number (no dash)
        match = re.match(r'^(\d+)_(\d+)\.jpg$', filename)
        if not match:
            continue  # Already negative or different format
        
        old_id = match.group(1)
        photo_id = match.group(2)
        new_filename = f"-{old_id}_{photo_id}.jpg"
        
        old_path = os.path.join(chats_avatar_dir, filename)
        new_path = os.path.join(chats_avatar_dir, new_filename)
        
        if os.path.exists(new_path):
            logger.debug(f"  Avatar already migrated: {filename}")
            stats["skipped"] += 1
            continue
        
        if dry_run:
            logger.info(f"  [DRY RUN] Would rename avatar: {filename} → {new_filename}")
            stats["renamed"] += 1
        else:
            try:
                shutil.move(old_path, new_path)
                logger.info(f"  Renamed avatar: {filename} → {new_filename}")
                stats["renamed"] += 1
            except Exception as e:
                logger.error(f"  Error renaming avatar {filename}: {e}")
                stats["errors"] += 1
    
    return stats


async def migrate(db_url: str, media_path: str, dry_run: bool = True):
    """Main migration function."""
    
    logger.info("=" * 70)
    logger.info("Media Path Migration Script - Normalize to Negative IDs")
    logger.info("=" * 70)
    
    if dry_run:
        logger.info("🔍 DRY RUN MODE - No changes will be made")
    else:
        logger.warning("⚠️  LIVE MODE - Changes will be applied!")
    
    logger.info(f"Database: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    logger.info(f"Media path: {media_path}")
    logger.info("")
    
    # Create async engine
    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    stats = {
        "chats_processed": 0,
        "folders_renamed": 0,
        "paths_updated": 0,
        "avatars_renamed": 0,
        "errors": 0
    }
    
    async with async_session() as session:
        # Get all group/channel/supergroup chats
        chats = await get_group_channel_chats(session)
        logger.info(f"Found {len(chats)} groups/channels/supergroups to check")
        logger.info("")
        
        for chat in chats:
            chat_id = chat["id"]  # Negative (e.g., -35258041)
            old_folder = str(abs(chat_id))  # Positive (e.g., "35258041")
            new_folder = str(chat_id)  # Negative (e.g., "-35258041")
            
            old_folder_path = os.path.join(media_path, old_folder)
            new_folder_path = os.path.join(media_path, new_folder)
            
            # Check if old folder exists
            if not os.path.exists(old_folder_path):
                continue  # Nothing to migrate for this chat
            
            stats["chats_processed"] += 1
            logger.info(f"📁 Chat {chat_id} ({chat['type']}): {chat['title']}")
            
            # Check for paths that need updating in DB
            messages_to_update = await get_media_paths_for_chat(session, chat_id, old_folder)
            
            if messages_to_update:
                logger.info(f"   Found {len(messages_to_update)} media paths to update in database")
                
                for msg in messages_to_update:
                    old_path = msg["media_path"]
                    new_path = old_path.replace(f"/media/{old_folder}/", f"/media/{new_folder}/")
                    
                    if dry_run:
                        logger.debug(f"   [DRY RUN] Would update: {old_path} → {new_path}")
                    else:
                        await update_media_path(session, msg["id"], old_path, new_path)
                    
                    stats["paths_updated"] += 1
            
            # Rename the folder
            if os.path.exists(new_folder_path):
                # New folder already exists - merge contents
                logger.info(f"   ⚠️  Both folders exist, merging {old_folder}/ into {new_folder}/")
                
                if not dry_run:
                    for item in os.listdir(old_folder_path):
                        src = os.path.join(old_folder_path, item)
                        dst = os.path.join(new_folder_path, item)
                        if not os.path.exists(dst):
                            shutil.move(src, dst)
                    # Remove old folder if empty
                    if not os.listdir(old_folder_path):
                        os.rmdir(old_folder_path)
                
                stats["folders_renamed"] += 1
            else:
                # Simple rename
                if dry_run:
                    logger.info(f"   [DRY RUN] Would rename folder: {old_folder}/ → {new_folder}/")
                else:
                    shutil.move(old_folder_path, new_folder_path)
                    logger.info(f"   Renamed folder: {old_folder}/ → {new_folder}/")
                
                stats["folders_renamed"] += 1
        
        # Migrate avatars
        logger.info("")
        logger.info("📷 Migrating avatars...")
        avatar_stats = await migrate_avatars(media_path, dry_run)
        stats["avatars_renamed"] = avatar_stats["renamed"]
        stats["errors"] += avatar_stats["errors"]
        
        # Commit database changes
        if not dry_run:
            await session.commit()
            logger.info("")
            logger.info("✅ Database changes committed")
    
    await engine.dispose()
    
    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("MIGRATION SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Chats processed:    {stats['chats_processed']}")
    logger.info(f"Folders renamed:    {stats['folders_renamed']}")
    logger.info(f"DB paths updated:   {stats['paths_updated']}")
    logger.info(f"Avatars renamed:    {stats['avatars_renamed']}")
    logger.info(f"Errors:             {stats['errors']}")
    
    if dry_run:
        logger.info("")
        logger.info("🔍 This was a DRY RUN. To apply changes, run without --dry-run")
    else:
        logger.info("")
        logger.info("✅ Migration complete!")
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Migrate media paths to use negative IDs for groups/channels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them"
    )
    parser.add_argument(
        "--media-path",
        default=os.environ.get("MEDIA_PATH", "/data/backups/media"),
        help="Path to media directory (default: $MEDIA_PATH or /data/backups/media)"
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data/telegram_backup.db"),
        help="Database URL (default: $DATABASE_URL)"
    )
    
    args = parser.parse_args()
    
    # Convert sync DB URL to async if needed
    db_url = args.db_url
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
    elif db_url.startswith("sqlite://"):
        db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://")
    
    asyncio.run(migrate(db_url, args.media_path, args.dry_run))


if __name__ == "__main__":
    main()
