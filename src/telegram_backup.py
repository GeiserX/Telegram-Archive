"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import os
import logging
import hashlib
from datetime import datetime
from typing import Optional, List, Dict
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
        self.config.validate_credentials()
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
            
            # Connect to Telegram
            logger.info("Connecting to Telegram...")
            await self.client.start(phone=self.config.phone)
            
            # Get current user info
            me = await self.client.get_me()
            logger.info(f"Logged in as {me.first_name} ({me.id})")
            
            # Store owner ID and backfill is_outgoing for existing messages
            self.db.set_metadata('owner_id', str(me.id))
            self.db.backfill_is_outgoing(me.id)

            start_time = datetime.now()
            
            # Get all dialogs (chats)
            logger.info("Fetching dialog list...")
            dialogs = await self._get_dialogs()
            logger.info(f"Found {len(dialogs)} total dialogs")

            # Filter dialogs based on chat type and ID filters
            # Also delete explicitly excluded chats from database
            filtered_dialogs = []
            explicitly_excluded_chat_ids = set()
            
            for dialog in dialogs:
                entity = dialog.entity
                chat_id = entity.id

                is_user = isinstance(entity, User) and not entity.bot
                is_group = isinstance(entity, Chat) or (
                    isinstance(entity, Channel) and entity.megagroup
                )
                is_channel = isinstance(entity, Channel) and not entity.megagroup

                # Check if chat is explicitly in an exclude list (not just filtered out)
                is_explicitly_excluded = (
                    chat_id in self.config.global_exclude_ids or
                    (is_user and chat_id in self.config.private_exclude_ids) or
                    (is_group and chat_id in self.config.groups_exclude_ids) or
                    (is_channel and chat_id in self.config.channels_exclude_ids)
                )

                if is_explicitly_excluded:
                    # Chat is explicitly excluded - mark for deletion
                    explicitly_excluded_chat_ids.add(chat_id)
                elif self.config.should_backup_chat(chat_id, is_user, is_group, is_channel):
                    # Chat should be backed up
                    filtered_dialogs.append(dialog)
            
            # Delete only explicitly excluded chats from database
            if explicitly_excluded_chat_ids:
                logger.info(f"Deleting {len(explicitly_excluded_chat_ids)} explicitly excluded chats from database...")
                for chat_id in explicitly_excluded_chat_ids:
                    try:
                        self.db.delete_chat_and_related_data(chat_id, self.config.media_path)
                    except Exception as e:
                        logger.error(f"Error deleting chat {chat_id}: {e}", exc_info=True)

            logger.info(f"Backing up {len(filtered_dialogs)} dialogs after filtering")

            if not filtered_dialogs:
                logger.info("No dialogs to back up after filtering")
                return

            # Ensure we start from the most recently active chats
            filtered_dialogs.sort(
                key=lambda d: getattr(d, "date", None) or datetime.min,
                reverse=True,
            )

            # Detect whether we've already completed at least one full backup run
            # (i.e. some chats have a non-zero last_message_id recorded)
            has_synced_before = any(
                self.db.get_last_message_id(dialog.entity.id) > 0
                for dialog in filtered_dialogs
            )

            # Backup each dialog
            total_messages = 0
            for i, dialog in enumerate(filtered_dialogs, 1):
                entity = dialog.entity
                chat_id = entity.id
                chat_name = self._get_chat_name(entity)
                logger.info(f"[{i}/{len(filtered_dialogs)}] Backing up: {chat_name} (ID: {chat_id})")

                try:
                    message_count = await self._backup_dialog(dialog)
                    total_messages += message_count
                    logger.info(f"  → Backed up {message_count} new messages")

                    # Optimization: after initial full run, if the most recently
                    # active chat has no new messages, we assume the rest don't either.

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

        # Ensure profile photos for users and groups/channels are backed up.
        # This runs on every dialog backup but only downloads new files when
        # Telegram reports a different profile photo.
        try:
            await self._ensure_profile_photo(entity)
        except Exception as e:
            logger.error(f"Error downloading profile photo for {chat_id}: {e}", exc_info=True)
        
        # Get last synced message ID for incremental backup
        last_message_id = self.db.get_last_message_id(chat_id)
        
        # Fetch new messages
        messages = []
        batch_data = []
        batch_size = self.config.batch_size
        total_processed = 0
        
        async for message in self.client.iter_messages(
            entity,
            min_id=last_message_id,
            reverse=True
        ):
            messages.append(message)
            
            # Process message
            msg_data = await self._process_message(message, chat_id)
            batch_data.append(msg_data)
            
            # Batch insert every 50 messages
            if len(batch_data) >= batch_size:
                self.db.insert_messages_batch(batch_data)
                total_processed += len(batch_data)
                logger.info(f"  → Processed {total_processed} messages...")
                batch_data = []
        
        # Insert remaining messages
        if batch_data:
            self.db.insert_messages_batch(batch_data)
            total_processed += len(batch_data)
            
        # Update sync status
        if messages:
            max_message_id = max(msg.id for msg in messages)
            self.db.update_sync_status(chat_id, max_message_id, len(messages))
            
        # Sync deletions and edits if enabled (expensive!)
        if self.config.sync_deletions_edits:
            await self._sync_deletions_and_edits(chat_id, entity)
        
        return len(messages)

    async def _sync_deletions_and_edits(self, chat_id: int, entity):
        """
        Sync deletions and edits for existing messages in the database.
        
        Args:
            chat_id: Chat ID to sync
            entity: Telegram entity
        """
        logger.info(f"  → Syncing deletions and edits for chat {chat_id}...")
        
        # Get all local message IDs and their edit dates
        local_messages = self.db.get_messages_sync_data(chat_id)
        if not local_messages:
            return
            
        local_ids = list(local_messages.keys())
        total_checked = 0
        total_deleted = 0
        total_updated = 0
        
        # Process in batches
        batch_size = 100
        for i in range(0, len(local_ids), batch_size):
            batch_ids = local_ids[i:i + batch_size]
            
            try:
                # Fetch current state from Telegram
                remote_messages = await self.client.get_messages(entity, ids=batch_ids)
                
                for msg_id, remote_msg in zip(batch_ids, remote_messages):
                    # Check for deletion
                    if remote_msg is None:
                        self.db.delete_message(chat_id, msg_id)
                        total_deleted += 1
                        continue
                    
                    # Check for edits
                    # We compare string representations of edit_date
                    remote_edit_date = remote_msg.edit_date
                    local_edit_date_str = local_messages[msg_id]
                    
                    should_update = False
                    
                    if remote_edit_date:
                        # If remote has edit_date, check if it differs from local
                        # This handles cases where local is None or different
                        if str(remote_edit_date) != str(local_edit_date_str):
                             should_update = True
                    
                    if should_update:
                        # Update text and edit_date
                        self.db.update_message_text(chat_id, msg_id, remote_msg.message, remote_msg.edit_date)
                        total_updated += 1
                        
            except Exception as e:
                logger.error(f"Error syncing batch for chat {chat_id}: {e}")
            
            total_checked += len(batch_ids)
            if total_checked % 1000 == 0:
                logger.info(f"  → Checked {total_checked}/{len(local_ids)} messages for sync...")
                
        if total_deleted > 0 or total_updated > 0:
            logger.info(f"  → Sync result: {total_deleted} deleted, {total_updated} updated")
    
    def _extract_forward_from_id(self, message: Message) -> Optional[int]:
        """
        Extract forward sender ID safely handling different Peer types.
        
        Args:
            message: Message object
            
        Returns:
            ID of the forward sender or None
        """
        if not message.fwd_from or not message.fwd_from.from_id:
            return None
        
        peer = message.fwd_from.from_id
        
        # Handle different Peer types
        if hasattr(peer, 'user_id'):
            return peer.user_id
        if hasattr(peer, 'channel_id'):
            return peer.channel_id
        if hasattr(peer, 'chat_id'):
            return peer.chat_id
            
        return None

    async def _process_message(self, message: Message, chat_id: int) -> Dict:
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
            'reply_to_text': None,
            'forward_from_id': self._extract_forward_from_id(message),
            'edit_date': message.edit_date,
            'media_type': None,
            'media_id': None,
            'media_path': None,
            'media_path': None,
            'raw_data': {},
            'is_outgoing': 1 if message.out else 0
        }
        
        # Get reply text if this is a reply
        if message.reply_to_msg_id and message.reply_to:
            reply_msg = message.reply_to
            if hasattr(reply_msg, 'message'):
                # Truncate to first 100 chars like Telegram does
                reply_text = (reply_msg.message or '')[:100]
                message_data['reply_to_text'] = reply_text
        
        # Handle media
        if message.media and self.config.download_media:
            media_info = await self._process_media(message, chat_id)
            if media_info:
                message_data['media_type'] = media_info['type']
                message_data['media_id'] = media_info['id']
                message_data['media_path'] = media_info.get('file_path')
        
        # Return message data for batch processing
        return message_data

    async def _ensure_profile_photo(self, entity) -> None:
        """
        Download and keep a copy of the profile photo for users and chats.

        We only ever add new files when Telegram reports a different photo,
        and we never delete older ones. This way, if a user removes their
        photo later, we still keep at least one historical copy.
        """
        # Some entities (e.g. Deleted Account) may not have a photo attribute
        photo = getattr(entity, "photo", None)
        if not photo:
            return

        # Determine target directory based on entity type
        if isinstance(entity, User):
            base_dir = os.path.join(self.config.media_path, "avatars", "users")
        else:
            # Covers Chat and Channel (groups, supergroups, channels)
            base_dir = os.path.join(self.config.media_path, "avatars", "chats")

        os.makedirs(base_dir, exist_ok=True)

        # Use Telegram's internal photo id to derive a stable filename so
        # a new photo results in a new file, while old ones are kept.
        photo_id = getattr(photo, "photo_id", None) or getattr(photo, "id", None)
        suffix = str(photo_id) if photo_id is not None else "current"
        file_name = f"{entity.id}_{suffix}.jpg"
        file_path = os.path.join(base_dir, file_name)

        # If we've already downloaded this exact photo, skip
        if os.path.exists(file_path):
            return

        await self.client.download_profile_photo(entity, file_path)
    
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
        
        # Get Telegram's file unique ID for deduplication
        telegram_file_id = None
        if hasattr(media, 'photo'):
            telegram_file_id = str(getattr(media.photo, 'id', None))
        elif hasattr(media, 'document'):
            telegram_file_id = str(getattr(media.document, 'id', None))
        
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
            
            # Generate filename using file_id for automatic deduplication
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
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
    
    def _get_media_filename(self, message: Message, media_type: str, telegram_file_id: str = None) -> str:
        """
        Generate a unique filename using Telegram's file_id.
        Properly handles files sent "as documents" by checking mime_type and original filename.
        """
        import mimetypes

        # First, try to get original filename from document attributes
        original_name = None
        mime_type = None

        if hasattr(message.media, 'document') and message.media.document:
            doc = message.media.document
            mime_type = getattr(doc, 'mime_type', None)

            for attr in doc.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    original_name = attr.file_name
                    break

        # If we have original filename, use it (with file_id prefix for uniqueness)
        if original_name and telegram_file_id:
            safe_id = str(telegram_file_id).replace('/', '_').replace('\\', '_')
            return f"{safe_id}_{original_name}"

        # Determine extension from mime_type, then fall back to media_type
        extension = None

        if mime_type:
            # Use mimetypes to get proper extension from mime_type
            ext = mimetypes.guess_extension(mime_type)
            if ext:
                extension = ext.lstrip('.')
                # Fix common mimetypes oddities
                if extension == 'jpe':
                    extension = 'jpg'

        # Fall back to media_type-based extension
        if not extension:
            extension = self._get_media_extension(media_type)

        # Build filename
        if telegram_file_id:
            safe_id = str(telegram_file_id).replace('/', '_').replace('\\', '_')
            return f"{safe_id}.{extension}"

        # Last resort: timestamp-based
        timestamp = message.date.strftime('%Y%m%d_%H%M%S')
        return f"{message.id}_{timestamp}.{extension}"

    def _get_media_extension(self, media_type: str) -> str:
        """Get file extension for media type (fallback only)."""
        extensions = {
            'photo': 'jpg',
            'video': 'mp4',
            'audio': 'mp3',
            'voice': 'ogg',
            'document': 'bin'  # Only used if mime_type detection fails
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
