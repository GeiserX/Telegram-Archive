#!/usr/bin/env python3
"""
One-time migration script to populate pinned_message_id for existing chats.
Run inside the backup container: 
  docker exec -it telegram-backup-annais python /app/scripts/migrate_pinned_messages.py
"""
import asyncio
import os
import sys

# Ensure /app is in path
sys.path.insert(0, '/app')

from telethon import TelegramClient
from sqlalchemy import select, update

# Import from the db package
from src.db import DatabaseManager, init_database
from src.db.models import Chat as ChatModel


async def migrate_pinned_messages():
    # Initialize database using the app's init function
    await init_database()
    from src.db import get_db_manager
    db_manager = await get_db_manager()
    
    # Initialize Telegram client
    api_id = int(os.environ.get('TELEGRAM_API_ID', 0))
    api_hash = os.environ.get('TELEGRAM_API_HASH', '')
    session_name = os.environ.get('SESSION_NAME', 'telegram_backup')
    session_dir = os.environ.get('SESSION_PATH', '/data/session')
    session_path = os.path.join(session_dir, session_name)
    
    if not api_id or not api_hash:
        print("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        return
    
    print(f"Using session: {session_path}")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    
    print("Connected to Telegram")
    
    # Get all chats from database
    async with db_manager.async_session_factory() as session:
        result = await session.execute(
            select(ChatModel.id, ChatModel.title, ChatModel.pinned_message_id)
        )
        chats = result.all()
    
    print(f"Found {len(chats)} chats in database")
    
    updated = 0
    skipped = 0
    errors = 0
    
    for chat_id, title, current_pinned in chats:
        display_title = (title or str(chat_id))[:40]
        
        if current_pinned is not None:
            skipped += 1
            continue
            
        try:
            # Get entity from Telegram
            entity = await client.get_entity(chat_id)
            pinned_msg_id = getattr(entity, 'pinned_msg_id', None)
            
            if pinned_msg_id:
                async with db_manager.async_session_factory() as session:
                    await session.execute(
                        update(ChatModel)
                        .where(ChatModel.id == chat_id)
                        .values(pinned_message_id=pinned_msg_id)
                    )
                    await session.commit()
                print(f"✓ {display_title}: pinned message #{pinned_msg_id}")
                updated += 1
            else:
                skipped += 1
                
        except Exception as e:
            error_msg = str(e)[:50]
            print(f"✗ {display_title}: {error_msg}")
            errors += 1
    
    await client.disconnect()
    
    print(f"\nDone! Updated: {updated}, Skipped: {skipped}, Errors: {errors}")


if __name__ == '__main__':
    asyncio.run(migrate_pinned_messages())
