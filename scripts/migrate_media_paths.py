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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def get_group_channel_chats(session) -> list:
    """Get all chats that are groups/channels/supergroups (have negative IDs)."""
    result = await session.execute(text("SELECT id, type, title FROM chats WHERE id < 0 ORDER BY id"))
    return [{"id": row[0], "type": row[1], "title": row[2]} for row in result.fetchall()]


async def count_paths_for_chat(session, chat_id: int, old_folder: str) -> dict:
    """Count media paths that use the old (positive) folder name in both tables."""
    pattern = f"%/media/{old_folder}/%"

    # Count in messages table
    msg_result = await session.execute(
        text("SELECT COUNT(*) FROM messages WHERE chat_id = :chat_id AND media_path LIKE :pattern"),
        {"chat_id": chat_id, "pattern": pattern},
    )
    msg_count = msg_result.scalar() or 0

    # Count in media table
    media_result = await session.execute(
        text("SELECT COUNT(*) FROM media WHERE chat_id = :chat_id AND file_path LIKE :pattern"),
        {"chat_id": chat_id, "pattern": pattern},
    )
    media_count = media_result.scalar() or 0

    return {"messages": msg_count, "media": media_count}


async def bulk_update_media_paths(session, chat_id: int, old_folder: str, new_folder: str) -> dict:
    """Bulk update all media paths for a chat using SQL REPLACE - single query per table!"""
    old_pattern = f"/media/{old_folder}/"
    new_pattern = f"/media/{new_folder}/"

    # Bulk update messages table
    msg_result = await session.execute(
        text("""
            UPDATE messages
            SET media_path = REPLACE(media_path, :old_pattern, :new_pattern)
            WHERE chat_id = :chat_id AND media_path LIKE :like_pattern
        """),
        {
            "chat_id": chat_id,
            "old_pattern": old_pattern,
            "new_pattern": new_pattern,
            "like_pattern": f"%{old_pattern}%",
        },
    )
    msg_updated = msg_result.rowcount

    # Bulk update media table
    media_result = await session.execute(
        text("""
            UPDATE media
            SET file_path = REPLACE(file_path, :old_pattern, :new_pattern)
            WHERE chat_id = :chat_id AND file_path LIKE :like_pattern
        """),
        {
            "chat_id": chat_id,
            "old_pattern": old_pattern,
            "new_pattern": new_pattern,
            "like_pattern": f"%{old_pattern}%",
        },
    )
    media_updated = media_result.rowcount

    return {"messages": msg_updated, "media": media_updated}


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
        if not filename.endswith(".jpg"):
            continue

        # Check if it starts with a positive number (no dash)
        match = re.match(r"^(\d+)_(\d+)\.jpg$", filename)
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

    # Log database info from non-sensitive env vars (avoids taint tracking)
    _db_type = os.environ.get("DB_TYPE", "sqlite").lower()
    if _db_type == "postgresql":
        _host = os.environ.get("POSTGRES_HOST", "localhost")
        _port = os.environ.get("POSTGRES_PORT", "5432")
        _db = os.environ.get("POSTGRES_DB", "telegram_backup")
        logger.info(f"Database: postgresql://*:***@{_host}:{_port}/{_db}")
    else:
        logger.info(f"Database: {_db_type}")
    logger.info(f"Media path: {media_path}")
    logger.info("")

    # Create async engine
    engine = create_async_engine(db_url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    stats = {"chats_processed": 0, "folders_renamed": 0, "paths_updated": 0, "avatars_renamed": 0, "errors": 0}

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

            # Count paths that need updating
            counts = await count_paths_for_chat(session, chat_id, old_folder)

            if counts["messages"] > 0 or counts["media"] > 0:
                logger.info(f"   Found {counts['messages']} message + {counts['media']} media paths to update")

                if dry_run:
                    logger.debug("   [DRY RUN] Would bulk update paths")
                    stats["paths_updated"] += counts["messages"] + counts["media"]
                else:
                    # BULK UPDATE - single query per table instead of one per row!
                    updated = await bulk_update_media_paths(session, chat_id, old_folder, new_folder)
                    stats["paths_updated"] += updated["messages"] + updated["media"]
                    logger.info(f"   ✓ Updated {updated['messages']} message + {updated['media']} media paths")

            # Rename the folder
            if os.path.exists(new_folder_path):
                # New folder already exists - merge contents
                logger.info(f"   ⚠️  Both folders exist, merging {old_folder}/ into {new_folder}/")

                if not dry_run:
                    for item in os.listdir(old_folder_path):
                        src = os.path.join(old_folder_path, item)
                        dst = os.path.join(new_folder_path, item)
                        if not os.path.exists(dst):
                            # File doesn't exist in destination - move it
                            shutil.move(src, dst)
                        else:
                            # File exists in both - delete from old folder (keep new)
                            os.remove(src)
                    # Remove old folder (should be empty now)
                    try:
                        os.rmdir(old_folder_path)
                        logger.info(f"   ✓ Removed empty folder: {old_folder}/")
                    except OSError:
                        logger.warning(f"   Could not remove folder {old_folder}/ (not empty?)")

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


def get_database_url() -> str:
    """Build database URL from environment variables (same logic as Config)."""
    # Check for explicit DATABASE_URL first
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    # Otherwise build from DB_TYPE and components
    db_type = os.environ.get("DB_TYPE", "sqlite").lower()

    if db_type == "postgresql":
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "telegram_backup")
        user = os.environ.get("POSTGRES_USER", "telegram")
        password = os.environ.get("POSTGRES_PASSWORD", "telegram")
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"
    else:
        # SQLite
        backup_path = os.environ.get("BACKUP_PATH", "/data/backups")
        return f"sqlite:///{backup_path}/telegram_backup.db"


def main():
    parser = argparse.ArgumentParser(
        description="Migrate media paths to use negative IDs for groups/channels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying them")
    parser.add_argument(
        "--media-path",
        default=os.environ.get("MEDIA_PATH", "/data/backups/media"),
        help="Path to media directory (default: $MEDIA_PATH or /data/backups/media)",
    )
    parser.add_argument(
        "--db-url", default=None, help="Database URL (default: built from env vars like DB_TYPE, POSTGRES_*)"
    )

    args = parser.parse_args()

    # Get database URL
    db_url = args.db_url if args.db_url else get_database_url()

    # Convert sync DB URL to async if needed
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
    elif db_url.startswith("sqlite://"):
        db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://")

    asyncio.run(migrate(db_url, args.media_path, args.dry_run))


if __name__ == "__main__":
    main()
