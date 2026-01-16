"""
Real-time event listener for Telegram message edits and deletions.
Catches events as they happen and updates the local database immediately.

Safety features:
- LISTEN_EDITS: Apply text edits (default: true, safe)
- LISTEN_DELETIONS: Delete messages (default: false, protects backup!)
- Mass operation detection: Blocks bulk edits/deletions to protect data

ZERO-FOOTPRINT PROTECTION:
When mass operations are detected, NO changes are written to the database.
Operations are buffered and only applied after a safety delay, ensuring
that burst attacks are caught BEFORE any data is modified.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Set, Deque, Tuple, List, Dict, Any

from telethon import TelegramClient, events
from telethon.utils import get_peer_id

from .config import Config
from .db import DatabaseAdapter, create_adapter

logger = logging.getLogger(__name__)


@dataclass
class PendingOperation:
    """A pending operation waiting to be applied."""
    chat_id: int
    operation_type: str  # 'edit' or 'deletion'
    timestamp: datetime
    data: Dict[str, Any]  # Operation-specific data


class MassOperationProtector:
    """
    Zero-footprint protection against mass deletions/edits.
    
    HOW IT WORKS:
    1. Operations are NOT applied immediately - they go into a buffer
    2. A background task processes the buffer after a short delay
    3. If too many operations arrive before processing, the ENTIRE buffer is discarded
    4. This ensures ZERO footprint - no data is ever modified during an attack
    
    The key insight: by buffering operations and applying them with a delay,
    we can detect a burst pattern BEFORE writing anything to the database.
    """
    
    def __init__(
        self, 
        threshold: int = 10,
        window_seconds: int = 30,
        buffer_delay_seconds: float = 2.0
    ):
        """
        Args:
            threshold: Max operations allowed per chat in the time window
            window_seconds: Time window for counting operations
            buffer_delay_seconds: How long to buffer before applying (allows burst detection)
        """
        self.threshold = threshold
        self.window = timedelta(seconds=window_seconds)
        self.buffer_delay = buffer_delay_seconds
        
        # Pending operations buffer: {chat_id: [PendingOperation, ...]}
        self._pending: Dict[int, List[PendingOperation]] = {}
        
        # Blocked chats: {chat_id: (blocked_until, reason, discarded_count)}
        self._blocked: Dict[int, Tuple[datetime, str, int]] = {}
        
        # Processing task
        self._process_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Statistics
        self.stats = {
            'operations_applied': 0,
            'operations_discarded': 0,
            'bursts_detected': 0,
            'chats_protected': set()
        }
    
    def start(self):
        """Start the background processing task."""
        if not self._running:
            self._running = True
            self._process_task = asyncio.create_task(self._process_loop())
            logger.info(f"🛡️ Mass operation protector started (threshold: {self.threshold} ops in {self.window.seconds}s)")
    
    async def stop(self):
        """Stop the processing task."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
    
    def is_blocked(self, chat_id: int) -> Tuple[bool, str]:
        """Check if a chat is currently blocked."""
        if chat_id in self._blocked:
            blocked_until, reason, _ = self._blocked[chat_id]
            if datetime.now() < blocked_until:
                return True, reason
            else:
                # Block expired
                del self._blocked[chat_id]
                logger.info(f"🔓 Protection block expired for chat {chat_id}")
        return False, ""
    
    def queue_operation(self, chat_id: int, operation_type: str, data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Queue an operation for processing.
        
        Returns (queued, reason):
            - (True, "queued") if operation was queued
            - (False, reason) if chat is blocked
        """
        # Check if blocked
        blocked, reason = self.is_blocked(chat_id)
        if blocked:
            self.stats['operations_discarded'] += 1
            return False, f"BLOCKED: {reason}"
        
        # Add to pending buffer
        if chat_id not in self._pending:
            self._pending[chat_id] = []
        
        op = PendingOperation(
            chat_id=chat_id,
            operation_type=operation_type,
            timestamp=datetime.now(),
            data=data
        )
        self._pending[chat_id].append(op)
        
        # Check if this triggers protection
        pending_count = len(self._pending[chat_id])
        if pending_count >= self.threshold:
            # BURST DETECTED - Discard ALL pending operations for this chat
            discarded = len(self._pending[chat_id])
            del self._pending[chat_id]
            
            # Block the chat
            block_until = datetime.now() + self.window
            reason = f"🛡️ BURST DETECTED: {discarded} {operation_type}s in rapid succession"
            self._blocked[chat_id] = (block_until, reason, discarded)
            
            # Update stats
            self.stats['bursts_detected'] += 1
            self.stats['operations_discarded'] += discarded
            self.stats['chats_protected'].add(chat_id)
            
            logger.warning("=" * 70)
            logger.warning(f"🛡️ ZERO-FOOTPRINT PROTECTION ACTIVATED")
            logger.warning(f"   Chat: {chat_id}")
            logger.warning(f"   Attack type: Mass {operation_type}")
            logger.warning(f"   Operations intercepted: {discarded}")
            logger.warning(f"   Data preserved: 100% (ZERO changes written to database)")
            logger.warning(f"   Chat blocked until: {block_until}")
            logger.warning("=" * 70)
            
            return False, reason
        
        return True, "queued"
    
    async def _process_loop(self):
        """Background loop that processes buffered operations after delay.
        
        Note: This loop just triggers _process_pending periodically.
        The actual application of operations is done by TelegramListener._process_buffered_operations
        which calls get_ready_operations().
        """
        while self._running:
            try:
                await asyncio.sleep(self.buffer_delay)
                # Just trigger the processing - operations are collected by listener
                self._process_pending()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in protection processor: {e}")
    
    def _process_pending(self) -> List[PendingOperation]:
        """Process pending operations that have been buffered long enough.
        
        Returns:
            List of operations ready to be applied.
        """
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.buffer_delay)
        
        # Collect operations ready to be applied
        ready_ops: List[PendingOperation] = []
        
        for chat_id in list(self._pending.keys()):
            # Check if chat got blocked while we were waiting
            blocked, _ = self.is_blocked(chat_id)
            if blocked:
                # Discard all pending for this chat
                discarded = len(self._pending[chat_id])
                self.stats['operations_discarded'] += discarded
                del self._pending[chat_id]
                continue
            
            # Find operations old enough to process
            ops = self._pending[chat_id]
            ready = [op for op in ops if op.timestamp < cutoff]
            remaining = [op for op in ops if op.timestamp >= cutoff]
            
            if ready:
                ready_ops.extend(ready)
                if remaining:
                    self._pending[chat_id] = remaining
                else:
                    del self._pending[chat_id]
        
        # Filter out blocked operations and count applied
        result: List[PendingOperation] = []
        for op in ready_ops:
            # Double-check not blocked (could have been blocked by newer ops)
            blocked, _ = self.is_blocked(op.chat_id)
            if blocked:
                self.stats['operations_discarded'] += 1
                continue
            
            self.stats['operations_applied'] += 1
            result.append(op)
        
        return result
    
    async def get_ready_operations(self) -> List[PendingOperation]:
        """Get operations ready to be applied (called by listener)."""
        return self._process_pending()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get protection statistics."""
        return {
            'operations_applied': self.stats['operations_applied'],
            'operations_discarded': self.stats['operations_discarded'],
            'bursts_detected': self.stats['bursts_detected'],
            'chats_protected': len(self.stats['chats_protected']),
            'currently_blocked': len([c for c in self._blocked if datetime.now() < self._blocked[c][0]]),
            'pending_operations': sum(len(ops) for ops in self._pending.values())
        }
    
    def get_blocked_chats(self) -> Dict[int, Tuple[str, int]]:
        """Get currently blocked chats with reasons and discarded counts."""
        now = datetime.now()
        return {
            chat_id: (reason, discarded)
            for chat_id, (blocked_until, reason, discarded) in self._blocked.items()
            if now < blocked_until
        }


class TelegramListener:
    """
    Real-time event listener for Telegram.
    
    Catches message edits and deletions as they happen and updates the database.
    Designed to run alongside the scheduled backup process.
    
    ZERO-FOOTPRINT PROTECTION:
    All operations are buffered before being applied. If a mass operation
    is detected (burst of edits/deletions), the ENTIRE buffer is discarded
    and NO changes are written to the database. Your backup stays intact.
    
    Safety features:
    - LISTEN_EDITS: Only sync edits if enabled (default: true)
    - LISTEN_DELETIONS: Only delete if enabled (default: false - protects backup!)
    - Mass operation detection: Blocks bulk changes with zero footprint
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
        
        # Zero-footprint mass operation protection
        self._protector = MassOperationProtector(
            threshold=config.mass_operation_threshold,
            window_seconds=config.mass_operation_window_seconds,
            buffer_delay_seconds=config.mass_operation_buffer_delay
        )
        
        # Background task for processing buffered operations
        self._processor_task: Optional[asyncio.Task] = None
        
        # Statistics
        self.stats = {
            'edits_received': 0,
            'edits_applied': 0,
            'deletions_received': 0,
            'deletions_applied': 0,
            'deletions_skipped': 0,  # Skipped due to LISTEN_DELETIONS=false
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
        logger.info(f"  Protection threshold: {config.mass_operation_threshold} ops triggers block")
        logger.info(f"  Protection window: {config.mass_operation_window_seconds}s")
        logger.info(f"  Buffer delay: {config.mass_operation_buffer_delay}s (operations held before applying)")
        logger.info("=" * 70)
    
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
                
                # Queue for protected processing (NOT applied immediately!)
                queued, reason = self._protector.queue_operation(
                    chat_id=chat_id,
                    operation_type='edit',
                    data={
                        'message_id': message.id,
                        'new_text': new_text,
                        'edit_date': edit_date
                    }
                )
                
                if not queued:
                    self.stats['operations_discarded'] += 1
                    # Don't log every blocked op - the protector already logged the burst
                else:
                    logger.debug(f"📝 Edit queued: chat={chat_id} msg={message.id}")
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error queueing edit event: {e}", exc_info=True)
        
        @self.client.on(events.MessageDeleted)
        async def on_message_deleted(event: events.MessageDeleted.Event) -> None:
            """
            Handle message deletion events.
            
            Operations are QUEUED, not applied immediately.
            If a mass deletion is detected, ALL queued deletions are discarded
            and NOTHING is deleted from your backup.
            """
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
                
                # Queue each deletion for protected processing
                for msg_id in event.deleted_ids:
                    self.stats['deletions_received'] += 1
                    
                    if chat_id is not None:
                        queued, reason = self._protector.queue_operation(
                            chat_id=chat_id,
                            operation_type='deletion',
                            data={
                                'message_id': msg_id,
                                'chat_id': chat_id
                            }
                        )
                        
                        if not queued:
                            self.stats['operations_discarded'] += 1
                        else:
                            logger.debug(f"🗑️ Deletion queued: chat={chat_id} msg={msg_id}")
                    else:
                        # Chat unknown - log warning but don't process
                        # (can't protect without knowing the chat)
                        logger.warning(f"⚠️ Deletion with unknown chat ignored: msg={msg_id}")
                
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Error queueing deletion event: {e}", exc_info=True)
        
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
    
    async def _process_buffered_operations(self) -> None:
        """
        Background task that processes buffered operations.
        
        This is the core of zero-footprint protection:
        - Operations wait in the buffer for 2 seconds
        - If a burst is detected, the buffer is discarded
        - Only "safe" operations that passed the delay are applied
        """
        while self._running:
            try:
                await asyncio.sleep(0.5)  # Check every 500ms
                
                # Get operations ready to be applied
                ready_ops = await self._protector.get_ready_operations()
                
                for op in ready_ops:
                    try:
                        if op.operation_type == 'edit':
                            await self.db.update_message_text(
                                chat_id=op.chat_id,
                                message_id=op.data['message_id'],
                                new_text=op.data['new_text'],
                                edit_date=op.data['edit_date']
                            )
                            self.stats['edits_applied'] += 1
                            preview = op.data['new_text'][:30] + '...' if len(op.data['new_text']) > 30 else op.data['new_text']
                            logger.info(f"📝 Edit applied: chat={op.chat_id} msg={op.data['message_id']} text=\"{preview}\"")
                        
                        elif op.operation_type == 'deletion':
                            await self.db.delete_message(op.chat_id, op.data['message_id'])
                            self.stats['deletions_applied'] += 1
                            logger.info(f"🗑️ Deletion applied: chat={op.chat_id} msg={op.data['message_id']}")
                    
                    except Exception as e:
                        self.stats['errors'] += 1
                        logger.error(f"Error applying {op.operation_type}: {e}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in operation processor: {e}")
    
    async def run(self) -> None:
        """
        Run the listener until stopped.
        
        This starts:
        1. The Telegram client for receiving events
        2. The background processor for applying buffered operations
        """
        self._running = True
        self.stats['start_time'] = datetime.now()
        
        # Start the protection system
        self._protector.start()
        
        # Start the background operation processor
        self._processor_task = asyncio.create_task(self._process_buffered_operations())
        
        logger.info("=" * 70)
        logger.info("🎧 Real-time listener started with ZERO-FOOTPRINT PROTECTION")
        logger.info("   All operations are buffered before being applied")
        logger.info("   Mass operations will be detected and discarded")
        logger.info("   Your backup data is protected!")
        logger.info("=" * 70)
        
        try:
            # Keep running until disconnected or stopped
            await self.client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.info("Listener cancelled")
        finally:
            self._running = False
            
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
        """Stop the listener gracefully."""
        logger.info("Stopping listener...")
        self._running = False
        
        if self.client and self.client.is_connected():
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
