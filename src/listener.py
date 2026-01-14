"""
Real-time event listener for Telegram message edits and deletions.
Catches events as they happen and updates the local database immediately.

Safety features:
- LISTEN_EDITS: Apply text edits (default: true, safe)
- LISTEN_DELETIONS: Delete messages (default: false, protects backup!)
- Mass operation detection: Blocks bulk edits/deletions to protect data
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Set, Deque, Tuple

from telethon import TelegramClient, events
from telethon.utils import get_peer_id

from .config import Config
from .db import DatabaseAdapter, create_adapter

logger = logging.getLogger(__name__)


class MassOperationDetector:
    """
    Detects and blocks mass deletions/edits to protect backup integrity.
    
    Tracks operations per chat within a sliding time window.
    If operations exceed threshold, blocks further operations and alerts.
    """
    
    def __init__(self, threshold: int, window_seconds: int):
        """
        Args:
            threshold: Max operations allowed per chat in the time window
            window_seconds: Time window in seconds
        """
        self.threshold = threshold
        self.window = timedelta(seconds=window_seconds)
        # Per-chat operation timestamps: {chat_id: deque[(timestamp, count)]}
        self._operations: dict[int, Deque[Tuple[datetime, int]]] = {}
        # Blocked chats: {chat_id: (blocked_until, reason)}
        self._blocked: dict[int, Tuple[datetime, str]] = {}
        
    def _cleanup_old(self, chat_id: int) -> None:
        """Remove operations outside the time window."""
        if chat_id not in self._operations:
            return
        now = datetime.now()
        cutoff = now - self.window
        while self._operations[chat_id] and self._operations[chat_id][0][0] < cutoff:
            self._operations[chat_id].popleft()
    
    def _count_recent(self, chat_id: int) -> int:
        """Count operations in the current time window."""
        if chat_id not in self._operations:
            return 0
        return sum(count for _, count in self._operations[chat_id])
    
    def check_and_record(self, chat_id: int, operation_type: str, count: int = 1) -> Tuple[bool, str]:
        """
        Check if operation is allowed and record it.
        
        Args:
            chat_id: The chat ID
            operation_type: 'edit' or 'deletion'
            count: Number of operations (for batch deletions)
            
        Returns:
            (allowed, reason): True if allowed, False if blocked with reason
        """
        now = datetime.now()
        
        # Check if chat is currently blocked
        if chat_id in self._blocked:
            blocked_until, reason = self._blocked[chat_id]
            if now < blocked_until:
                return False, f"BLOCKED: {reason}"
            else:
                # Block expired, remove it
                del self._blocked[chat_id]
                logger.info(f"🔓 Block expired for chat {chat_id}")
        
        # Initialize if needed
        if chat_id not in self._operations:
            self._operations[chat_id] = deque()
        
        # Clean up old entries
        self._cleanup_old(chat_id)
        
        # Count recent operations
        recent_count = self._count_recent(chat_id)
        
        # Check threshold
        if recent_count + count > self.threshold:
            # Block this chat for the window duration
            block_until = now + self.window
            reason = f"Mass {operation_type} detected: {recent_count + count} operations in {self.window.seconds}s (threshold: {self.threshold})"
            self._blocked[chat_id] = (block_until, reason)
            logger.warning(f"🛑 {reason} - Chat {chat_id} BLOCKED until {block_until}")
            return False, reason
        
        # Record the operation
        self._operations[chat_id].append((now, count))
        return True, "OK"
    
    def get_blocked_chats(self) -> dict[int, str]:
        """Get currently blocked chats and their reasons."""
        now = datetime.now()
        return {
            chat_id: reason 
            for chat_id, (blocked_until, reason) in self._blocked.items() 
            if now < blocked_until
        }


class TelegramListener:
    """
    Real-time event listener for Telegram.
    
    Catches message edits and deletions as they happen and updates the database.
    Designed to run alongside the scheduled backup process.
    
    Safety features:
    - LISTEN_EDITS: Only sync edits if enabled (default: true)
    - LISTEN_DELETIONS: Only delete if enabled (default: false - protects backup!)
    - Mass operation detection: Blocks bulk changes to protect data integrity
    """
    
    def __init__(self, config: Config, db: DatabaseAdapter):
        """
        Initialize the listener.
        
        Args:
            config: Configuration object
            db: Database adapter (must be initialized)
        """
        self.config = config
        self.config.validate_credentials()
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._running = False
        self._tracked_chat_ids: Set[int] = set()
        
        # Mass operation protection
        self._mass_detector = MassOperationDetector(
            threshold=config.mass_operation_threshold,
            window_seconds=config.mass_operation_window_seconds
        )
        
        # Statistics
        self.stats = {
            'edits_processed': 0,
            'edits_blocked': 0,
            'deletions_processed': 0,
            'deletions_blocked': 0,
            'deletions_skipped': 0,  # Skipped due to LISTEN_DELETIONS=false
            'mass_operations_blocked': 0,
            'errors': 0,
            'start_time': None
        }
        
        # Log safety settings
        logger.info("TelegramListener initialized")
        logger.info(f"  LISTEN_EDITS: {config.listen_edits}")
        if config.listen_deletions:
            logger.warning(f"  ⚠️ LISTEN_DELETIONS: true - Messages WILL be deleted from backup!")
        else:
            logger.info(f"  LISTEN_DELETIONS: false (backup protected)")
        logger.info(f"  Mass operation protection: >{config.mass_operation_threshold} ops in {config.mass_operation_window_seconds}s")
    
    @classmethod
    async def create(cls, config: Config) -> "TelegramListener":
        """
        Factory method to create TelegramListener with initialized database.
        
        Args:
            config: Configuration object
            
        Returns:
            Initialized TelegramListener instance
        """
        db = await create_adapter()
        return cls(config, db)
    
    async def connect(self) -> None:
        """Connect to Telegram and set up event handlers."""
        self.client = TelegramClient(
            self.config.session_path,
            self.config.api_id,
            self.config.api_hash
        )
        
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
    
    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""
        
        @self.client.on(events.MessageEdited)
        async def on_message_edited(event: events.MessageEdited.Event) -> None:
            """Handle message edit events."""
            # Check if edits are enabled
            if not self.config.listen_edits:
                return
                
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                if not self._should_process_chat(chat_id):
                    return
                
                # Check mass operation protection
                allowed, reason = self._mass_detector.check_and_record(chat_id, 'edit')
                if not allowed:
                    self.stats['edits_blocked'] += 1
                    self.stats['mass_operations_blocked'] += 1
                    logger.warning(f"🛑 Edit blocked: chat={chat_id} msg={event.message.id} - {reason}")
                    return
                
                message = event.message
                new_text = message.text or ''
                edit_date = message.edit_date
                
                # Update in database
                await self.db.update_message_text(
                    chat_id=chat_id,
                    message_id=message.id,
                    new_text=new_text,
                    edit_date=edit_date
                )
                
                self.stats['edits_processed'] += 1
                
                # Truncate text for logging
                preview = new_text[:50] + '...' if len(new_text) > 50 else new_text
                logger.info(f"📝 Edit: chat={chat_id} msg={message.id} text=\"{preview}\"")
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error processing edit event: {e}", exc_info=True)
        
        @self.client.on(events.MessageDeleted)
        async def on_message_deleted(event: events.MessageDeleted.Event) -> None:
            """Handle message deletion events."""
            # Check if deletions are enabled (DEFAULT: FALSE to protect backup!)
            if not self.config.listen_deletions:
                # Just log and skip - don't delete from backup
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
                
                deletion_count = len(event.deleted_ids)
                
                # Check mass operation protection BEFORE processing
                if chat_id is not None:
                    allowed, reason = self._mass_detector.check_and_record(
                        chat_id, 'deletion', count=deletion_count
                    )
                    if not allowed:
                        self.stats['deletions_blocked'] += deletion_count
                        self.stats['mass_operations_blocked'] += 1
                        logger.warning(f"🛑 Mass deletion blocked: chat={chat_id} count={deletion_count} - {reason}")
                        return
                
                for msg_id in event.deleted_ids:
                    if chat_id is not None:
                        # We know the chat - delete directly
                        await self.db.delete_message(chat_id, msg_id)
                        logger.info(f"🗑️ Deleted: chat={chat_id} msg={msg_id}")
                    else:
                        # Chat unknown - check each deletion individually (can't determine chat for protection)
                        # This is rare and less efficient
                        deleted = await self.db.delete_message_by_id_any_chat(msg_id)
                        if deleted:
                            logger.info(f"🗑️ Deleted: msg={msg_id} (chat unknown)")
                    
                    self.stats['deletions_processed'] += 1
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error processing deletion event: {e}", exc_info=True)
        
        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            """
            Handle new messages to keep tracked chat list updated.
            
            This ensures newly backed-up chats are tracked for edits/deletions.
            """
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                # Add to tracked chats if we should be backing up this chat
                if chat_id not in self._tracked_chat_ids:
                    if self._should_process_chat(chat_id):
                        self._tracked_chat_ids.add(chat_id)
                        logger.debug(f"Added chat {chat_id} to tracking list")
                        
            except Exception as e:
                logger.debug(f"Error in new message handler: {e}")
    
    async def run(self) -> None:
        """
        Run the listener until stopped.
        
        This keeps the client connected and processing events.
        """
        self._running = True
        self.stats['start_time'] = datetime.now()
        
        logger.info("=" * 60)
        logger.info("🎧 Real-time listener started")
        logger.info("Listening for message edits and deletions...")
        logger.info("=" * 60)
        
        try:
            # Keep running until disconnected or stopped
            await self.client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.info("Listener cancelled")
        finally:
            self._running = False
            await self._log_stats()
    
    async def stop(self) -> None:
        """Stop the listener gracefully."""
        logger.info("Stopping listener...")
        self._running = False
        
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        
        await self._log_stats()
        logger.info("Listener stopped")
    
    async def _log_stats(self) -> None:
        """Log listener statistics."""
        if self.stats['start_time']:
            uptime = datetime.now() - self.stats['start_time']
            logger.info("=" * 60)
            logger.info("Listener Statistics")
            logger.info(f"  Uptime: {uptime}")
            logger.info(f"  Edits processed: {self.stats['edits_processed']}")
            if self.stats['edits_blocked']:
                logger.warning(f"  Edits BLOCKED (mass operation): {self.stats['edits_blocked']}")
            logger.info(f"  Deletions processed: {self.stats['deletions_processed']}")
            if self.stats['deletions_skipped']:
                logger.info(f"  Deletions skipped (LISTEN_DELETIONS=false): {self.stats['deletions_skipped']}")
            if self.stats['deletions_blocked']:
                logger.warning(f"  Deletions BLOCKED (mass operation): {self.stats['deletions_blocked']}")
            if self.stats['mass_operations_blocked']:
                logger.warning(f"  ⚠️ Mass operations blocked: {self.stats['mass_operations_blocked']} incidents")
            logger.info(f"  Errors: {self.stats['errors']}")
            
            # Show currently blocked chats
            blocked = self._mass_detector.get_blocked_chats()
            if blocked:
                logger.warning(f"  Currently blocked chats: {len(blocked)}")
                for chat_id, reason in blocked.items():
                    logger.warning(f"    - {chat_id}: {reason}")
            logger.info("=" * 60)
    
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
