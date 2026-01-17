#!/usr/bin/env python3
"""
Update Media File Sizes Script

This script scans the media directory and updates the file_size column in the
database for all media records where file_size is NULL or 0.

Useful for backups created with older versions that didn't record file sizes.

Usage:
    # Dry run (see what would be updated)
    python -m scripts.update_media_sizes --dry-run
    
    # Actually update the database
    python -m scripts.update_media_sizes
    
    # Update all records, even those with existing sizes
    python -m scripts.update_media_sizes --force
"""

import asyncio
import os
import sys
import argparse
import logging

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.config import Config
from src.db import create_adapter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def _bulk_update_sizes(session, updates: list):
    """
    Bulk update file sizes using a single SQL query with CASE statement.
    Much faster than individual UPDATEs - does 1000 rows in one round-trip!
    
    Args:
        session: SQLAlchemy async session
        updates: List of (media_id, file_size) tuples
    """
    if not updates:
        return
    
    # Build a VALUES clause and UPDATE with JOIN
    # PostgreSQL: UPDATE media SET file_size = v.size FROM (VALUES ...) AS v(id, size) WHERE media.id = v.id
    # SQLite: Use CASE WHEN approach
    
    # Detect database type from connection
    dialect = session.bind.dialect.name
    
    if dialect == 'postgresql':
        # PostgreSQL - use UPDATE FROM VALUES (fastest)
        values_str = ", ".join(f"('{mid}', {size})" for mid, size in updates)
        query = text(f"""
            UPDATE media 
            SET file_size = v.size::bigint
            FROM (VALUES {values_str}) AS v(id, size)
            WHERE media.id = v.id
        """)
    else:
        # SQLite - use CASE WHEN
        case_clauses = " ".join(f"WHEN '{mid}' THEN {size}" for mid, size in updates)
        ids = ", ".join(f"'{mid}'" for mid, _ in updates)
        query = text(f"""
            UPDATE media 
            SET file_size = CASE id {case_clauses} END
            WHERE id IN ({ids})
        """)
    
    await session.execute(query)


async def update_media_sizes(dry_run: bool = False, force: bool = False):
    """
    Update file sizes for media records in the database.
    
    Args:
        dry_run: If True, only report what would be done without making changes
        force: If True, update all records including those with existing sizes
    """
    config = Config()
    db = await create_adapter()
    
    media_base_path = config.media_path
    if not os.path.exists(media_base_path):
        logger.error(f"Media path does not exist: {media_base_path}")
        return
    
    logger.info(f"Media path: {media_base_path}")
    logger.info(f"Dry run: {dry_run}")
    logger.info(f"Force update all: {force}")
    
    # Get media records that need updating
    async with db.db_manager.async_session_factory() as session:
        from sqlalchemy import select, update, not_
        from src.db.models import Media
        
        # Media types that have actual downloadable files
        DOWNLOADABLE_TYPES = ['photo', 'video', 'audio', 'voice', 'document', 'sticker', 'animation']
        # Types without files (just metadata): geo, poll, contact, venue, etc.
        
        # Build query - only downloadable types, exclude non-file types
        base_filter = Media.type.in_(DOWNLOADABLE_TYPES)
        
        if force:
            query = select(Media).where(base_filter)
            logger.info("Fetching ALL downloadable media records...")
        else:
            query = select(Media).where(
                base_filter,
                (Media.file_size == None) | (Media.file_size == 0)
            )
            logger.info("Fetching downloadable media records with missing file sizes...")
        
        logger.info(f"(Skipping non-file types: geo, poll, contact, venue, etc.)")
        
        result = await session.execute(query)
        media_records = result.scalars().all()
        
        logger.info(f"Found {len(media_records)} records to process")
        
        updated_count = 0
        missing_count = 0
        error_count = 0
        total_size_added = 0
        
        BATCH_SIZE = 1000
        pending_updates = []  # Collect updates for bulk execution
        
        for i, media in enumerate(media_records):
            # Flush batch and report progress
            if i % BATCH_SIZE == 0 and i > 0:
                if pending_updates and not dry_run:
                    # BULK UPDATE using VALUES - single query for entire batch!
                    await _bulk_update_sizes(session, pending_updates)
                    await session.commit()
                    pending_updates = []
                logger.info(f"Progress: {i}/{len(media_records)} ({updated_count} updated, {missing_count} missing) - committed")
            
            # Construct full path
            if media.file_path:
                # file_path might be absolute or relative
                if media.file_path.startswith('/'):
                    full_path = media.file_path
                else:
                    full_path = os.path.join(media_base_path, media.file_path)
            else:
                # Fallback: construct from chat_id and file_name
                if media.chat_id and media.file_name:
                    full_path = os.path.join(media_base_path, str(media.chat_id), media.file_name)
                else:
                    error_count += 1
                    continue
            
            # Check if file exists and get size
            if os.path.exists(full_path):
                try:
                    file_size = os.path.getsize(full_path)
                    pending_updates.append((media.id, file_size))
                    updated_count += 1
                    total_size_added += file_size
                except Exception as e:
                    logger.error(f"Error processing {full_path}: {e}")
                    error_count += 1
            else:
                missing_count += 1
                if missing_count <= 10:  # Only log first 10 missing files
                    logger.warning(f"File not found: {full_path}")
        
        # Flush remaining updates
        if pending_updates and not dry_run:
            await _bulk_update_sizes(session, pending_updates)
            await session.commit()
            logger.info("Final batch committed to database")
        
        # Summary
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total records processed: {len(media_records)}")
        logger.info(f"Updated: {updated_count}")
        logger.info(f"Missing files: {missing_count}")
        logger.info(f"Errors: {error_count}")
        logger.info(f"Total size added: {total_size_added / (1024*1024*1024):.2f} GB")
        
        if dry_run:
            logger.info("\n*** DRY RUN - No changes were made ***")
            logger.info("Run without --dry-run to apply changes")


def main():
    parser = argparse.ArgumentParser(
        description='Update file sizes for media records in the database'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Update all records, even those with existing sizes'
    )
    
    args = parser.parse_args()
    
    asyncio.run(update_media_sizes(dry_run=args.dry_run, force=args.force))


if __name__ == '__main__':
    main()
