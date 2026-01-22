"""
Main Telegram backup module.
Handles Telegram client connection, message fetching, and incremental backup logic.
"""

import os
import logging
import hashlib
import base64
from datetime import datetime, timezone
from typing import Optional, List, Dict
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import (
    User, Chat, Channel, Message,
    MessageMediaPhoto, MessageMediaDocument,
    MessageMediaContact,
    MessageMediaGeo, MessageMediaPoll,
    TextWithEntities,
    PeerChannel, PeerChat, PeerUser
)
from telethon.utils import get_peer_id

from .config import Config
from .avatar_utils import get_avatar_paths
from .db import DatabaseAdapter, create_adapter

logger = logging.getLogger(__name__)


class TelegramBackup:
    """Main class for managing Telegram backups."""
    
    def __init__(
        self, 
        config: Config, 
        db: DatabaseAdapter,
        client: Optional[TelegramClient] = None
    ):
        """
        Initialize Telegram backup manager.
        
        Args:
            config: Configuration object
            db: Async database adapter (must be initialized before passing)
            client: Optional existing TelegramClient to use (for shared connection).
                   If not provided, will create a new client in connect().
        """
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.client: Optional[TelegramClient] = client
        self._owns_client = client is None  # Track if we created the client
        
        logger.info("TelegramBackup initialized")
    
    def _get_marked_id(self, entity) -> int:
        """
        Get the marked ID for an entity (with -100 prefix for channels/supergroups).
        
        Telegram uses different ID formats:
        - Users: positive ID (e.g., 123456789)
        - Basic groups (Chat): negative ID (e.g., -123456789)
        - Supergroups/Channels: marked with -100 prefix (e.g., -1001234567890)
        
        This ensures IDs match what users see in Telegram and configure in env vars.
        """
        return get_peer_id(entity)
    
    @classmethod
    async def create(
        cls, 
        config: Config,
        client: Optional[TelegramClient] = None
    ) -> "TelegramBackup":
        """
        Factory method to create TelegramBackup with initialized database.
        
        Args:
            config: Configuration object
            client: Optional existing TelegramClient to use (for shared connection)
            
        Returns:
            Initialized TelegramBackup instance
        """
        db = await create_adapter()
        return cls(config, db, client=client)
    
    async def connect(self):
        """
        Connect to Telegram and authenticate.
        
        If a client was provided in __init__, verifies it's connected.
        Otherwise, creates a new client and connects.
        """
        # If using shared client, just verify it's connected
        if self.client is not None and not self._owns_client:
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")
            logger.debug("Using shared Telegram client")
            return
        
        # Create new client
        self.client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash
        )
        self._owns_client = True
        
        # Fix for database locked errors: Enable WAL mode for session DB
        # This is critical for concurrency when the viewer is also running
        try:
            if hasattr(self.client.session, '_conn'):
                # Ensure connection is open
                if self.client.session._conn is None:
                    # Trigger connection if lazy loaded (though usually it's open)
                    pass 
                
                if self.client.session._conn:
                    self.client.session._conn.execute("PRAGMA journal_mode=WAL")
                    self.client.session._conn.execute("PRAGMA busy_timeout=30000")
                    logger.info("Enabled WAL mode for Telethon session database")
        except Exception as e:
            logger.warning(f"Could not enable WAL mode for session DB: {e}")
        
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
        """
        Disconnect from Telegram.
        
        Only disconnects if we own the client (created it ourselves).
        Shared clients are managed by the connection owner.
        """
        if self.client and self._owns_client:
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
            await self.db.set_metadata('owner_id', str(me.id))
            await self.db.backfill_is_outgoing(me.id)

            start_time = datetime.now()
            
            # Store last backup time in UTC at the START of backup (not when it finishes)
            last_backup_time = datetime.utcnow().isoformat() + 'Z'
            await self.db.set_metadata('last_backup_time', last_backup_time)
            
            # Get all dialogs (chats)
            logger.info("Fetching dialog list...")
            dialogs = await self._get_dialogs()
            logger.info(f"Found {len(dialogs)} total dialogs")

            # Filter dialogs based on chat type and ID filters
            # Also delete explicitly excluded chats from database
            filtered_dialogs = []
            explicitly_excluded_chat_ids = set()
            seen_chat_ids = set()  # Track which IDs we've processed from dialogs
            
            for dialog in dialogs:
                entity = dialog.entity
                # Use marked ID (with -100 prefix for channels/supergroups) to match user config
                chat_id = self._get_marked_id(entity)
                seen_chat_ids.add(chat_id)

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
            
            # Fetch explicitly included chats that weren't in dialogs
            # This handles cases where chats don't appear in the dialog list
            # (newly created, archived, or not recently messaged)
            all_include_ids = (
                self.config.global_include_ids |
                self.config.private_include_ids |
                self.config.groups_include_ids |
                self.config.channels_include_ids
            )
            missing_include_ids = all_include_ids - seen_chat_ids - explicitly_excluded_chat_ids
            
            if missing_include_ids:
                logger.info(f"Fetching {len(missing_include_ids)} explicitly included chats not in dialogs...")
                for include_id in missing_include_ids:
                    try:
                        entity = await self.client.get_entity(include_id)
                        # Create a simple dialog-like wrapper
                        class SimpleDialog:
                            def __init__(self, entity):
                                self.entity = entity
                                self.date = datetime.now()
                        
                        filtered_dialogs.append(SimpleDialog(entity))
                        logger.info(f"  → Added explicitly included chat: {self._get_chat_name(entity)} (ID: {include_id})")
                    except Exception as e:
                        logger.warning(f"  → Could not fetch included chat {include_id}: {e}")
            
            # Delete only explicitly excluded chats from database
            if explicitly_excluded_chat_ids:
                logger.info(f"Deleting {len(explicitly_excluded_chat_ids)} explicitly excluded chats from database...")
                for chat_id in explicitly_excluded_chat_ids:
                    try:
                        await self.db.delete_chat_and_related_data(chat_id, self.config.media_path)
                    except Exception as e:
                        logger.error(f"Error deleting chat {chat_id}: {e}", exc_info=True)

            logger.info(f"Backing up {len(filtered_dialogs)} dialogs after filtering")

            if not filtered_dialogs:
                logger.info("No dialogs to back up after filtering")
                return

            # Sort dialogs: priority chats first, then by most recently active
            # Priority chats (PRIORITY_CHAT_IDS) are always processed first
            # Use .timestamp() to avoid comparing timezone-aware vs naive datetimes
            # (Saved Messages chat has UTC timezone, others may be naive)
            # Fixes: https://github.com/GeiserX/Telegram-Archive/issues/12
            priority_ids = self.config.priority_chat_ids
            
            def dialog_sort_key(d):
                chat_id = self._get_marked_id(d.entity)
                is_priority = chat_id in priority_ids
                timestamp = (getattr(d, "date", None) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
                # Sort by: (not is_priority, -timestamp) so priority=True sorts first, then by recency
                return (not is_priority, -timestamp)
            
            filtered_dialogs.sort(key=dialog_sort_key)
            
            # Log priority chats if any
            if priority_ids:
                priority_count = sum(1 for d in filtered_dialogs if self._get_marked_id(d.entity) in priority_ids)
                if priority_count > 0:
                    logger.info(f"📌 {priority_count} priority chat(s) will be processed first")

            # Detect whether we've already completed at least one full backup run
            # (i.e. some chats have a non-zero last_message_id recorded)
            has_synced_before = False
            for dialog in filtered_dialogs:
                if await self.db.get_last_message_id(self._get_marked_id(dialog.entity)) > 0:
                    has_synced_before = True
                    break

            # Backup each dialog
            total_messages = 0
            for i, dialog in enumerate(filtered_dialogs, 1):
                entity = dialog.entity
                chat_id = self._get_marked_id(entity)
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
            stats = await self.db.get_statistics()
            
            # Note: last_backup_time is stored at the START of backup (see beginning of backup_all)
            
            logger.info("=" * 60)
            logger.info("Backup completed successfully!")
            logger.info(f"Duration: {duration:.2f} seconds")
            logger.info(f"New messages: {total_messages}")
            logger.info(f"Total chats: {stats['chats']}")
            logger.info(f"Total messages: {stats['messages']}")
            logger.info(f"Total media files: {stats['media_files']}")
            logger.info(f"Total storage: {stats['total_size_mb']} MB")
            logger.info("=" * 60)
            
            # Run media verification if enabled
            if self.config.verify_media:
                await self._verify_and_redownload_media()
            
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
    
    async def _verify_and_redownload_media(self) -> None:
        """
        Verify all media files on disk and re-download missing/corrupted ones.
        
        This method:
        1. Queries all media records marked as downloaded
        2. Checks if files exist on disk
        3. Optionally verifies file size matches DB record
        4. Re-downloads missing/corrupted files from Telegram
        
        Edge cases handled:
        - File missing on disk: re-download
        - File is 0 bytes: re-download (interrupted download)
        - File size mismatch: re-download (corrupted)
        - Message deleted on Telegram: log warning, skip
        - Chat inaccessible: log warning, skip chat
        - Media expired: log warning, skip
        """
        logger.info("=" * 60)
        logger.info("Starting media verification...")
        
        media_records = await self.db.get_media_for_verification()
        logger.info(f"Found {len(media_records)} media records to verify")
        
        missing_files = []
        corrupted_files = []
        
        # Phase 1: Check which files need re-downloading
        for record in media_records:
            file_path = record.get('file_path')
            if not file_path:
                continue
                
            # Check if file exists
            if not os.path.exists(file_path):
                missing_files.append(record)
                continue
            
            # Check if file is empty (interrupted download)
            if os.path.getsize(file_path) == 0:
                corrupted_files.append(record)
                continue
            
            # Check file size matches (if we have the expected size)
            expected_size = record.get('file_size')
            if expected_size and expected_size > 0:
                actual_size = os.path.getsize(file_path)
                # Allow 1% tolerance for size differences (encoding variations)
                if abs(actual_size - expected_size) > expected_size * 0.01:
                    corrupted_files.append(record)
        
        total_issues = len(missing_files) + len(corrupted_files)
        if total_issues == 0:
            logger.info("✓ All media files verified - no issues found")
            logger.info("=" * 60)
            return
        
        logger.info(f"Found {len(missing_files)} missing files, {len(corrupted_files)} corrupted files")
        logger.info("Starting re-download process...")
        
        # Phase 2: Re-download missing/corrupted files
        files_to_redownload = missing_files + corrupted_files
        
        # Group by chat_id for efficient fetching
        by_chat: Dict[int, List[Dict]] = {}
        for record in files_to_redownload:
            chat_id = record.get('chat_id')
            if chat_id:
                by_chat.setdefault(chat_id, []).append(record)
        
        redownloaded = 0
        failed = 0
        
        for chat_id, records in by_chat.items():
            try:
                # Get message IDs to fetch
                message_ids = [r['message_id'] for r in records if r.get('message_id')]
                if not message_ids:
                    continue
                
                # Fetch messages from Telegram in batch
                try:
                    messages = await self.client.get_messages(chat_id, ids=message_ids)
                except Exception as e:
                    logger.warning(f"Cannot access chat {chat_id} for media verification: {e}")
                    failed += len(records)
                    continue
                
                # Create a map of message_id -> message
                msg_map = {}
                for msg in messages:
                    if msg:  # msg can be None if message was deleted
                        msg_map[msg.id] = msg
                
                # Re-download each file
                for record in records:
                    msg_id = record.get('message_id')
                    msg = msg_map.get(msg_id)
                    
                    if not msg:
                        logger.warning(f"Message {msg_id} in chat {chat_id} was deleted - cannot recover media")
                        failed += 1
                        continue
                    
                    if not msg.media:
                        logger.warning(f"Message {msg_id} no longer has media - cannot recover")
                        failed += 1
                        continue
                    
                    try:
                        # Delete corrupted file if exists
                        file_path = record.get('file_path')
                        if file_path and os.path.exists(file_path):
                            os.remove(file_path)
                        
                        # Re-download using existing method
                        result = await self._process_media(msg, chat_id)
                        if result and result.get('downloaded'):
                            redownloaded += 1
                            logger.debug(f"Re-downloaded media for message {msg_id}")
                        else:
                            failed += 1
                            logger.warning(f"Failed to re-download media for message {msg_id}")
                    except Exception as e:
                        failed += 1
                        logger.error(f"Error re-downloading media for message {msg_id}: {e}")
                
            except Exception as e:
                logger.error(f"Error processing chat {chat_id} for media verification: {e}")
                failed += len(records)
        
        logger.info("=" * 60)
        logger.info("Media verification completed!")
        logger.info(f"Re-downloaded: {redownloaded} files")
        logger.info(f"Failed/Unrecoverable: {failed} files")
        logger.info("=" * 60)
    
    async def _backup_dialog(self, dialog) -> int:
        """
        Backup a single dialog (chat).
        
        Args:
            dialog: Dialog object from Telegram
            
        Returns:
            Number of new messages backed up
        """
        entity = dialog.entity
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_id = self._get_marked_id(entity)

        # Save chat information
        chat_data = self._extract_chat_data(entity)
        await self.db.upsert_chat(chat_data)

        # Ensure profile photos for users and groups/channels are backed up.
        # This runs on every dialog backup but only downloads new files when
        # Telegram reports a different profile photo.
        try:
            await self._ensure_profile_photo(entity, chat_id)
        except Exception as e:
            logger.error(f"Error downloading profile photo for {chat_id}: {e}", exc_info=True)
        
        # Get last synced message ID for incremental backup
        last_message_id = await self.db.get_last_message_id(chat_id)
        
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
                await self.db.insert_messages_batch(batch_data)
                # Store reactions for this batch
                for msg in batch_data:
                    if msg.get('reactions'):
                        reactions_list = []
                        for reaction in msg['reactions']:
                            # Store each user's reaction separately if we have user info
                            # Otherwise store as aggregated count
                            if reaction.get('user_ids') and len(reaction['user_ids']) > 0:
                                # We have specific users - store each one
                                for user_id in reaction['user_ids']:
                                    reactions_list.append({
                                        'emoji': reaction['emoji'],
                                        'user_id': user_id,
                                        'count': 1
                                    })
                                # If count is higher than user_ids, add remaining as anonymous
                                remaining = reaction.get('count', 0) - len(reaction['user_ids'])
                                if remaining > 0:
                                    reactions_list.append({
                                        'emoji': reaction['emoji'],
                                        'user_id': None,
                                        'count': remaining
                                    })
                            else:
                                # No user info - store as aggregated count
                                reactions_list.append({
                                    'emoji': reaction['emoji'],
                                    'user_id': None,
                                    'count': reaction.get('count', 1)
                                })
                        if reactions_list:
                            await self.db.insert_reactions(msg['id'], chat_id, reactions_list)
                total_processed += len(batch_data)
                logger.info(f"  → Processed {total_processed} messages...")
                batch_data = []
        
        # Insert remaining messages
        if batch_data:
            await self.db.insert_messages_batch(batch_data)
            # Store reactions for remaining messages
            for msg in batch_data:
                if msg.get('reactions'):
                    reactions_list = []
                    for reaction in msg['reactions']:
                        if reaction.get('user_ids') and len(reaction['user_ids']) > 0:
                            for user_id in reaction['user_ids']:
                                reactions_list.append({
                                    'emoji': reaction['emoji'],
                                    'user_id': user_id,
                                    'count': 1
                                })
                            remaining = reaction.get('count', 0) - len(reaction['user_ids'])
                            if remaining > 0:
                                reactions_list.append({
                                    'emoji': reaction['emoji'],
                                    'user_id': None,
                                    'count': remaining
                                })
                        else:
                            reactions_list.append({
                                'emoji': reaction['emoji'],
                                'user_id': None,
                                'count': reaction.get('count', 1)
                            })
                    if reactions_list:
                        await self.db.insert_reactions(msg['id'], chat_id, reactions_list)
            total_processed += len(batch_data)
            
        # Update sync status
        if messages:
            max_message_id = max(msg.id for msg in messages)
            await self.db.update_sync_status(chat_id, max_message_id, len(messages))
            
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
        local_messages = await self.db.get_messages_sync_data(chat_id)
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
                        await self.db.delete_message(chat_id, msg_id)
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
                        await self.db.update_message_text(chat_id, msg_id, remote_msg.message, remote_msg.edit_date)
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

    def _text_with_entities_to_string(self, text_obj) -> str:
        """
        Convert TextWithEntities or string to a plain string.
        
        Args:
            text_obj: TextWithEntities object or string
            
        Returns:
            Plain string representation
        """
        if text_obj is None:
            return ''
        if isinstance(text_obj, str):
            return text_obj
        if isinstance(text_obj, TextWithEntities):
            # Extract the text from TextWithEntities
            return text_obj.text if hasattr(text_obj, 'text') else str(text_obj)
        # Fallback for any other type
        return str(text_obj)

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
                await self.db.upsert_user(sender_data)
        
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
            'raw_data': {},
            'is_outgoing': 1 if message.out else 0
        }
        
        # Capture grouped_id for album detection (multiple photos/videos sent together)
        if message.grouped_id:
            message_data['raw_data']['grouped_id'] = str(message.grouped_id)
        
        # Capture forwarded message info (name of original sender)
        if message.fwd_from:
            fwd = message.fwd_from
            # fwd_from.from_name is set when forwarding from hidden users or deleted accounts
            if fwd.from_name:
                message_data['raw_data']['forward_from_name'] = fwd.from_name
            elif fwd.from_id:
                # Try to resolve the name from the entity
                try:
                    fwd_entity = await self.client.get_entity(fwd.from_id)
                    if hasattr(fwd_entity, 'title'):
                        message_data['raw_data']['forward_from_name'] = fwd_entity.title
                    elif hasattr(fwd_entity, 'first_name'):
                        name = fwd_entity.first_name or ''
                        if fwd_entity.last_name:
                            name += ' ' + fwd_entity.last_name
                        message_data['raw_data']['forward_from_name'] = name.strip()
                except Exception:
                    # Can't resolve - will fall back to ID in viewer
                    pass
        
        # Capture channel post author (signature) if available
        if hasattr(message, 'post_author') and message.post_author:
            message_data['raw_data']['post_author'] = message.post_author
        
        # Get reply text if this is a reply
        if message.reply_to_msg_id and message.reply_to:
            reply_msg = message.reply_to
            if hasattr(reply_msg, 'message'):
                # Truncate to first 100 chars like Telegram does
                reply_text = (reply_msg.message or '')[:100]
                message_data['reply_to_text'] = reply_text
        
        # Handle media
        if message.media:
            # Handle Polls specially (store structure in raw_data, do not download)
            if isinstance(message.media, MessageMediaPoll):
                message_data['media_type'] = 'poll'
                poll = message.media.poll
                results = message.media.results
                
                # Parse results if available
                results_data = None
                if results:
                    try:
                        results_list = []
                        if results.results:
                            for r in results.results:
                                results_list.append({
                                    'option': base64.b64encode(r.option).decode('ascii'),
                                    'voters': r.voters,
                                    'correct': r.correct
                                })
                        results_data = {
                            'total_voters': results.total_voters,
                            'results': results_list
                        }
                    except Exception as e:
                        logger.warning(f"Error parsing poll results: {e}")

                # Store poll structure
                # Convert TextWithEntities to strings for JSON serialization
                question_text = self._text_with_entities_to_string(getattr(poll, 'question', ''))
                message_data['raw_data']['poll'] = {
                    'id': getattr(poll, 'id', None),
                    'question': question_text,
                    'answers': [{
                        'text': self._text_with_entities_to_string(getattr(a, 'text', '')), 
                        'option': base64.b64encode(a.option).decode('ascii')
                    } for a in poll.answers],
                    'closed': poll.closed,
                    'public_voters': poll.public_voters,
                    'multiple_choice': poll.multiple_choice,
                    'quiz': poll.quiz,
                    'results': results_data
                }

            elif self.config.download_media:
                media_info = await self._process_media(message, chat_id)
                if media_info:
                    message_data['media_type'] = media_info['type']
                    message_data['media_id'] = media_info['id']
                    message_data['media_path'] = media_info.get('file_path')
        
        # Extract reactions if available
        reactions_data = []
        if hasattr(message, 'reactions') and message.reactions:
            try:
                # Check if reactions.results exists (MessageReactions object)
                if hasattr(message.reactions, 'results') and message.reactions.results:
                    for reaction in message.reactions.results:
                        emoji = reaction.reaction
                        # Handle both emoji strings and ReactionEmoji objects
                        if hasattr(emoji, 'emoticon'):
                            emoji_str = emoji.emoticon
                        elif hasattr(emoji, 'document_id'):
                            # Custom emoji (animated sticker) - use document_id as identifier
                            emoji_str = f"custom_{emoji.document_id}"
                        else:
                            emoji_str = str(emoji)
                        
                        # Get user IDs who reacted (if available)
                        user_ids = []
                        if hasattr(reaction, 'recent_reactions') and reaction.recent_reactions:
                            for recent in reaction.recent_reactions:
                                if hasattr(recent, 'peer_id'):
                                    peer = recent.peer_id
                                    if hasattr(peer, 'user_id'):
                                        user_ids.append(peer.user_id)
                                    elif hasattr(peer, 'channel_id'):
                                        user_ids.append(peer.channel_id)
                        
                        reactions_data.append({
                            'emoji': emoji_str,
                            'count': reaction.count,
                            'user_ids': user_ids
                        })
                    
                    if reactions_data:
                        logger.debug(f"Extracted {len(reactions_data)} reactions for message {message.id}")
            except Exception as e:
                logger.warning(f"Error extracting reactions for message {message.id}: {e}")
                import traceback
                logger.debug(traceback.format_exc())
        
        # Store reactions separately (will be called after message is inserted)
        message_data['reactions'] = reactions_data
        
        # Return message data for batch processing
        return message_data

    async def _ensure_profile_photo(self, entity, marked_id: int = None) -> None:
        """
        Download the current profile photo for users and chats.

        Downloads the profile photo on every backup run to ensure avatars
        stay up-to-date. Files are named `<chat_id>_<photo_id>.jpg` so the
        viewer can pick the freshest version.
        
        Args:
            entity: Telegram entity (User, Chat, Channel)
            marked_id: The marked chat ID (negative for groups/channels) for consistent file naming
        """
        file_id = marked_id if marked_id is not None else self._get_marked_id(entity)
        avatar_path, _legacy_path = get_avatar_paths(self.config.media_path, entity, file_id)

        # Nothing to download (no avatar set)
        if avatar_path is None:
            logger.debug(f"No avatar available for {file_id}")
            return

        try:
            # Avoid redundant downloads when we already have the current photo
            needs_download = (
                not os.path.exists(avatar_path) or os.path.getsize(avatar_path) == 0
            )

            if not needs_download:
                return

            result = await self.client.download_profile_photo(
                entity, 
                file=avatar_path,
                download_big=False  # Small size is usually sufficient
            )
            if result:
                logger.info(f"📷 Avatar downloaded: {avatar_path}")
        except Exception as e:
            logger.warning(f"Failed to download avatar for {file_id}: {e}")
    
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
        
        # Check file size (estimated)
        file_size = self._get_media_size(media)
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
        
        # Download media (with optional global deduplication)
        try:
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)
            
            # Generate filename using file_id for automatic deduplication
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)
            
            # Check if deduplication is enabled
            if getattr(self.config, 'deduplicate_media', True):
                # Global deduplication: use _shared directory for actual files
                shared_dir = os.path.join(self.config.media_path, '_shared')
                os.makedirs(shared_dir, exist_ok=True)
                shared_file_path = os.path.join(shared_dir, file_name)
                
                # Check if file already exists (either directly or in shared)
                if not os.path.exists(file_path):
                    if os.path.exists(shared_file_path):
                        # File exists in shared - create symlink
                        try:
                            # Use relative symlink for portability
                            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
                            os.symlink(rel_path, file_path)
                            logger.debug(f"Created symlink for deduplicated media: {file_name}")
                        except OSError as e:
                            # Symlink failed (e.g., Windows), copy reference instead
                            logger.warning(f"Symlink failed, downloading copy: {e}")
                            await self.client.download_media(message, file_path)
                    else:
                        # First time seeing this file - download to shared and create symlink
                        await self.client.download_media(message, shared_file_path)
                        logger.debug(f"Downloaded media to shared: {file_name}")
                        
                        # Create symlink in chat directory
                        try:
                            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
                            os.symlink(rel_path, file_path)
                        except OSError as e:
                            # Symlink failed - move file to chat dir instead
                            logger.warning(f"Symlink failed, using direct path: {e}")
                            import shutil
                            shutil.move(shared_file_path, file_path)
                
                # Update file_size with actual size from disk (follow symlinks)
                actual_path = shared_file_path if os.path.exists(shared_file_path) else file_path
                if os.path.exists(actual_path):
                    file_size = os.path.getsize(actual_path)
            else:
                # No deduplication - download directly to chat directory
                if not os.path.exists(file_path):
                    await self.client.download_media(message, file_path)
                    logger.debug(f"Downloaded media: {file_name}")
                
                # Update file_size with actual size from disk
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)

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
            await self.db.insert_media(media_data)
            
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
    
    def _get_media_size(self, media) -> int:
        """Get estimated size of media object in bytes."""
        # Document (Video, Audio, File)
        if hasattr(media, 'document') and media.document:
            return getattr(media.document, 'size', 0)
        
        # Photo (find largest size)
        if hasattr(media, 'photo') and media.photo:
            sizes = getattr(media.photo, 'sizes', [])
            if sizes:
                # Return size of the last one (usually the largest)
                # Some Size types have 'size' field, others don't (like PhotoCachedSize)
                largest = sizes[-1]
                return getattr(largest, 'size', 0)
        
        # Fallback to direct attribute
        return getattr(media, 'size', 0)

    def _get_media_type(self, media) -> Optional[str]:
        """Get media type as string."""
        if isinstance(media, MessageMediaPhoto):
            return 'photo'
        elif isinstance(media, MessageMediaDocument):
            # Check document attributes to determine specific type
            if hasattr(media, 'document') and media.document:
                is_animated = False
                for attr in media.document.attributes:
                    attr_type = type(attr).__name__
                    if 'Animated' in attr_type:
                        is_animated = True
                    if 'Video' in attr_type:
                        # If animated, it's a GIF
                        return 'animation' if is_animated else 'video'
                    elif 'Audio' in attr_type:
                        # Voice notes have .voice=True on DocumentAttributeAudio
                        if hasattr(attr, 'voice') and attr.voice:
                            return 'voice'
                        return 'audio'
                    elif 'Sticker' in attr_type:
                        return 'sticker'
                # If animated but no video attribute, still an animation
                if is_animated:
                    return 'animation'
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
        # Use marked ID (with -100 prefix for channels/supergroups) for consistency
        chat_data = {'id': self._get_marked_id(entity)}
        
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


async def run_backup(
    config: Config,
    client: Optional[TelegramClient] = None
):
    """
    Run a single backup operation.

    Args:
        config: Configuration object
        client: Optional existing TelegramClient to use (for shared connection).
               If provided, the backup will use this client instead of creating
               its own, avoiding session file lock conflicts.
    """
    backup = await TelegramBackup.create(config, client=client)
    try:
        await backup.connect()
        await backup.backup_all()
    finally:
        await backup.disconnect()
        await backup.db.close()


if __name__ == '__main__':
    # Test backup
    import asyncio
    from .config import Config, setup_logging
    
    config = Config()
    setup_logging(config)
    
    asyncio.run(run_backup(config))
