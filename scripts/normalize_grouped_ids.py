#!/usr/bin/env python3
"""
Migration script to normalize grouped_id values in raw_data.

Problem: grouped_id was stored as integer in old detect_albums.py runs,
but as string in new listener album handler. JavaScript === comparison
fails between "123" and 123.

Solution: Convert all grouped_id values to strings for consistent comparison.

Usage:
    python scripts/normalize_grouped_ids.py [--dry-run]
"""

import asyncio
import json
import logging
import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import DatabaseAdapter, create_adapter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def normalize_grouped_ids(dry_run: bool = False):
    """
    Normalize all grouped_id values to strings.
    
    Args:
        dry_run: If True, only report what would be changed without making changes.
    """
    db = await create_adapter()
    
    try:
        # Find all messages with integer grouped_id
        # We detect this by checking if grouped_id exists and is not a string
        query = """
            SELECT id, chat_id, raw_data 
            FROM messages 
            WHERE raw_data IS NOT NULL 
            AND raw_data::text LIKE '%grouped_id%'
            AND raw_data::text NOT LIKE '%"grouped_id": "%'
        """
        
        async with db.db_manager.async_session_factory() as session:
            from sqlalchemy import text
            result = await session.execute(text(query))
            rows = result.fetchall()
        
        if not rows:
            logger.info("✅ No messages with integer grouped_id found. Database is already normalized.")
            return
        
        logger.info(f"Found {len(rows)} messages with integer grouped_id")
        
        if dry_run:
            logger.info("DRY RUN - No changes will be made")
            for row in rows[:10]:  # Show first 10 examples
                msg_id, chat_id, raw_data = row
                if isinstance(raw_data, str):
                    raw_data = json.loads(raw_data)
                logger.info(f"  Message {msg_id} in chat {chat_id}: grouped_id = {raw_data.get('grouped_id')} (type: {type(raw_data.get('grouped_id')).__name__})")
            if len(rows) > 10:
                logger.info(f"  ... and {len(rows) - 10} more")
            return
        
        # Update each message
        updated = 0
        errors = 0
        
        async with db.db_manager.async_session_factory() as session:
            from sqlalchemy import text
            
            for row in rows:
                msg_id, chat_id, raw_data = row
                
                try:
                    # Parse raw_data if it's a string
                    if isinstance(raw_data, str):
                        raw_data = json.loads(raw_data)
                    
                    # Convert grouped_id to string
                    if 'grouped_id' in raw_data and not isinstance(raw_data['grouped_id'], str):
                        raw_data['grouped_id'] = str(raw_data['grouped_id'])
                        
                        # Update the message
                        update_query = text("""
                            UPDATE messages 
                            SET raw_data = :raw_data 
                            WHERE id = :msg_id AND chat_id = :chat_id
                        """)
                        await session.execute(update_query, {
                            'raw_data': json.dumps(raw_data),
                            'msg_id': msg_id,
                            'chat_id': chat_id
                        })
                        updated += 1
                        
                        if updated % 1000 == 0:
                            logger.info(f"  Updated {updated}/{len(rows)} messages...")
                            await session.commit()
                            
                except Exception as e:
                    errors += 1
                    logger.warning(f"Error updating message {msg_id}: {e}")
            
            await session.commit()
        
        logger.info(f"✅ Normalized {updated} messages, {errors} errors")
        
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description='Normalize grouped_id values to strings')
    parser.add_argument('--dry-run', action='store_true', help='Only report changes, do not modify database')
    args = parser.parse_args()
    
    asyncio.run(normalize_grouped_ids(dry_run=args.dry_run))


if __name__ == '__main__':
    main()
