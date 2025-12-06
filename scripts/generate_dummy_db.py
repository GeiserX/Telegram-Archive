#!/usr/bin/env python3
"""
Script to generate a dummy SQLite database for testing the Telegram Archive Viewer.
Usage: python scripts/generate_dummy_db.py
"""
import os
import sys
import random
import shutil
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import Database

def generate_dummy_db(db_path="data/backups/telegram_backup.db"):
    # Ensure directory exists
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    # Remove existing db if any
    if os.path.exists(db_path):
        os.remove(db_path)
    
    db = Database(db_path)
    
    print(f"Generating dummy database at {db_path}...")

    # 1. Create Users
    users = [
        {'id': 1001, 'username': 'alice', 'first_name': 'Alice', 'last_name': 'Wonderland'},
        {'id': 1002, 'username': 'bob_builder', 'first_name': 'Bob', 'last_name': 'Builder'},
        {'id': 1003, 'username': 'charlie', 'first_name': 'Charlie', 'last_name': 'Chaplin'},
        {'id': 9999, 'username': 'me', 'first_name': 'Me', 'last_name': ''}  # Owner
    ]
    
    for u in users:
        db.upsert_user(u)

    # 2. Create Chats
    chats = [
        # Private Chat
        {
            'id': 1001, 'type': 'private', 'title': 'Alice Wonderland', 
            'username': 'alice', 'first_name': 'Alice', 'last_name': 'Wonderland',
            'updated_at': datetime.now()
        },
        # Group Chat
        {
            'id': -100987654321, 'type': 'group', 'title': 'Project Alpha 🚀', 
            'participants_count': 15, 'updated_at': datetime.now()
        },
        # Channel
        {
            'id': -100123456789, 'type': 'channel', 'title': 'Tech News 📰', 
            'username': 'technews_daily', 'participants_count': 50320, 
            'updated_at': datetime.now()
        }
    ]
    
    for c in chats:
        db.upsert_chat(c)

    # 3. Generate Messages
    messages = []
    base_time = datetime.now() - timedelta(days=5)
    
    # Alice Chat Messages
    chat_id = 1001
    
    messages.append({
        'id': 1, 'chat_id': chat_id, 'sender_id': 1001, 
        'date': base_time + timedelta(hours=1),
        'text': 'Hey! Check out this new archiving tool.',
        'is_outgoing': 0
    })
    messages.append({
        'id': 2, 'chat_id': chat_id, 'sender_id': 9999, 
        'date': base_time + timedelta(hours=1, minutes=5),
        'text': 'Oh nice, is it self-hosted?',
        'is_outgoing': 1
    })
    messages.append({
        'id': 3, 'chat_id': chat_id, 'sender_id': 1001, 
        'date': base_time + timedelta(hours=1, minutes=6),
        'text': 'Yeah, runs in Docker. Super slick UI too.',
        'is_outgoing': 0
    })
    # Sticker
    messages.append({
        'id': 4, 'chat_id': 1001, 'sender_id': 1001, 
        'date': base_time + timedelta(hours=1, minutes=7),
        'text': '', 'media_type': 'sticker', 'is_outgoing': 0,
        'raw_data': {'sticker': {'emoji': '😎'}}
    })

    # Group Chat Messages
    chat_id = -100987654321
    messages.append({
        'id': 100, 'chat_id': chat_id, 'sender_id': 1002, 
        'date': base_time + timedelta(days=1),
        'text': 'Meeting start in 10 mins',
        'is_outgoing': 0
    })
    messages.append({
        'id': 101, 'chat_id': chat_id, 'sender_id': 1003, 
        'date': base_time + timedelta(days=1, minutes=2),
        'text': 'I will be late, stuck in traffic 🚗',
        'is_outgoing': 0
    })
    messages.append({
        'id': 102, 'chat_id': chat_id, 'sender_id': 9999, 
        'date': base_time + timedelta(days=1, minutes=5),
        'text': 'No worries, we will record it.',
        'is_outgoing': 1,
        'reply_to_msg_id': 101,
        'reply_to_text': 'I will be late, stuck in traffic 🚗'
    })

    # Channel Messages
    chat_id = -100123456789
    messages.append({
        'id': 500, 'chat_id': chat_id, 'sender_id': None, 
        'date': base_time + timedelta(days=2),
        'text': '📢 BREAKING: Python 4.0 released today! (Just kidding)',
        'is_outgoing': 0
    })
    messages.append({
        'id': 501, 'chat_id': chat_id, 'sender_id': None, 
        'date': base_time + timedelta(days=2, hours=4),
        'text': 'Here is the full changelog:',
        'is_outgoing': 0,
        'media_type': 'document',
        'media_path': 'changelog.pdf'
    })

    # Insert all messages
    db.insert_messages_batch(messages)
    
    # Update stats
    for c in chats:
        db.update_sync_status(c['id'], 1000, 10)

    print("Dummy database created successfully!")
    db.close()

if __name__ == "__main__":
    generate_dummy_db()
