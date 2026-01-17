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

from src.config import Config
from src.db import create_adapter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def update_media_sizes(dry_run: bool = False, force: bool = False):
    """
    Update file sizes for media records in the database.
    
    Args:
        dry_run: If True, only report what would be done without making changes
        force: If True, update all records including those with existing sizes
    """
    config = Config()
    db = await create_adapter(config.database_url if hasattr(config, 'database_url') else None, config)
    
    media_base_path = config.media_path
    if not os.path.exists(media_base_path):
        logger.error(f"Media path does not exist: {media_base_path}")
        return
    
    logger.info(f"Media path: {media_base_path}")
    logger.info(f"Dry run: {dry_run}")
    logger.info(f"Force update all: {force}")
    
    # Get media records that need updating
    async with db.db_manager.async_session_factory() as session:
        from sqlalchemy import select, update
        from src.db.models import Media
        
        # Build query - either all records or just those without sizes
        if force:
            query = select(Media)
            logger.info("Fetching ALL media records...")
        else:
            query = select(Media).where(
                (Media.file_size == None) | (Media.file_size == 0)
            )
            logger.info("Fetching media records with missing file sizes...")
        
        result = await session.execute(query)
        media_records = result.scalars().all()
        
        logger.info(f"Found {len(media_records)} records to process")
        
        updated_count = 0
        missing_count = 0
        error_count = 0
        total_size_added = 0
        
        for i, media in enumerate(media_records):
            if i % 1000 == 0 and i > 0:
                logger.info(f"Progress: {i}/{len(media_records)} ({updated_count} updated, {missing_count} missing)")
            
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
                    
                    if not dry_run:
                        # Update the record
                        await session.execute(
                            update(Media)
                            .where(Media.id == media.id)
                            .values(file_size=file_size)
                        )
                    
                    updated_count += 1
                    total_size_added += file_size
                    
                except Exception as e:
                    logger.error(f"Error processing {full_path}: {e}")
                    error_count += 1
            else:
                missing_count += 1
                if missing_count <= 10:  # Only log first 10 missing files
                    logger.warning(f"File not found: {full_path}")
        
        if not dry_run:
            await session.commit()
            logger.info("Changes committed to database")
        
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
