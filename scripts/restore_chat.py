#!/usr/bin/env python3
"""
Restore chat history from backup to Telegram.

⚠️  USE WITH CAUTION - This will send potentially thousands of messages!

IMPORTANT LIMITATIONS:
- Messages will be sent as YOU (the logged-in user), not the original sender
- Original timestamps are shown in message text, not as actual message time
- Media is re-uploaded as new files
- Telegram rate limits apply (~30 messages/minute for safety)

This is a Telegram API limitation - there is no way to send messages
as another user or with custom timestamps.

Usage:
    # Restore to the same chat (most common use case)
    python scripts/restore_chat.py --chat -1001234567890

    # Restore to a different destination
    python scripts/restore_chat.py --source-chat -1001234567890 --dest-chat -1009876543210

    # Dry run (show what would be sent without actually sending)
    python scripts/restore_chat.py --chat -1001234567890 --dry-run

    # Restore only messages after a certain date
    python scripts/restore_chat.py --chat -1001234567890 --after 2024-01-01

    # Restore only messages before a certain date
    python scripts/restore_chat.py --chat -1001234567890 --before 2024-06-01

    # Limit number of messages
    python scripts/restore_chat.py --chat -1001234567890 --limit 100
    
    # Skip media (text only)
    python scripts/restore_chat.py --chat -1001234567890 --no-media

Environment variables (same as backup container):
    DB_TYPE, POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
    or DB_PATH for SQLite

    TELEGRAM_API_ID, TELEGRAM_API_HASH, SESSION_NAME (or SESSION_PATH)
    BACKUP_PATH (for media files, default: /data/backups)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional, List, Dict, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SlowModeWaitError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def get_db_adapter():
    """Initialize and return database adapter."""
    from src.db import init_database, DatabaseAdapter
    db_manager = await init_database()
    return DatabaseAdapter(db_manager)


async def get_telegram_client():
    """Initialize and return Telegram client."""
    api_id = os.getenv('TELEGRAM_API_ID')
    api_hash = os.getenv('TELEGRAM_API_HASH')
    
    if not api_id or not api_hash:
        raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables required")
    
    session_path = os.getenv('SESSION_PATH')
    if not session_path:
        session_name = os.getenv('SESSION_NAME', 'telegram_backup')
        session_dir = os.getenv('SESSION_DIR', '/data/session')
        session_path = os.path.join(session_dir, session_name)
    
    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()
    
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session not authorized. Run the main backup first to authenticate.")
    
    return client


def format_message_header(sender_name: str, date: datetime) -> str:
    """Format the message header with sender and timestamp."""
    date_str = date.strftime('%Y-%m-%d %H:%M') if date else 'Unknown date'
    return f"[{sender_name} - {date_str}]"


def parse_msg_date(msg: Dict[str, Any]) -> Optional[datetime]:
    """Parse message date from various formats."""
    d = msg.get('date')
    if d is None:
        return None
    if isinstance(d, datetime):
        return d
    if isinstance(d, str):
        try:
            return datetime.fromisoformat(d.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None


async def restore_chat(
    source_chat_id: int,
    dest_chat_id: int,
    dry_run: bool = False,
    after_date: Optional[datetime] = None,
    before_date: Optional[datetime] = None,
    limit: Optional[int] = None,
    delay: float = 2.0,
    include_media: bool = True
):
    """
    Restore messages from backup to Telegram chat.
    
    Args:
        source_chat_id: Chat ID to read messages from (in backup DB)
        dest_chat_id: Chat ID to send messages to
        dry_run: If True, only show what would be sent
        after_date: Only restore messages after this date
        before_date: Only restore messages before this date
        limit: Maximum number of messages to restore
        delay: Seconds to wait between messages (rate limiting)
        include_media: If True, also upload media files
    """
    logger.info("=" * 60)
    logger.info("TELEGRAM CHAT RESTORE")
    logger.info("=" * 60)
    logger.warning("⚠️  Messages will be sent as YOU, not the original sender!")
    logger.warning("⚠️  Original timestamps shown in text only.")
    logger.info(f"📎 Media: {'Included' if include_media else 'Skipped (text only)'}")
    if dry_run:
        logger.info("🔍 DRY RUN MODE - No messages will actually be sent")
    logger.info("=" * 60)
    
    # Initialize database
    logger.info("Connecting to database...")
    db = await get_db_adapter()
    
    # Get chat info
    chat = await db.get_chat_by_id(source_chat_id)
    if not chat:
        logger.error(f"Chat {source_chat_id} not found in backup database!")
        return
    
    chat_name = chat.get('title') or chat.get('first_name') or f"Chat {source_chat_id}"
    logger.info(f"Source chat: {chat_name} (ID: {source_chat_id})")
    logger.info(f"Destination: {'Same chat' if source_chat_id == dest_chat_id else dest_chat_id}")
    
    # Get messages from backup (with media info)
    logger.info("Loading messages from backup...")
    messages = []
    async for msg in db.get_messages_for_export(source_chat_id, include_media=True):
        messages.append(msg)
    
    if not messages:
        logger.warning("No messages found in backup for this chat!")
        return
    
    logger.info(f"Found {len(messages)} messages in backup")
    
    # Filter by date
    if after_date:
        messages = [m for m in messages if parse_msg_date(m) and parse_msg_date(m).replace(tzinfo=None) > after_date]
        logger.info(f"After date filter: {len(messages)} messages remaining")
    
    if before_date:
        messages = [m for m in messages if parse_msg_date(m) and parse_msg_date(m).replace(tzinfo=None) < before_date]
        logger.info(f"Before date filter: {len(messages)} messages remaining")
    
    # Sort by date (oldest first for chronological restore)
    messages.sort(key=lambda m: parse_msg_date(m) or datetime.min)
    
    # Apply limit
    if limit and len(messages) > limit:
        messages = messages[:limit]
        logger.info(f"Limited to {limit} messages")
    
    if not messages:
        logger.warning("No messages to restore after filtering!")
        return
    
    # Count media
    media_count = sum(1 for m in messages if m.get('media_path') and include_media)
    logger.info(f"\nWill restore {len(messages)} messages ({media_count} with media)")
    logger.info(f"Estimated time: ~{len(messages) * delay / 60:.1f} minutes (with {delay}s delay)")
    
    # Get media base path
    media_base_path = os.getenv('BACKUP_PATH', '/data/backups')
    
    if dry_run:
        logger.info("\n--- DRY RUN PREVIEW (first 10 messages) ---")
        for msg in messages[:10]:
            sender_name = msg.get('sender', {}).get('name', 'Unknown')
            msg_date = parse_msg_date(msg)
            header = format_message_header(sender_name, msg_date)
            text = (msg.get('text', '') or '')[:80]
            media_info = ""
            if msg.get('media_path') and include_media:
                media_path = os.path.join(media_base_path, msg['media_path'])
                exists = "✓" if os.path.exists(media_path) else "✗ MISSING"
                media_info = f" [📎 {msg.get('media_type', 'media')} {exists}]"
            logger.info(f"  {header}{media_info}\n    {text}")
        if len(messages) > 10:
            logger.info(f"  ... and {len(messages) - 10} more messages")
        logger.info("\nRun without --dry-run to actually send messages.")
        return
    
    # Confirm before proceeding
    logger.warning(f"\n⚠️  About to send {len(messages)} messages to chat {dest_chat_id}")
    logger.warning("⚠️  This action cannot be undone!")
    confirm = input("Type 'YES' to proceed: ")
    if confirm != 'YES':
        logger.info("Aborted by user.")
        return
    
    # Initialize Telegram client
    logger.info("\nConnecting to Telegram...")
    client = await get_telegram_client()
    me = await client.get_me()
    logger.info(f"Logged in as: {me.first_name} ({me.phone})")
    
    # Verify destination chat exists
    try:
        dest_entity = await client.get_entity(dest_chat_id)
        dest_name = getattr(dest_entity, 'title', None) or getattr(dest_entity, 'first_name', 'Unknown')
        logger.info(f"Destination verified: {dest_name}")
    except Exception as e:
        logger.error(f"Cannot access destination chat {dest_chat_id}: {e}")
        return
    
    # Restore messages (oldest first)
    logger.info("\n--- Starting restore ---")
    sent_count = 0
    media_sent = 0
    error_count = 0
    
    for i, msg in enumerate(messages, 1):
        try:
            # Get sender info
            sender_name = msg.get('sender', {}).get('name', 'Unknown')
            msg_date = parse_msg_date(msg)
            
            # Format message with header
            header = format_message_header(sender_name, msg_date)
            text = msg.get('text', '') or ''
            full_text = f"{header}\n{text}" if text else header
            
            # Check for media
            media_file = None
            if include_media and msg.get('media_path'):
                potential_path = os.path.join(media_base_path, msg['media_path'])
                if os.path.exists(potential_path):
                    media_file = potential_path
                else:
                    logger.warning(f"Media file not found: {potential_path}")
            
            # Send message with media (combined) or text only
            if media_file:
                # Send media with caption (text as caption)
                # For photos/videos, caption limit is 1024 chars
                caption = full_text[:1024] if len(full_text) <= 1024 else full_text[:1021] + "..."
                
                await client.send_file(
                    dest_chat_id,
                    media_file,
                    caption=caption
                )
                media_sent += 1
                sent_count += 1
                
            elif full_text.strip():
                # Text-only message
                await client.send_message(dest_chat_id, full_text)
                sent_count += 1
            else:
                # Skip empty messages with no media
                continue
            
            # Progress logging
            if sent_count % 10 == 0:
                pct = i / len(messages) * 100
                logger.info(f"Progress: {sent_count}/{len(messages)} messages ({pct:.1f}%) - {media_sent} with media")
            
            # Rate limiting delay - wait for upload to complete before continuing
            await asyncio.sleep(delay)
            
        except FloodWaitError as e:
            logger.warning(f"Flood wait: sleeping {e.seconds} seconds...")
            await asyncio.sleep(e.seconds + 1)
            # Retry will happen on next iteration, message is skipped
            error_count += 1
        except SlowModeWaitError as e:
            logger.warning(f"Slow mode: sleeping {e.seconds} seconds...")
            await asyncio.sleep(e.seconds + 1)
            error_count += 1
        except Exception as e:
            error_count += 1
            logger.error(f"Error sending message {msg.get('id')}: {e}")
            if error_count > 20:
                logger.error("Too many errors, stopping.")
                break
    
    # Cleanup
    await client.disconnect()
    
    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("RESTORE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Messages sent: {sent_count}")
    logger.info(f"Media uploaded: {media_sent}")
    logger.info(f"Errors: {error_count}")
    logger.info("=" * 60)


def parse_date(date_str: str) -> datetime:
    """Parse date string in YYYY-MM-DD format."""
    return datetime.strptime(date_str, '%Y-%m-%d')


async def main():
    parser = argparse.ArgumentParser(
        description='Restore chat history from backup to Telegram.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Restore to the same chat
    python scripts/restore_chat.py --chat -1001234567890

    # Dry run first (RECOMMENDED)
    python scripts/restore_chat.py --chat -1001234567890 --dry-run

    # Restore to different destination
    python scripts/restore_chat.py --source-chat -1001234567890 --dest-chat -1009876543210

    # Restore only recent messages
    python scripts/restore_chat.py --chat -1001234567890 --after 2024-01-01

    # Text only (no media)
    python scripts/restore_chat.py --chat -1001234567890 --no-media

⚠️  USE WITH CAUTION - Messages will be sent as YOU, not original senders!
        """
    )
    
    # Chat selection (either --chat for same source/dest, or --source-chat/--dest-chat)
    chat_group = parser.add_mutually_exclusive_group(required=True)
    chat_group.add_argument(
        '--chat', type=int,
        help='Chat ID to restore (sends back to same chat)'
    )
    chat_group.add_argument(
        '--source-chat', type=int,
        help='Source chat ID (use with --dest-chat for different destination)'
    )
    
    parser.add_argument(
        '--dest-chat', type=int,
        help='Destination chat ID (required if using --source-chat)'
    )
    
    # Filters
    parser.add_argument(
        '--after', type=str,
        help='Only restore messages after this date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--before', type=str,
        help='Only restore messages before this date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--limit', type=int,
        help='Maximum number of messages to restore'
    )
    
    # Options
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show what would be sent without actually sending'
    )
    parser.add_argument(
        '--delay', type=float, default=2.0,
        help='Seconds between messages for rate limiting (default: 2.0)'
    )
    parser.add_argument(
        '--no-media', action='store_true',
        help='Skip media files, restore text only'
    )
    
    args = parser.parse_args()
    
    # Determine source and destination
    if args.chat:
        source_chat_id = args.chat
        dest_chat_id = args.chat
    else:
        source_chat_id = args.source_chat
        if not args.dest_chat:
            parser.error("--dest-chat is required when using --source-chat")
        dest_chat_id = args.dest_chat
    
    # Parse dates
    after_date = parse_date(args.after) if args.after else None
    before_date = parse_date(args.before) if args.before else None
    
    await restore_chat(
        source_chat_id=source_chat_id,
        dest_chat_id=dest_chat_id,
        dry_run=args.dry_run,
        after_date=after_date,
        before_date=before_date,
        limit=args.limit,
        delay=args.delay,
        include_media=not args.no_media
    )


if __name__ == '__main__':
    asyncio.run(main())
