"""
Scheduler for automated Telegram backups.
Runs backup tasks on a configurable cron schedule.
"""

import asyncio
import logging
import signal
import sys
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config, setup_logging
from .telegram_backup import run_backup

logger = logging.getLogger(__name__)


class BackupScheduler:
    """Scheduler for automated backups."""
    
    def __init__(self, config: Config):
        """
        Initialize backup scheduler.
        
        Args:
            config: Configuration object
        """
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.running = False
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.stop()
    
    async def _run_backup_job(self):
        """Wrapper for backup job that handles errors."""
        try:
            logger.info("Scheduled backup starting...")
            await run_backup(self.config)
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
    
    async def run_forever(self):
        """Keep the scheduler running."""
        self.start()
        
        # Run initial backup immediately on startup
        logger.info("Running initial backup on startup...")
        try:
            await run_backup(self.config)
            logger.info("Initial backup completed")
        except Exception as e:
            logger.error(f"Initial backup failed: {e}", exc_info=True)
        
        # Keep running until stopped
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.stop()


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
        logger.info(f"Chat types: {', '.join(config.chat_types)}")
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
