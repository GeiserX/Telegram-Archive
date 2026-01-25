#!/usr/bin/env python3
"""
Migration script to populate is_pinned for existing messages.
Uses Telegram's InputMessagesFilterPinned to efficiently fetch all pinned messages.

Run inside the backup container:
  docker exec -it telegram-backup-sergio python /app/scripts/migrate_pinned_messages.py
"""
import asyncio
import os
import sys

# Ensure /app is in path
sys.path.insert(0, '/app')

import asyncpg
from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterPinned


async def migrate_pinned_messages():
    # DB connection
    conn = await asyncpg.connect(
        host=os.environ['POSTGRES_HOST'],
        port=int(os.environ.get('POSTGRES_PORT', 5432)),
        user=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'],
        database=os.environ['POSTGRES_DB']
    )
    print("Connected to database")
    
    # Ensure is_pinned column exists
    try:
        await conn.execute(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_pinned INTEGER DEFAULT 0"
        )
        print("Ensured is_pinned column exists")
    except Exception as e:
        print(f"Column check: {e}")
    
    # Reset all is_pinned to 0 first (in case pinned messages changed)
    reset_count = await conn.execute("UPDATE messages SET is_pinned = 0 WHERE is_pinned = 1")
    print(f"Reset is_pinned for all messages")
    
    # Telegram client
    session_path = os.path.join(
        os.environ.get('SESSION_PATH', '/data/session'),
        os.environ.get('SESSION_NAME', 'telegram_backup')
    )
    client = TelegramClient(
        session_path,
        int(os.environ['TELEGRAM_API_ID']),
        os.environ['TELEGRAM_API_HASH']
    )
    await client.start()
    print("Connected to Telegram")
    
    # Get all chats from database
    rows = await conn.fetch("SELECT id, title, type FROM chats")
    print(f"Found {len(rows)} chats to scan for pinned messages")
    
    total_updated = 0
    chats_with_pinned = 0
    
    for row in rows:
        chat_id, title, chat_type = row['id'], row['title'], row['type']
        display = (title or str(chat_id))[:40]
        
        # Skip private chats (they don't have pinned messages in the same way)
        if chat_type in ('private', 'bot'):
            continue
        
        try:
            # Get all pinned messages using Telegram's filter
            entity = await client.get_entity(chat_id)
            pinned_messages = await client.get_messages(
                entity, 
                filter=InputMessagesFilterPinned(),
                limit=100  # Get up to 100 pinned messages per chat
            )
            
            if pinned_messages:
                # Update is_pinned for these messages in the database
                pinned_ids = [msg.id for msg in pinned_messages]
                
                # Batch update
                result = await conn.execute(
                    """
                    UPDATE messages 
                    SET is_pinned = 1 
                    WHERE chat_id = $1 AND id = ANY($2::bigint[])
                    """,
                    chat_id, pinned_ids
                )
                
                updated_count = int(result.split()[-1]) if result else 0
                if updated_count > 0:
                    print(f"✓ {display}: {updated_count} pinned messages")
                    total_updated += updated_count
                    chats_with_pinned += 1
                    
        except Exception as e:
            err = str(e)[:60]
            # Only print errors for channels/groups, not for inaccessible chats
            if "Could not find" not in err and "No user has" not in err:
                print(f"✗ {display}: {err}")
    
    await client.disconnect()
    await conn.close()
    
    print(f"\nDone!")
    print(f"  Chats with pinned messages: {chats_with_pinned}")
    print(f"  Total pinned messages found: {total_updated}")


if __name__ == '__main__':
    asyncio.run(migrate_pinned_messages())
