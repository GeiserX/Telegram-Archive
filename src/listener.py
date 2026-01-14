"""
Real-time event listener for Telegram message edits and deletions.
Catches events as they happen and updates the local database immediately.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Set

from telethon import TelegramClient, events
from telethon.utils import get_peer_id

from .config import Config
from .db import DatabaseAdapter, create_adapter

logger = logging.getLogger(__name__)


class TelegramListener:
    """
    Real-time event listener for Telegram.
    
    Catches message edits and deletions as they happen and updates the database.
    Designed to run alongside the scheduled backup process.
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
        
        # Statistics
        self.stats = {
            'edits_processed': 0,
            'deletions_processed': 0,
            'errors': 0,
            'start_time': None
        }
        
        logger.info("TelegramListener initialized")
    
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
            try:
                chat_id = self._get_marked_id(event.chat_id)
                
                if not self._should_process_chat(chat_id):
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
            try:
                # Note: event.chat_id might be None for some deletion events
                chat_id = event.chat_id
                if chat_id is not None:
                    chat_id = self._get_marked_id(chat_id)
                    
                    if not self._should_process_chat(chat_id):
                        return
                
                for msg_id in event.deleted_ids:
                    if chat_id is not None:
                        # We know the chat - delete directly
                        await self.db.delete_message(chat_id, msg_id)
                        logger.info(f"🗑️ Deleted: chat={chat_id} msg={msg_id}")
                    else:
                        # Chat unknown - try to find and delete from all chats
                        # This is less efficient but handles edge cases
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
            logger.info(f"  Deletions processed: {self.stats['deletions_processed']}")
            logger.info(f"  Errors: {self.stats['errors']}")
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
