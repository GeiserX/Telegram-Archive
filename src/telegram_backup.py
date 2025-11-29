"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import os
import logging
import hashlib
from datetime import datetime
from typing import Optional, List
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import (
    User, Chat, Channel, Message,
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaContact,
    MessageMediaGeo, MessageMediaPoll
)

from .config import Config
from .database import Database

logger = logging.getLogger(__name__)


class TelegramBackup:
    """Main class for managing Telegram backups."""
    
    def __init__(self, config: Config):
        """
        Initialize Telegram backup manager.
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.db = Database(config.database_path)
        self.client: Optional[TelegramClient] = None
        
        logger.info("TelegramBackup initialized")
    
    async def connect(self):
        """Connect to Telegram and authenticate."""
        self.client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash
        )
        
        # Connect without starting interactive flow
        await self.client.connect()
        
        # Check authorization status
        if not await self.client.is_user_authorized():
            logger.error("❌ Session not authorized!")
            logger.error("Please run the authentication setup first:")
            logger.error("  Docker: ./init_auth.bat (Windows) or ./init_auth.sh (Linux/Mac)")
            logger.error("  Local:  python -m src.setup_auth")
            raise RuntimeError("Session not authorized. Please run authentication setup.")
            
        me = await self.client.get_me()
        logger.info(f"Connected as {me.first_name} ({me.phone})")
    
    async def disconnect(self):
        """Disconnect from Telegram."""
        if self.client:
            await self.client.disconnect()
            logger.info("Disconnected from Telegram")
    
    async def backup_all(self):
        """
        Perform backup of all configured chats.
        This is the main entry point for scheduled backups.
        """
        try:
            logger.info("Starting backup process...")
            start_time = datetime.now()
            
            # Get all dialogs (chats)
            dialogs = await self._get_dialogs()
            logger.info(f"Found {len(dialogs)} total dialogs")
            
            # Filter dialogs based on chat type and ID filters
            filtered_dialogs = []
            for dialog in dialogs:
                entity = dialog.entity
                chat_id = entity.id
                
                is_user = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, Chat) or (
                    isinstance(entity, Channel) and entity.megagroup
                )
                is_channel = isinstance(entity, Channel) and not entity.megagroup
                
                if self.config.should_backup_chat(chat_id, is_user, is_group, is_channel):
                    filtered_dialogs.append(dialog)
            
            logger.info(f"Backing up {len(filtered_dialogs)} dialogs after filtering")
            
            # Backup each dialog
            total_messages = 0
            for i, dialog in enumerate(filtered_dialogs, 1):
                entity = dialog.entity
                chat_name = self._get_chat_name(entity)
                logger.info(f"[{i}/{len(filtered_dialogs)}] Backing up: {chat_name}")
                
                try:
                    message_count = await self._backup_dialog(dialog)
                    total_messages += message_count
                    logger.info(f"  → Backed up {message_count} new messages")
                except Exception as e:
                    logger.error(f"  → Error backing up {chat_name}: {e}", exc_info=True)
            
            # Log statistics
            duration = (datetime.now() - start_time).total_seconds()
            stats = self.db.get_statistics()
            
            logger.info("=" * 60)
            logger.info("Backup completed successfully!")
            logger.info(f"Duration: {duration:.2f} seconds")
            logger.info(f"New messages: {total_messages}")
            logger.info(f"Total chats: {stats['chats']}")
            logger.info(f"Total messages: {stats['messages']}")
            logger.info(f"Total media files: {stats['media_files']}")
            logger.info(f"Total storage: {stats['total_size_mb']} MB")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"Backup failed: {e}", exc_info=True)
            raise
    
    async def _get_dialogs(self) -> List:
        """
        Get all dialogs (chats) from Telegram.
        
        Returns:
            List of dialog objects
        """
        # Use the simpler get_dialogs method which handles pagination automatically
        dialogs = await self.client.get_dialogs()
        return dialogs
    
    async def _backup_dialog(self, dialog) -> int:
        """
        Backup a single dialog (chat).
        
        Args:
            dialog: Dialog object from Telegram
            
        Returns:
            Number of new messages backed up
        """
        entity = dialog.entity
        chat_id = entity.id
        
        # Save chat information
        chat_data = self._extract_chat_data(entity)
        self.db.upsert_chat(chat_data)
        
        # Get last synced message ID for incremental backup
        last_message_id = self.db.get_last_message_id(chat_id)
        
        # Fetch new messages
        messages = []
        async for message in self.client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True
        ):
            messages.append(message)
        
        if not messages:
            return 0
        
        # Process messages
        for message in messages:
            await self._process_message(message, chat_id)
        
        # Update sync status
        if messages:
            max_message_id = max(msg.id for msg in messages)
            self.db.update_sync_status(chat_id, max_message_id, len(messages))
        
        return len(messages)
    
    async def _process_message(self, message: Message, chat_id: int):
        """
        Process and save a single message.
        
        Args:
            message: Message object from Telegram
            chat_id: Chat identifier
        """
        # Save sender information if available
        if message.sender:
            sender_data = self._extract_user_data(message.sender)
            if sender_data:
                self.db.upsert_user(sender_data)
        
        # Extract message data
        message_data = {
            'id': message.id,
            'chat_id': chat_id,
            'sender_id': message.sender_id,
            'date': message.date,
            'text': message.text or '',
            'reply_to_msg_id': message.reply_to_msg_id,
            'forward_from_id': message.fwd_from.from_id.user_id if message.fwd_from else None,
            'edit_date': message.edit_date,
            'media_type': None,
            'media_id': None,
            'media_path': None,
            'raw_data': {}
        }
        
        # Handle media
        if message.media and self.config.download_media:
            media_info = await self._process_media(message, chat_id)
            if media_info:
                message_data['media_type'] = media_info['type']
                message_data['media_id'] = media_info['id']
                message_data['media_path'] = media_info.get('file_path')
        
        # Save message
        self.db.insert_message(message_data)
    
    async def _process_media(self, message: Message, chat_id: int) -> Optional[dict]:
        """
        Process and download media from a message.
        
        Args:
            message: Message object with media
            chat_id: Chat identifier
            
        Returns:
            Dictionary with media information, or None if skipped
        """
        media = message.media
        media_type = self._get_media_type(media)
        
        if not media_type:
            return None
        
        # Generate unique media ID
        media_id = f"{chat_id}_{message.id}_{media_type}"
        
        # Check file size
        file_size = getattr(media, 'size', 0) or 0
        max_size = self.config.get_max_media_size_bytes()
        
        if file_size > max_size:
            logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
            return {
                'id': media_id,
                'type': media_type,
                'message_id': message.id,
                'chat_id': chat_id,
                'file_size': file_size,
                'downloaded': False
            }
        
        # Download media
        try:
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)
            
            # Generate filename
            file_name = self._get_media_filename(message, media_type)
            file_path = os.path.join(chat_media_dir, file_name)
            
            # Download if not already exists
            if not os.path.exists(file_path):
                await self.client.download_media(message, file_path)
                logger.debug(f"Downloaded media: {file_name}")
            
            # Extract media metadata
            media_data = {
                'id': media_id,
                'type': media_type,
                'message_id': message.id,
                'chat_id': chat_id,
                'file_name': file_name,
                'file_path': file_path,
                'file_size': file_size,
                'mime_type': getattr(media, 'mime_type', None),
                'downloaded': True,
                'download_date': datetime.now()
            }
            
            # Add type-specific metadata
            if hasattr(media, 'photo'):
                photo = media.photo
                media_data['width'] = getattr(photo, 'w', None)
                media_data['height'] = getattr(photo, 'h', None)
            elif hasattr(media, 'document'):
                doc = media.document
                for attr in doc.attributes:
                    if hasattr(attr, 'w') and hasattr(attr, 'h'):
                        media_data['width'] = attr.w
                        media_data['height'] = attr.h
                    if hasattr(attr, 'duration'):
                        media_data['duration'] = attr.duration
            
            # Save to database
            self.db.insert_media(media_data)
            
            return media_data
            
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return {
                'id': media_id,
                'type': media_type,
                'message_id': message.id,
                'chat_id': chat_id,
                'downloaded': False
            }
    
    def _get_media_type(self, media) -> Optional[str]:
        """Get media type as string."""
        if isinstance(media, MessageMediaPhoto):
            return 'photo'
        elif isinstance(media, MessageMediaDocument):
            # Check document attributes to determine specific type
            if hasattr(media, 'document') and media.document:
                for attr in media.document.attributes:
                    attr_type = type(attr).__name__
                    if 'Video' in attr_type:
                        return 'video'
                    elif 'Audio' in attr_type:
                        return 'audio'
                    elif 'Voice' in attr_type:
                        return 'voice'
            return 'document'
        elif isinstance(media, MessageMediaContact):
            return 'contact'
        elif isinstance(media, MessageMediaGeo):
            return 'geo'
        elif isinstance(media, MessageMediaPoll):
            return 'poll'
        return None
    
    def _get_media_filename(self, message: Message, media_type: str) -> str:
        """Generate a filename for media."""
        # Try to get original filename
        if hasattr(message.media, 'document'):
            for attr in message.media.document.attributes:
                if hasattr(attr, 'file_name'):
                    return attr.file_name
        
        # Generate filename based on message ID and type
        timestamp = message.date.strftime('%Y%m%d_%H%M%S')
        extension = self._get_media_extension(media_type)
        return f"{timestamp}_{message.id}.{extension}"
    
    def _get_media_extension(self, media_type: str) -> str:
        """Get file extension for media type."""
        extensions = {
            'photo': 'jpg',
            'video': 'mp4',
            'audio': 'mp3',
            'voice': 'ogg',
            'document': 'bin'
        }
        return extensions.get(media_type, 'bin')
    
    def _extract_chat_data(self, entity) -> dict:
        """Extract chat data from entity."""
        chat_data = {'id': entity.id}
        
        if isinstance(entity, User):
            chat_data['type'] = 'private'
            chat_data['first_name'] = entity.first_name
            chat_data['last_name'] = entity.last_name
            chat_data['username'] = entity.username
            chat_data['phone'] = entity.phone
        elif isinstance(entity, Chat):
            chat_data['type'] = 'group'
            chat_data['title'] = entity.title
            chat_data['participants_count'] = entity.participants_count
        elif isinstance(entity, Channel):
            chat_data['type'] = 'channel' if not entity.megagroup else 'group'
            chat_data['title'] = entity.title
            chat_data['username'] = entity.username
        
        return chat_data
    
    def _extract_user_data(self, user) -> Optional[dict]:
        """Extract user data from user entity."""
        if not isinstance(user, User):
            return None
        
        return {
            'id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone': user.phone,
            'is_bot': user.bot
        }
    
    def _get_chat_name(self, entity) -> str:
        """Get a readable name for a chat."""
        if isinstance(entity, User):
            name = entity.first_name or ''
            if entity.last_name:
                name += f" {entity.last_name}"
            if entity.username:
                name += f" (@{entity.username})"
            return name or f"User {entity.id}"
        elif isinstance(entity, (Chat, Channel)):
            return entity.title or f"Chat {entity.id}"
        return f"Unknown {entity.id}"


async def run_backup(config: Config):
    """
    Run a single backup operation.
    
    Args:
        config: Configuration object
    """
    backup = TelegramBackup(config)
    try:
        await backup.connect()
        await backup.backup_all()
    finally:
        await backup.disconnect()
        backup.db.close()


if __name__ == '__main__':
    # Test backup
    import asyncio
    from .config import Config, setup_logging
    
    config = Config()
    setup_logging(config)
    
    asyncio.run(run_backup(config))
