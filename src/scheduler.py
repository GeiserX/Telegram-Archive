"""
Scheduler for automated Telegram backups.
Runs backup tasks on a configurable cron schedule.

Optionally runs a real-time listener that catches message edits and deletions
between scheduled backup runs (when ENABLE_LISTENER=true).

SHARED CONNECTION ARCHITECTURE:
A single TelegramClient is shared between the backup and listener components.
This avoids session file lock conflicts and allows both to run simultaneously.
"""

import asyncio
import logging
import signal
import sys
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config, setup_logging
from .connection import TelegramConnection
from .telegram_backup import run_backup

logger = logging.getLogger(__name__)


class BackupScheduler:
    """
    Scheduler for automated backups with optional real-time listener.
    
    Uses a shared TelegramClient connection for both backup and listener,
    eliminating session file lock conflicts.
    """
    
    def __init__(self, config: Config):
        """
        Initialize backup scheduler.
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.running = False
        
        # Shared Telegram connection (used by both backup and listener)
        self._connection: Optional[TelegramConnection] = None
        
        # Real-time listener (optional)
        self._listener = None
        self._listener_task: Optional[asyncio.Task] = None
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.stop()
    
    async def _run_backup_job(self):
        """
        Wrapper for backup job that handles errors.
        
        Uses the shared connection - no need to pause the listener since both
        use the same TelegramClient.
        """
        try:
            logger.info("Scheduled backup starting...")
            
            # Ensure connection is still alive
            client = await self._connection.ensure_connected()
            
            # Run backup using shared client
            await run_backup(self.config, client=client)
            
            # Reload tracked chats in listener after backup
            # (new chats may have been added)
            if self._listener:
                await self._listener._load_tracked_chats()
            
            logger.info("Scheduled backup completed successfully")
            
        except Exception as e:
            logger.error(f"Scheduled backup failed: {e}", exc_info=True)
    
    def start(self):
        """Start the scheduler."""
        # Parse cron schedule
        # Format: minute hour day month day_of_week
        # Example: "0 */6 * * *" = every 6 hours
        try:
            parts = self.config.schedule.split()
            if len(parts) != 5:
                raise ValueError(
                    f"Invalid cron schedule format: {self.config.schedule}. "
                    "Expected format: 'minute hour day month day_of_week'"
                )
            
            minute, hour, day, month, day_of_week = parts
            
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week
            )
            
            # Add job to scheduler
            self.scheduler.add_job(
                self._run_backup_job,
                trigger=trigger,
                id='telegram_backup',
                name='Telegram Backup',
                replace_existing=True
            )
            
            logger.info(f"Backup scheduled with cron: {self.config.schedule}")
            
            # Start scheduler
            self.scheduler.start()
            self.running = True
            
            logger.info("Scheduler started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}", exc_info=True)
            raise
    
    def stop(self):
        """Stop the scheduler."""
        if self.running:
            logger.info("Stopping scheduler...")
            self.scheduler.shutdown(wait=True)
            self.running = False
            logger.info("Scheduler stopped")
    
    async def _connect(self) -> None:
        """Establish shared Telegram connection."""
        logger.info("Establishing shared Telegram connection...")
        self._connection = TelegramConnection(self.config)
        await self._connection.connect()
        logger.info("Shared connection established")
    
    async def _disconnect(self) -> None:
        """Close shared Telegram connection."""
        if self._connection:
            await self._connection.disconnect()
            self._connection = None
    
    async def _start_listener(self) -> None:
        """Start the real-time listener if enabled."""
        if not self.config.enable_listener:
            return
        
        if not self._connection or not self._connection.is_connected:
            logger.error("Cannot start listener: not connected to Telegram")
            return
        
        try:
            from .listener import TelegramListener
            
            logger.info("Starting real-time listener...")
            
            # Create listener with shared client
            self._listener = await TelegramListener.create(
                self.config,
                client=self._connection.client
            )
            await self._listener.connect()
            
            # Run listener in background task
            self._listener_task = asyncio.create_task(
                self._listener.run(),
                name="telegram_listener"
            )
            logger.info("Real-time listener started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start listener: {e}", exc_info=True)
            self._listener = None
            self._listener_task = None
    
    async def _stop_listener(self) -> None:
        """Stop the real-time listener if running."""
        if self._listener_task:
            logger.info("Stopping real-time listener...")
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        
        if self._listener:
            await self._listener.close()
            self._listener = None
            logger.info("Real-time listener stopped")
    
    async def run_forever(self):
        """
        Keep the scheduler running with optional listener.
        
        Flow:
        1. Connect to Telegram (shared connection)
        2. Start scheduler
        3. Start listener if enabled (uses shared connection)
        4. Run initial backup (uses shared connection)
        5. Keep running until stopped
        """
        # Establish shared connection
        await self._connect()
        
        # Start scheduler
        self.start()
        
        # Start real-time listener if enabled (uses shared connection)
        await self._start_listener()
        
        # Run initial backup immediately on startup (uses shared connection)
        logger.info("Running initial backup on startup...")
        try:
            await run_backup(self.config, client=self._connection.client)
            logger.info("Initial backup completed")
            
            # Reload tracked chats in listener after initial backup
            if self._listener:
                await self._listener._load_tracked_chats()
                
        except Exception as e:
            logger.error(f"Initial backup failed: {e}", exc_info=True)
        
        # Keep running until stopped
        try:
            while self.running:
                await asyncio.sleep(1)
                
                # Check if listener task died unexpectedly and restart it
                if self.config.enable_listener and self._listener_task:
                    if self._listener_task.done():
                        # Check if there was an exception
                        try:
                            exc = self._listener_task.exception()
                            if exc:
                                logger.error(f"Listener task died with error: {exc}")
                        except asyncio.CancelledError:
                            pass
                        
                        logger.warning("Listener task died, restarting...")
                        await self._stop_listener()
                        await asyncio.sleep(5)  # Brief pause before restart
                        await self._start_listener()
                        
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            await self._stop_listener()
            self.stop()
            await self._disconnect()


async def main():
    """Main entry point for the scheduler."""
    try:
        # Load configuration
        from .config import Config, setup_logging
        config = Config()
        setup_logging(config)
        
        logger.info("=" * 60)
        logger.info("Telegram Backup Automation")
        logger.info("=" * 60)
        logger.info(f"Schedule: {config.schedule}")
        logger.info(f"Backup path: {config.backup_path}")
        logger.info(f"Download media: {config.download_media}")
        logger.info(f"Chat types: {', '.join(config.chat_types) or '(whitelist-only mode)'}")
        logger.info(f"Real-time listener: {'ENABLED' if config.enable_listener else 'disabled'}")
        if config.sync_deletions_edits:
            logger.warning("⚠️  SYNC_DELETIONS_EDITS: ENABLED")
            logger.warning("   → Will re-check ALL messages for edits/deletions each run")
            logger.warning("   → This is expensive but catches changes made while offline")
        logger.info("=" * 60)
        
        # Create and run scheduler
        scheduler = BackupScheduler(config)
        await scheduler.run_forever()
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
