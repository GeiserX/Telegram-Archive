"""
Real-time event listener for Telegram message edits and deletions.
Catches events as they happen and updates the local database immediately.

Safety features:
- LISTEN_EDITS: Apply text edits (default: true, safe)
- LISTEN_DELETIONS: Delete messages (default: true, protected by zero-footprint)
- Mass operation detection: Blocks bulk edits/deletions to protect data

ZERO-FOOTPRINT PROTECTION:
When mass operations are detected, NO changes are written to the database.
Operations are buffered and only applied after a safety delay, ensuring
that burst attacks are caught BEFORE any data is modified.
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Set, Deque, Tuple, List, Dict, Any

import os
from telethon import TelegramClient, events
from telethon.tl.types import User, MessageMediaPhoto, MessageMediaDocument, MessageMediaContact, MessageMediaGeo, MessageMediaPoll
from telethon.utils import get_peer_id

from .config import Config
from .db import DatabaseAdapter, create_adapter
from .realtime import RealtimeNotifier, NotificationType

logger = logging.getLogger(__name__)


class MassOperationProtector:
    """
    Rate-limiting protection against mass deletions/edits.
    
    HOW IT WORKS:
    - Uses a sliding time window to count operations per chat
    - Operations are applied IMMEDIATELY if under threshold
    - Once threshold exceeded, chat is blocked for remainder of window
    
    PARAMETERS:
    - THRESHOLD (default 10): Max operations allowed in the time window
    - WINDOW_SECONDS (default 30): Sliding time window for counting operations
    
    EXAMPLE:
    - User deletes 2 messages → both applied immediately ✓
    - User deletes 10 messages over 30s → all applied ✓  
    - Attacker deletes 50 messages in 10s → first 10 applied, remaining 40 blocked ✓
    
    This provides RATE LIMITING - normal usage works, mass attacks are capped.
    For zero deletions from backup, disable LISTEN_DELETIONS entirely.
    """
    
    def __init__(
        self, 
        threshold: int = 10,
        window_seconds: int = 30,
        buffer_delay_seconds: float = 2.0  # DEPRECATED: kept for config compatibility
    ):
        """
        Args:
            threshold: Max operations allowed per chat in the time window
            window_seconds: Sliding window for counting operations
            buffer_delay_seconds: DEPRECATED - no longer used, operations apply immediately
        """
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.window = timedelta(seconds=window_seconds)
        
        # Operation history for sliding window: {chat_id: deque of timestamps}
        self._operation_history: Dict[int, Deque[datetime]] = {}
        
        # Blocked chats: {chat_id: (blocked_until, reason, blocked_count)}
        self._blocked: Dict[int, Tuple[datetime, str, int]] = {}
        
        self._running = False
        
        # Statistics
        self.stats = {
            'operations_applied': 0,
            'operations_blocked': 0,
            'rate_limits_triggered': 0,
            'chats_rate_limited': set()
        }
    
    def start(self):
        """Start the protector."""
        self._running = True
        logger.info(f"🛡️ Rate limiter active: max {self.threshold} ops per {self.window_seconds}s per chat")
    
    async def stop(self):
        """Stop the protector."""
        self._running = False
    
    def is_blocked(self, chat_id: int) -> Tuple[bool, str]:
        """Check if a chat is currently rate-limited."""
        if chat_id in self._blocked:
            blocked_until, reason, _ = self._blocked[chat_id]
            if datetime.now() < blocked_until:
                return True, reason
            else:
                # Block expired
                del self._blocked[chat_id]
                logger.info(f"🔓 Rate limit expired for chat {chat_id}")
        return False, ""
    
    def _count_ops_in_window(self, chat_id: int) -> int:
        """Count operations in the sliding time window for a chat."""
        if chat_id not in self._operation_history:
            return 0
        
        now = datetime.now()
        cutoff = now - self.window
        
        # Clean old entries and count
        history = self._operation_history[chat_id]
        while history and history[0] < cutoff:
            history.popleft()
        
        return len(history)
    
    def _record_operation(self, chat_id: int):
        """Record an operation timestamp for sliding window tracking."""
        if chat_id not in self._operation_history:
            self._operation_history[chat_id] = deque()
        self._operation_history[chat_id].append(datetime.now())
    
    def check_operation(self, chat_id: int, operation_type: str) -> Tuple[bool, str]:
        """
        Check if an operation should be allowed.
        
        Returns (allowed, reason):
            - (True, "allowed") if operation can proceed
            - (False, reason) if chat is rate-limited
        """
        # Check if already blocked
        blocked, reason = self.is_blocked(chat_id)
        if blocked:
            self.stats['operations_blocked'] += 1
            return False, f"RATE LIMITED: {reason}"
        
        # Record this operation
        self._record_operation(chat_id)
        
        # Check sliding window
        ops_in_window = self._count_ops_in_window(chat_id)
        
        if ops_in_window > self.threshold:
            # Rate limit triggered - block further operations
            block_until = datetime.now() + self.window
            reason = f"Rate limit: {ops_in_window} {operation_type}s in {self.window_seconds}s (max: {self.threshold})"
            self._blocked[chat_id] = (block_until, reason, ops_in_window - self.threshold)
            
            # Update stats
            self.stats['rate_limits_triggered'] += 1
            self.stats['operations_blocked'] += 1
            self.stats['chats_rate_limited'].add(chat_id)
            
            logger.warning("=" * 70)
            logger.warning(f"🛡️ RATE LIMIT TRIGGERED")
            logger.warning(f"   Chat: {chat_id}")
            logger.warning(f"   Operation type: {operation_type}")
            logger.warning(f"   Operations in {self.window_seconds}s: {ops_in_window} (max: {self.threshold})")
            logger.warning(f"   First {self.threshold} were applied, remaining blocked")
            logger.warning(f"   Chat blocked until: {block_until}")
            logger.warning("=" * 70)
            
            return False, reason
        
        # Operation allowed
        self.stats['operations_applied'] += 1
        return True, "allowed"
    
    def get_stats(self) -> Dict[str, Any]:
        """Get rate limiter statistics."""
        return {
            'operations_applied': self.stats['operations_applied'],
            'operations_blocked': self.stats['operations_blocked'],
            'rate_limits_triggered': self.stats['rate_limits_triggered'],
            'chats_rate_limited': len(self.stats['chats_rate_limited']),
            'currently_blocked': len([c for c in self._blocked if datetime.now() < self._blocked[c][0]])
        }
    
    def get_blocked_chats(self) -> Dict[int, Tuple[str, int]]:
        """Get currently rate-limited chats."""
        now = datetime.now()
        return {
            chat_id: (reason, blocked_count)
            for chat_id, (blocked_until, reason, blocked_count) in self._blocked.items()
            if now < blocked_until
        }


class TelegramListener:
    """
    Real-time event listener for Telegram.
    
    Catches message edits and deletions as they happen and updates the database.
    Designed to run alongside the scheduled backup process.
    
    RATE LIMITING PROTECTION:
    Uses a sliding window to limit operations per chat. Normal usage (deleting
    a few messages) works instantly. Mass operations (deleting 50+ messages)
    are blocked after the threshold, protecting most of your backup.
    
    Example: threshold=10, window=30s
    - Delete 2 messages → both applied ✓
    - Delete 50 messages in 10s → first 10 applied, remaining 40 blocked ✓
    
    Safety features:
    - LISTEN_EDITS: Only sync edits if enabled (default: true)
    - LISTEN_DELETIONS: Sync deletions with rate limiting (default: true)
    - For zero deletions from backup, set LISTEN_DELETIONS=false
    """
    
    def __init__(
        self, 
        config: Config, 
        db: DatabaseAdapter,
        client: Optional[TelegramClient] = None
    ):
        """
        Initialize the listener.
        
        Args:
            config: Configuration object
            db: Database adapter (must be initialized)
            client: Optional existing TelegramClient to use (for shared connection).
                   If not provided, will create a new client in connect().
        """
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.client: Optional[TelegramClient] = client
        self._owns_client = client is None  # Track if we created the client
        self._running = False
        self._tracked_chat_ids: Set[int] = set()
        
        # Zero-footprint mass operation protection
        self._protector = MassOperationProtector(
            threshold=config.mass_operation_threshold,
            window_seconds=config.mass_operation_window_seconds,
            buffer_delay_seconds=config.mass_operation_buffer_delay
        )
        
        # Background task for processing buffered operations
        self._processor_task: Optional[asyncio.Task] = None
        
        # Real-time notifier for viewer WebSocket updates
        self._notifier: Optional[RealtimeNotifier] = None
        
        # Statistics
        self.stats = {
            'edits_received': 0,
            'edits_applied': 0,
            'deletions_received': 0,
            'deletions_applied': 0,
            'deletions_skipped': 0,  # Skipped due to LISTEN_DELETIONS=false
            'new_messages_received': 0,
            'new_messages_saved': 0,
            'bursts_intercepted': 0,
            'operations_discarded': 0,
            'errors': 0,
            'start_time': None
        }
        
        # Log safety settings
        logger.info("=" * 70)
        logger.info("🛡️ TelegramListener initialized with ZERO-FOOTPRINT PROTECTION")
        logger.info("=" * 70)
        logger.info(f"  LISTEN_EDITS: {config.listen_edits}")
        if config.listen_deletions:
            logger.warning(f"  ⚠️ LISTEN_DELETIONS: true - Deletions will be processed (with protection)")
        else:
            logger.info(f"  LISTEN_DELETIONS: false (backup fully protected)")
        if config.listen_new_messages:
            logger.info(f"  LISTEN_NEW_MESSAGES: true - New messages saved in real-time!")
            if config.listen_new_messages_media:
                logger.info(f"  LISTEN_NEW_MESSAGES_MEDIA: true - Media downloaded immediately!")
            else:
                logger.info(f"  LISTEN_NEW_MESSAGES_MEDIA: false (media on scheduled backup)")
        else:
            logger.info(f"  LISTEN_NEW_MESSAGES: false (saved on scheduled backup)")
        logger.info(f"  Protection threshold: {config.mass_operation_threshold} ops triggers block")
        logger.info(f"  Protection window: {config.mass_operation_window_seconds}s")
        logger.info(f"  Buffer delay: {config.mass_operation_buffer_delay}s (operations held before applying)")
        logger.info("=" * 70)
    
    @classmethod
    async def create(
        cls, 
        config: Config,
        client: Optional[TelegramClient] = None
    ) -> "TelegramListener":
        """
        Factory method to create TelegramListener with initialized database.
        
        Args:
            config: Configuration object
            client: Optional existing TelegramClient to use (for shared connection)
            
        Returns:
            Initialized TelegramListener instance
        """
        db = await create_adapter()
        return cls(config, db, client=client)
    
    async def connect(self) -> None:
        """
        Connect to Telegram and set up event handlers.
        
        If a client was provided in __init__, verifies it's connected.
        Otherwise, creates a new client and connects.
        """
        # If using shared client, just verify it's connected
        if self.client is not None and not self._owns_client:
            if not self.client.is_connected():
                raise RuntimeError("Shared client is not connected")
            me = await self.client.get_me()
            logger.info(f"Connected as {me.first_name} ({me.phone})")
        else:
            # Create new client
            self.client = TelegramClient(
                self.config.session_path,
                self.config.api_id,
                self.config.api_hash
            )
            self._owns_client = True
            
            # Connect and authenticate
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.error("❌ Session not authorized!")
                logger.error("Please run the authentication setup first.")
                raise RuntimeError("Session not authorized. Please run authentication setup.")
            
            me = await self.client.get_me()
            logger.info(f"Connected as {me.first_name} ({me.phone})")
        
        # Load tracked chat IDs from database
        await self._load_tracked_chats()
        
        # Initialize real-time notifier (auto-detects PostgreSQL vs SQLite)
        from .db import get_db_manager
        db_manager_instance = await get_db_manager()
        self._notifier = RealtimeNotifier(db_manager_instance)
        await self._notifier.init()
        logger.info("Real-time notifier initialized")
        
        # Register event handlers
        self._register_handlers()
        
        logger.info("Event handlers registered")
    
    async def _load_tracked_chats(self) -> None:
        """Load list of chat IDs we're backing up (to filter events)."""
        try:
            chats = await self.db.get_all_chats()
            self._tracked_chat_ids = {chat['id'] for chat in chats}
            logger.info(f"Tracking {len(self._tracked_chat_ids)} chats for real-time updates")
        except Exception as e:
            logger.warning(f"Could not load tracked chats: {e}")
            self._tracked_chat_ids = set()
    
    def _get_marked_id(self, entity_or_peer) -> int:
        """
        Get the marked ID for an entity (with -100 prefix for channels/supergroups).
        """
        try:
            return get_peer_id(entity_or_peer)
        except Exception:
            # Fallback for raw IDs
            if hasattr(entity_or_peer, 'id'):
                return entity_or_peer.id
            return entity_or_peer
    
    async def _notify_update(self, notification_type: str, data: dict) -> None:
        """
        Send a real-time notification to the viewer.
        
        Args:
            notification_type: Type of notification ('edit', 'delete', 'new_message')
            data: Notification data (must include 'chat_id')
        """
        if self._notifier is None:
            return
        
        try:
            from .realtime import NotificationType
            
            # Map string types to enum
            type_map = {
                'edit': NotificationType.EDIT,
                'delete': NotificationType.DELETE,
                'new_message': NotificationType.NEW_MESSAGE,
            }
            
            nt = type_map.get(notification_type)
            if nt is None:
                logger.warning(f"Unknown notification type: {notification_type}")
                return
            
            chat_id = data.get('chat_id', 0)
            await self._notifier.notify(nt, chat_id, data)
        except Exception as e:
            logger.debug(f"Failed to send notification: {e}")
    
    def _should_process_chat(self, chat_id: int) -> bool:
        """
        Check if we should process events for this chat.
        
        Returns True if:
        - Chat is in our tracked list (backed up at least once), OR
        - Chat matches our backup filters (include/exclude lists, chat types)
        """
        # First, check if it's in our tracked chats
        if chat_id in self._tracked_chat_ids:
            return True
        
        # If not tracked yet, check if it would be backed up based on config
        # We can't determine chat type without fetching the entity, so be conservative
        # and only process if it's in an explicit include list
        if chat_id in self.config.global_include_ids:
            return True
        if chat_id in self.config.private_include_ids:
            return True
        if chat_id in self.config.groups_include_ids:
            return True
        if chat_id in self.config.channels_include_ids:
            return True
        
        return False
    
    def _get_chat_type(self, entity) -> str:
        """Determine chat type from Telethon entity."""
        from telethon.tl.types import User as TelethonUser, Chat as TelethonChat, Channel
        
        if isinstance(entity, TelethonUser):
            return 'private'
        elif isinstance(entity, TelethonChat):
            return 'group'
        elif isinstance(entity, Channel):
            return 'channel' if not entity.megagroup else 'group'
        return 'unknown'
    
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
                        return 'animation' if is_animated else 'video'
                    elif 'Audio' in attr_type:
                        if hasattr(attr, 'voice') and attr.voice:
                            return 'voice'
                        return 'audio'
                    elif 'Sticker' in attr_type:
                        return 'sticker'
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
    
    def _get_media_filename(self, message, media_type: str, telegram_file_id: Optional[str] = None) -> str:
        """Generate a filename for media."""
        # Try to get original filename from document
        if hasattr(message.media, 'document') and message.media.document:
            for attr in message.media.document.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    # Use Telegram file ID + original name for deduplication
                    if telegram_file_id:
                        return f"{telegram_file_id}_{attr.file_name}"
                    return attr.file_name
        
        # Generate filename based on type
        extensions = {
            'photo': '.jpg',
            'video': '.mp4',
            'animation': '.mp4',
            'voice': '.ogg',
            'audio': '.mp3',
            'sticker': '.webp',
            'document': ''
        }
        ext = extensions.get(media_type, '')
        
        if telegram_file_id:
            return f"{telegram_file_id}{ext}"
        return f"{message.id}_{media_type}{ext}"
    
    async def _download_media(self, message, chat_id: int) -> Optional[str]:
        """
        Download media from a message.
        
        Returns the file path if successful, None otherwise.
        """
        media = message.media
        media_type = self._get_media_type(media)
        
        if not media_type or media_type in ('contact', 'geo', 'poll'):
            return None  # These don't have downloadable files
        
        try:
            # Get Telegram's file unique ID for deduplication
            telegram_file_id = None
            if hasattr(media, 'photo'):
                telegram_file_id = str(getattr(media.photo, 'id', None))
            elif hasattr(media, 'document'):
                telegram_file_id = str(getattr(media.document, 'id', None))
            
            # Check file size
            file_size = 0
            if hasattr(media, 'document') and media.document:
                file_size = getattr(media.document, 'size', 0)
            elif hasattr(media, 'photo') and media.photo:
                if hasattr(media.photo, 'sizes') and media.photo.sizes:
                    largest = max(media.photo.sizes, key=lambda s: getattr(s, 'size', 0), default=None)
                    if largest:
                        file_size = getattr(largest, 'size', 0)
            
            max_size = self.config.get_max_media_size_bytes()
            if file_size > max_size:
                logger.debug(f"Skipping large media file: {file_size / 1024 / 1024:.2f} MB")
                return None
            
            # Create chat-specific media directory
            chat_media_dir = os.path.join(self.config.media_path, str(chat_id))
            os.makedirs(chat_media_dir, exist_ok=True)
            
            # Generate filename
            file_name = self._get_media_filename(message, media_type, telegram_file_id)
            file_path = os.path.join(chat_media_dir, file_name)
            
            # Download if not already exists
            if not os.path.exists(file_path):
                await self.client.download_media(message, file_path)
            
            # Return the path as stored in DB (relative to media root)
            return f"{self.config.media_path}/{chat_id}/{file_name}"
            
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return None
    
    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""
        
        @self.client.on(events.MessageEdited)
        async def on_message_edited(event: events.MessageEdited.Event) -> None:
            """
            Handle message edit events.
            
            Operations are QUEUED, not applied immediately.
            The background processor applies them after the buffer delay,
            allowing burst detection BEFORE any data is modified.
            """
            # Check if edits are enabled
            if not self.config.listen_edits:
                return
                
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                if not self._should_process_chat(chat_id):
                    return
                
                self.stats['edits_received'] += 1
                
                message = event.message
                new_text = message.text or ''
                edit_date = message.edit_date
                
                # Check rate limit before applying
                allowed, reason = self._protector.check_operation(chat_id, 'edit')
                
                if not allowed:
                    self.stats['operations_discarded'] += 1
                    return
                
                # Apply the edit immediately
                await self.db.update_message_text(
                    chat_id=chat_id,
                    message_id=message.id,
                    new_text=new_text,
                    edit_date=edit_date
                )
                self.stats['edits_applied'] += 1
                logger.debug(f"📝 Edit applied: chat={chat_id} msg={message.id}")
                
                # Notify viewer of the update
                await self._notify_update('edit', {
                    'chat_id': chat_id,
                    'message_id': message.id,
                    'new_text': new_text,
                    'edit_date': edit_date.isoformat() if edit_date else None
                })
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error processing edit event: {e}", exc_info=True)
        
        @self.client.on(events.MessageDeleted)
        async def on_message_deleted(event: events.MessageDeleted.Event) -> None:
            """
            Handle message deletion events.
            
            Rate-limited: if too many deletions occur in a short time,
            further deletions are blocked to protect the backup.
            """
            # Check if deletions are enabled (DEFAULT: TRUE with rate limiting)
            if not self.config.listen_deletions:
                if event.deleted_ids:
                    self.stats['deletions_skipped'] += len(event.deleted_ids)
                    logger.debug(f"⏭️ Deletion skipped (LISTEN_DELETIONS=false): {len(event.deleted_ids)} messages")
                return
            
            try:
                # Note: event.chat_id might be None for some deletion events
                chat_id = event.chat_id
                if chat_id is not None:
                    chat_id = self._get_marked_id(chat_id)
                    
                    if not self._should_process_chat(chat_id):
                        return
                
                # Process each deletion
                for msg_id in event.deleted_ids:
                    self.stats['deletions_received'] += 1
                    
                    # If chat_id is unknown, try to look it up from the database
                    effective_chat_id = chat_id
                    if effective_chat_id is None:
                        try:
                            effective_chat_id = await self.db.get_chat_id_for_message(msg_id)
                            if effective_chat_id:
                                logger.debug(f"🔍 Resolved chat_id={effective_chat_id} for msg={msg_id} from database")
                        except Exception as e:
                            logger.debug(f"Could not look up chat for msg {msg_id}: {e}")
                    
                    if effective_chat_id is not None:
                        if not self._should_process_chat(effective_chat_id):
                            continue
                        
                        # Check rate limit before applying
                        allowed, reason = self._protector.check_operation(effective_chat_id, 'deletion')
                        
                        if not allowed:
                            self.stats['operations_discarded'] += 1
                            continue
                        
                        # Apply the deletion immediately
                        await self.db.delete_message(effective_chat_id, msg_id)
                        self.stats['deletions_applied'] += 1
                        logger.debug(f"🗑️ Deletion applied: chat={effective_chat_id} msg={msg_id}")
                        
                        # Notify viewer of the deletion
                        await self._notify_update('delete', {
                            'chat_id': effective_chat_id,
                            'message_id': msg_id
                        })
                    else:
                        logger.debug(f"⚠️ Deletion skipped (unknown chat): msg={msg_id}")
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error processing deletion event: {e}", exc_info=True)
        
        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            """
            Handle new messages.
            
            If LISTEN_NEW_MESSAGES is enabled, saves messages to database in real-time.
            Otherwise, just tracks chat IDs for edits/deletions.
            """
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                # Add to tracked chats if we should be backing up this chat
                if chat_id not in self._tracked_chat_ids:
                    if self._should_process_chat(chat_id):
                        self._tracked_chat_ids.add(chat_id)
                        logger.debug(f"Added chat {chat_id} to tracking list")
                
                # Skip if not in tracked chats
                if not self._should_process_chat(chat_id):
                    return
                
                self.stats['new_messages_received'] += 1
                
                # If LISTEN_NEW_MESSAGES is disabled, we're done
                if not self.config.listen_new_messages:
                    return
                
                # Save the message to database
                message = event.message
                
                # Ensure chat exists in database (prevents FK violation for new chats)
                chat_entity = await event.get_chat()
                if chat_entity:
                    chat_data = {
                        'id': chat_id,
                        'type': self._get_chat_type(chat_entity),
                        'title': getattr(chat_entity, 'title', None),
                        'username': getattr(chat_entity, 'username', None),
                        'first_name': getattr(chat_entity, 'first_name', None),
                        'last_name': getattr(chat_entity, 'last_name', None),
                    }
                    await self.db.upsert_chat(chat_data)
                
                # Save sender information if available
                if message.sender and isinstance(message.sender, User):
                    user_data = {
                        'id': message.sender.id,
                        'username': message.sender.username,
                        'first_name': message.sender.first_name,
                        'last_name': message.sender.last_name,
                        'phone': message.sender.phone,
                        'is_bot': message.sender.bot
                    }
                    await self.db.upsert_user(user_data)
                
                # Extract message data
                message_data = {
                    'id': message.id,
                    'chat_id': chat_id,
                    'sender_id': message.sender_id,
                    'date': message.date,
                    'text': message.text or '',
                    'reply_to_msg_id': message.reply_to_msg_id if hasattr(message, 'reply_to_msg_id') else None,
                    'reply_to_text': None,
                    'forward_from_id': None,  # Will be filled by next backup if needed
                    'edit_date': message.edit_date,
                    'media_type': None,
                    'media_id': None,
                    'media_path': None,
                    'raw_data': {},
                    'is_outgoing': 1 if message.out else 0
                }
                
                # Handle media if present
                if message.media:
                    media_type = self._get_media_type(message.media)
                    if media_type:
                        message_data['media_type'] = media_type
                        message_data['media_id'] = f"{chat_id}_{message.id}_{media_type}"
                        
                        # Download media immediately if enabled
                        if self.config.listen_new_messages_media and self.config.download_media:
                            try:
                                media_path = await self._download_media(message, chat_id)
                                if media_path:
                                    message_data['media_path'] = media_path
                                    logger.debug(f"📎 Downloaded media: {media_path}")
                            except Exception as e:
                                logger.warning(f"Failed to download media for message {message.id}: {e}")
                
                # Insert the message
                await self.db.insert_message(message_data)
                self.stats['new_messages_saved'] += 1
                
                # Send real-time notification
                if self._notifier:
                    await self._notifier.notify(
                        NotificationType.NEW_MESSAGE,
                        chat_id,
                        {'message': message_data}
                    )
                
                # Log the new message (truncate text for logging)
                text_preview = (message.text or '')[:50]
                if len(message.text or '') > 50:
                    text_preview += '...'
                media_indicator = f" [{message_data['media_type']}]" if message_data['media_type'] else ""
                logger.info(f"📩 New message saved: chat={chat_id} msg={message.id}{media_indicator} text='{text_preview}'")
                        
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error in new message handler: {e}", exc_info=True)
        
        # ChatAction handler - tracks chat metadata changes
        @self.client.on(events.ChatAction)
        async def on_chat_action(event: events.ChatAction.Event) -> None:
            """
            Handle chat action events (photo changes, member joins/leaves, title changes).
            
            Only active if LISTEN_CHAT_ACTIONS is enabled.
            """
            if not self.config.listen_chat_actions:
                return
            
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                if not self._should_process_chat(chat_id):
                    return
                
                # Track stats
                if 'chat_actions' not in self.stats:
                    self.stats['chat_actions'] = 0
                self.stats['chat_actions'] += 1
                
                action_type = None
                if event.new_photo:
                    action_type = 'photo_changed'
                    logger.info(f"📷 Chat photo changed: chat={chat_id}")
                elif getattr(event, 'photo', None) is None and not event.new_photo:
                    # Photo removed - Telethon doesn't have photo_removed attr in all versions
                    action_type = 'photo_removed'
                    logger.info(f"📷 Chat photo removed: chat={chat_id}")
                elif event.new_title:
                    action_type = 'title_changed'
                    logger.info(f"📝 Chat title changed to '{event.new_title}': chat={chat_id}")
                elif event.user_joined:
                    action_type = 'user_joined'
                    logger.debug(f"👤 User joined: chat={chat_id}")
                elif event.user_left:
                    action_type = 'user_left'
                    logger.debug(f"👤 User left: chat={chat_id}")
                elif event.user_added:
                    action_type = 'user_added'
                    logger.debug(f"👤 User added: chat={chat_id}")
                elif event.user_kicked:
                    action_type = 'user_kicked'
                    logger.debug(f"👤 User kicked: chat={chat_id}")
                
                # Update chat info if photo or title changed
                if action_type in ('photo_changed', 'title_changed'):
                    # Get full entity for update
                    try:
                        entity = await self.client.get_entity(chat_id)
                        if entity:
                            # Update chat in database
                            chat_data = {
                                'id': chat_id,
                                'type': 'channel' if hasattr(entity, 'broadcast') else 'group',
                                'title': getattr(entity, 'title', None),
                                'username': getattr(entity, 'username', None),
                            }
                            await self.db.upsert_chat(chat_data)
                            logger.info(f"✅ Chat {chat_id} metadata updated")
                    except Exception as e:
                        logger.warning(f"Failed to update chat metadata for {chat_id}: {e}")
                        
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error in chat action handler: {e}", exc_info=True)
        
        # Album handler - groups media uploads together
        @self.client.on(events.Album)
        async def on_album(event: events.Album.Event) -> None:
            """
            Handle album events (grouped photos/videos).
            
            Only active if LISTEN_ALBUMS is enabled.
            Albums are groups of photos/videos sent together.
            """
            if not self.config.listen_albums:
                return
            
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                if not self._should_process_chat(chat_id):
                    return
                
                # Track stats
                if 'albums_received' not in self.stats:
                    self.stats['albums_received'] = 0
                self.stats['albums_received'] += 1
                
                album_size = len(event.messages)
                logger.info(f"📸 Album received: chat={chat_id} size={album_size}")
                
                # If LISTEN_NEW_MESSAGES is enabled, save each message in the album
                if self.config.listen_new_messages:
                    for message in event.messages:
                        # Get actual media type (photo/video) instead of generic 'album'
                        media_type = self._get_media_type(message.media) if message.media else None
                        media_path = None
                        media_id = None
                        
                        # Download media if enabled
                        if media_type and self.config.listen_new_messages_media and self.config.download_media:
                            try:
                                media_path = await self._download_media(message, chat_id)
                                if media_path:
                                    media_id = f"{chat_id}_{message.id}_{media_type}"
                                    logger.debug(f"📎 Downloaded album media: {media_path}")
                            except Exception as e:
                                logger.warning(f"Failed to download album media for message {message.id}: {e}")
                        
                        message_data = {
                            'id': message.id,
                            'chat_id': chat_id,
                            'sender_id': message.sender_id,
                            'date': message.date,
                            'text': message.text or '',
                            'reply_to_msg_id': message.reply_to_msg_id if hasattr(message, 'reply_to_msg_id') else None,
                            'reply_to_text': None,
                            'forward_from_id': None,
                            'edit_date': message.edit_date,
                            'media_type': media_type,
                            'media_id': media_id,
                            'media_path': media_path,
                            'raw_data': {'grouped_id': str(message.grouped_id)} if message.grouped_id else {},
                            'is_outgoing': 1 if message.out else 0
                        }
                        await self.db.insert_message(message_data)
                        self.stats['new_messages_saved'] += 1
                    
                    logger.info(f"📸 Album saved: chat={chat_id} messages={album_size}")
                        
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error in album handler: {e}", exc_info=True)
    
    
    async def run(self) -> None:
        """
        Run the listener until stopped.
        
        Operations are applied immediately with rate limiting:
        - Normal usage (few deletions) → applied instantly
        - Mass operations → blocked after threshold
        """
        self._running = True
        self.stats['start_time'] = datetime.now()
        
        # Start the rate limiter
        self._protector.start()
        
        # Write listener status to database (for viewer to display)
        try:
            await self.db.set_metadata('listener_active_since', datetime.now().isoformat())
        except Exception as e:
            logger.warning(f"Could not write listener status to DB: {e}")
        
        logger.info("=" * 70)
        logger.info("🎧 Real-time listener started with RATE LIMITING")
        logger.info(f"   Max {self._protector.threshold} ops per {self._protector.window_seconds}s per chat")
        logger.info("   Normal usage works instantly, mass operations blocked")
        logger.info("=" * 70)
        
        try:
            # Keep running until disconnected or stopped
            await self.client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.info("Listener cancelled")
        finally:
            self._running = False
            # Clear listener status when stopped
            try:
                await self.db.set_metadata('listener_active_since', '')
            except Exception:
                pass
            
            # Stop the processor
            if self._processor_task:
                self._processor_task.cancel()
                try:
                    await self._processor_task
                except asyncio.CancelledError:
                    pass
            
            # Stop the protector
            await self._protector.stop()
            
            await self._log_stats()
    
    async def stop(self) -> None:
        """
        Stop the listener gracefully.
        
        Only disconnects if we own the client (created it ourselves).
        Shared clients are managed by the connection owner.
        """
        logger.info("Stopping listener...")
        self._running = False
        
        # Only disconnect if we own the client
        if self.client and self._owns_client and self.client.is_connected():
            await self.client.disconnect()
        
        await self._log_stats()
        logger.info("Listener stopped")
    
    async def _log_stats(self) -> None:
        """Log listener and protection statistics."""
        if self.stats['start_time']:
            uptime = datetime.now() - self.stats['start_time']
            protector_stats = self._protector.get_stats()
            
            logger.info("=" * 70)
            logger.info("📊 Listener Statistics")
            logger.info(f"   Uptime: {uptime}")
            logger.info("")
            logger.info("   📝 Edits:")
            logger.info(f"      Received: {self.stats['edits_received']}")
            logger.info(f"      Applied:  {self.stats['edits_applied']}")
            logger.info("")
            logger.info("   🗑️ Deletions:")
            logger.info(f"      Received: {self.stats['deletions_received']}")
            logger.info(f"      Applied:  {self.stats['deletions_applied']}")
            if self.stats['deletions_skipped']:
                logger.info(f"      Skipped (LISTEN_DELETIONS=false): {self.stats['deletions_skipped']}")
            logger.info("")
            logger.info("   📩 New Messages:")
            logger.info(f"      Received: {self.stats['new_messages_received']}")
            logger.info(f"      Saved:    {self.stats['new_messages_saved']}")
            logger.info("")
            logger.info("   🛡️ Protection:")
            logger.info(f"      Bursts intercepted: {protector_stats['bursts_detected']}")
            logger.info(f"      Operations discarded: {protector_stats['operations_discarded']}")
            logger.info(f"      Chats protected: {protector_stats['chats_protected']}")
            
            if self.stats['errors']:
                logger.warning(f"   ⚠️ Errors: {self.stats['errors']}")
            
            # Show currently blocked chats
            blocked = self._protector.get_blocked_chats()
            if blocked:
                logger.warning("")
                logger.warning(f"   🚫 Currently blocked chats: {len(blocked)}")
                for chat_id, (reason, discarded) in blocked.items():
                    logger.warning(f"      Chat {chat_id}: {discarded} ops discarded - {reason}")
            
            logger.info("=" * 70)
    
    async def close(self) -> None:
        """Clean up resources."""
        await self.stop()
        if self.db:
            await self.db.close()


async def run_listener(config: Config) -> None:
    """
    Run the real-time listener as a standalone process.
    
    Args:
        config: Configuration object
    """
    listener = await TelegramListener.create(config)
    
    try:
        await listener.connect()
        await listener.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        await listener.close()


async def main() -> None:
    """Main entry point for standalone listener mode."""
    from .config import Config, setup_logging
    
    try:
        config = Config()
        setup_logging(config)
        
        logger.info("=" * 60)
        logger.info("Telegram Archive - Real-time Listener")
        logger.info("=" * 60)
        logger.info("This mode catches message edits and deletions in real-time")
        logger.info("Run alongside the backup scheduler for complete coverage")
        logger.info("=" * 60)
        
        await run_listener(config)
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise


if __name__ == '__main__':
    asyncio.run(main())
