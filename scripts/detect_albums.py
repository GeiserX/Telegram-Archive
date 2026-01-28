#!/usr/bin/env python3
"""
Detect Albums Migration Script

This script detects photo/video albums in existing backups by analyzing
message timestamps and grouping consecutive media from the same sender
that were sent within a short time window.

Albums are identified by setting `grouped_id` in the message's raw_data JSON.

Usage:
    # Preview what would be detected (dry run)
    python -m scripts.detect_albums --dry-run
    
    # Apply album detection
    python -m scripts.detect_albums
    
    # Adjust time window (default: 2 seconds)
    python -m scripts.detect_albums --window 3
"""

import asyncio
import os
import sys
import argparse
import logging
import json
from datetime import timedelta
from collections import defaultdict

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


async def _bulk_update_raw_data(session, updates: list):
    """
    Bulk update raw_data for messages using a single SQL query.
    
    Args:
        session: SQLAlchemy async session  
        updates: List of (chat_id, message_id, raw_data_json_str) tuples
    """
    if not updates:
        return
    
    dialect = session.bind.dialect.name
    
    if dialect == 'postgresql':
        # PostgreSQL - UPDATE FROM VALUES
        # Escape single quotes in JSON strings
        values_str = ", ".join(
            f"({chat_id}::bigint, {msg_id}::bigint, '{raw_data.replace(chr(39), chr(39)+chr(39))}'::text)"
            for chat_id, msg_id, raw_data in updates
        )
        query = text(f"""
            UPDATE messages 
            SET raw_data = v.raw_data
            FROM (VALUES {values_str}) AS v(chat_id, id, raw_data)
            WHERE messages.chat_id = v.chat_id AND messages.id = v.id
        """)
    else:
        # SQLite - multiple CASE WHEN
        case_clauses = " ".join(
            f"WHEN chat_id = {chat_id} AND id = {msg_id} THEN '{raw_data.replace(chr(39), chr(39)+chr(39))}'"
            for chat_id, msg_id, raw_data in updates
        )
        where_clauses = " OR ".join(
            f"(chat_id = {chat_id} AND id = {msg_id})"
            for chat_id, msg_id, _ in updates
        )
        query = text(f"""
            UPDATE messages 
            SET raw_data = CASE {case_clauses} END
            WHERE {where_clauses}
        """)
    
    await session.execute(query)


async def detect_albums(dry_run: bool = False, window_seconds: int = 2):
    """
    Detect albums by grouping consecutive photos/videos from the same sender
    that were sent within a short time window.
    
    Args:
        dry_run: If True, only report what would be done
        window_seconds: Maximum seconds between messages to consider them part of same album
    """
    config = Config()
    db = await create_adapter()
    
    logger.info("=" * 70)
    logger.info("Album Detection Script - Group consecutive media")
    logger.info("=" * 70)
    logger.info(f"Dry run: {dry_run}")
    logger.info(f"Time window: {window_seconds} seconds")
    logger.info("")
    
    async with db.db_manager.async_session_factory() as session:
        from sqlalchemy import select, and_
        from src.db.models import Message, Media
        
        # Get all photo/video messages that don't have grouped_id, ordered by chat and date
        # v6.0.0: Join with Media table to get media type
        logger.info("Fetching photo/video messages without grouped_id...")
        
        result = await session.execute(
            select(Message)
            .join(Media, and_(
                Media.message_id == Message.id,
                Media.chat_id == Message.chat_id
            ))
            .where(
                Media.type.in_(['photo', 'video']),
            )
            .order_by(Message.chat_id, Message.date, Message.id)
        )
        messages = result.scalars().all()
        
        logger.info(f"Found {len(messages)} photo/video messages to analyze")
        
        # Group by chat
        by_chat = defaultdict(list)
        for msg in messages:
            by_chat[msg.chat_id].append(msg)
        
        logger.info(f"Spread across {len(by_chat)} chats")
        logger.info("")
        
        albums_detected = 0
        messages_grouped = 0
        already_grouped = 0
        pending_updates = []  # Collect all updates for bulk execution
        BATCH_SIZE = 1000
        
        for chat_idx, (chat_id, chat_messages) in enumerate(by_chat.items()):
            # Progress report every 100 chats
            if chat_idx % 100 == 0 and chat_idx > 0:
                logger.info(f"Progress: {chat_idx}/{len(by_chat)} chats, {albums_detected} albums found")
                # Flush pending updates
                if pending_updates and not dry_run:
                    await _bulk_update_raw_data(session, pending_updates)
                    await session.commit()
                    pending_updates = []
            
            # Skip chats with only 1 message
            if len(chat_messages) < 2:
                continue
            
            # Detect albums within this chat
            current_album = []
            
            for i, msg in enumerate(chat_messages):
                # Parse existing raw_data
                try:
                    raw_data = json.loads(msg.raw_data) if msg.raw_data else {}
                except:
                    raw_data = {}
                
                # Skip if already has grouped_id
                if raw_data.get('grouped_id'):
                    already_grouped += 1
                    # Finish current album if any
                    if len(current_album) >= 2:
                        albums_detected += 1
                        messages_grouped += len(current_album)
                    current_album = []
                    continue
                
                # Check if this message continues the current album
                if current_album:
                    last_msg = current_album[-1]
                    time_diff = (msg.date - last_msg.date).total_seconds() if msg.date and last_msg.date else 999
                    same_sender = msg.sender_id == last_msg.sender_id
                    
                    if same_sender and abs(time_diff) <= window_seconds:
                        # Continue album
                        current_album.append(msg)
                    else:
                        # End current album, start new potential album
                        if len(current_album) >= 2:
                            # This was an album - collect updates
                            grouped_id = current_album[0].id  # Use first message ID as group ID
                            
                            for album_msg in current_album:
                                try:
                                    existing_raw = json.loads(album_msg.raw_data) if album_msg.raw_data else {}
                                except:
                                    existing_raw = {}
                                existing_raw['grouped_id'] = grouped_id
                                existing_raw['album_detected'] = True
                                pending_updates.append((album_msg.chat_id, album_msg.id, json.dumps(existing_raw)))
                            
                            albums_detected += 1
                            messages_grouped += len(current_album)
                            
                            if albums_detected <= 10:
                                logger.info(f"  Album detected: {len(current_album)} items in chat {chat_id} (msg {grouped_id})")
                        
                        # Start new potential album
                        current_album = [msg]
                else:
                    # Start new potential album
                    current_album = [msg]
            
            # Handle last album in chat
            if len(current_album) >= 2:
                grouped_id = current_album[0].id
                
                for album_msg in current_album:
                    try:
                        existing_raw = json.loads(album_msg.raw_data) if album_msg.raw_data else {}
                    except:
                        existing_raw = {}
                    existing_raw['grouped_id'] = grouped_id
                    existing_raw['album_detected'] = True
                    pending_updates.append((album_msg.chat_id, album_msg.id, json.dumps(existing_raw)))
                
                albums_detected += 1
                messages_grouped += len(current_album)
                
                if albums_detected <= 10:
                    logger.info(f"  Album detected: {len(current_album)} items in chat {chat_id} (msg {grouped_id})")
        
        # Flush remaining updates
        if pending_updates and not dry_run:
            await _bulk_update_raw_data(session, pending_updates)
            await session.commit()
            logger.info("")
            logger.info("✅ Database changes committed")
        
        # Summary
        logger.info("")
        logger.info("=" * 70)
        logger.info("SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Albums detected:        {albums_detected}")
        logger.info(f"Messages grouped:       {messages_grouped}")
        logger.info(f"Already had grouped_id: {already_grouped}")
        logger.info(f"Avg album size:         {messages_grouped / albums_detected:.1f}" if albums_detected else "N/A")
        
        if dry_run:
            logger.info("")
            logger.info("*** DRY RUN - No changes were made ***")
            logger.info("Run without --dry-run to apply changes")


def main():
    parser = argparse.ArgumentParser(
        description='Detect photo/video albums by analyzing message timestamps'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )
    parser.add_argument(
        '--window',
        type=int,
        default=2,
        help='Maximum seconds between messages to consider them part of same album (default: 2)'
    )
    
    args = parser.parse_args()
    
    asyncio.run(detect_albums(dry_run=args.dry_run, window_seconds=args.window))


if __name__ == '__main__':
    main()
