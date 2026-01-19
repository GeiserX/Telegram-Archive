"""
Configuration management for Telegram Backup Automation.
Loads and validates settings from environment variables.
"""

import os
import logging
from typing import List, Optional
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Configuration settings loaded from environment variables."""
    
    def __init__(self):
        """Initialize configuration from environment variables."""
        # Telegram API credentials (optional for viewer, required for backup)
        self.api_id = int(os.getenv('TELEGRAM_API_ID')) if os.getenv('TELEGRAM_API_ID') else None
        self.api_hash = os.getenv('TELEGRAM_API_HASH')
        self.phone = os.getenv('TELEGRAM_PHONE')
        

        # Backup schedule (cron format)
        self.schedule = os.getenv('SCHEDULE', '0 */6 * * *')
        
        # Backup options
        self.backup_path = os.getenv('BACKUP_PATH', '/data/backups')
        self.download_media = os.getenv('DOWNLOAD_MEDIA', 'true').lower() == 'true'
        self.max_media_size_mb = int(os.getenv('MAX_MEDIA_SIZE_MB', '100'))
        
        # Batch processing configuration
        self.batch_size = int(os.getenv('BATCH_SIZE', '100'))

        # Database Configuration
        # Timeout for SQLite operations (seconds). 
        # Increase this if you experience "database is locked" errors (e.g., on Unraid/slow disks).
        # Default increased to 60s for better resilience with concurrent access (backup + web viewer).
        self.database_timeout = float(os.getenv('DATABASE_TIMEOUT', '60.0'))
        
        # Chat type filters
        # Use os.environ.get() to distinguish between "not set" vs "set to empty"
        # This ensures CHAT_TYPES= (empty) works for whitelist-only mode in Docker
        chat_types_env = os.environ.get('CHAT_TYPES')
        if chat_types_env is None:
            # Not set at all, use default (backup all types)
            chat_types_str = 'private,groups,channels'
        else:
            # Explicitly set (even if empty string)
            chat_types_str = chat_types_env
        self.chat_types = [ct.strip().lower() for ct in chat_types_str.split(',') if ct.strip()]
        self._validate_chat_types()
        
        # Granular chat ID filters
        # Global filters (backward compatibility with old names)
        self.global_include_ids = self._parse_id_list(
            os.getenv('GLOBAL_INCLUDE_CHAT_IDS') or os.getenv('INCLUDE_CHAT_IDS', '')
        )
        self.global_exclude_ids = self._parse_id_list(
            os.getenv('GLOBAL_EXCLUDE_CHAT_IDS') or os.getenv('EXCLUDE_CHAT_IDS', '')
        )
        
        # Per-type filters
        self.private_include_ids = self._parse_id_list(os.getenv('PRIVATE_INCLUDE_CHAT_IDS', ''))
        self.private_exclude_ids = self._parse_id_list(os.getenv('PRIVATE_EXCLUDE_CHAT_IDS', ''))
        
        self.groups_include_ids = self._parse_id_list(os.getenv('GROUPS_INCLUDE_CHAT_IDS', ''))
        self.groups_exclude_ids = self._parse_id_list(os.getenv('GROUPS_EXCLUDE_CHAT_IDS', ''))
        
        self.channels_include_ids = self._parse_id_list(os.getenv('CHANNELS_INCLUDE_CHAT_IDS', ''))
        self.channels_exclude_ids = self._parse_id_list(os.getenv('CHANNELS_EXCLUDE_CHAT_IDS', ''))
        
        # Priority chats - these are processed FIRST in all backup/sync operations
        # Useful for ensuring important chats are always backed up first
        self.priority_chat_ids = self._parse_id_list(os.getenv('PRIORITY_CHAT_IDS', ''))
        
        # Session configuration
        self.session_name = os.getenv('SESSION_NAME', 'telegram_backup')
        
        # Logging
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        # Handle common alias: WARN -> WARNING (Python uses WARNING, not WARN)
        if log_level == 'WARN':
            log_level = 'WARNING'
        self.log_level = getattr(logging, log_level, logging.INFO)
        
        # Derived paths
        # Store session in a separate directory from backups
        # If BACKUP_PATH is /data/backups, session goes to /data/session
        backup_parent = os.path.dirname(self.backup_path.rstrip('/\\'))
        self.session_dir = os.getenv('SESSION_DIR', os.path.join(backup_parent, 'session'))
        self.session_path = os.path.join(self.session_dir, f'{self.session_name}.session')
        
        self.session_path = os.path.join(self.session_dir, f'{self.session_name}.session')
        
        # Database path configuration
        # Default: inside backup_path
        # Can be overridden by DATABASE_PATH (full path) or DATABASE_DIR (directory)
        db_path_env = os.getenv('DATABASE_PATH')
        db_dir_env = os.getenv('DATABASE_DIR')
        
        if db_path_env:
            self.database_path = db_path_env
        elif db_dir_env:
            self.database_path = os.path.join(db_dir_env, 'telegram_backup.db')
        else:
            self.database_path = os.path.join(self.backup_path, 'telegram_backup.db')
            
        self.media_path = os.path.join(self.backup_path, 'media')
        
        # Ensure directories exist
        self._ensure_directories()
        
        # Sync options for exact Telegram mirroring (WARNING: expensive operation)
        # When enabled, checks all backed up messages for deletions/edits on Telegram
        self.sync_deletions_edits = os.getenv('SYNC_DELETIONS_EDITS', 'false').lower() == 'true'
        
        # Media verification mode
        # When enabled, checks all media files on disk and re-downloads missing/corrupted ones
        # Useful for recovering from interrupted backups or deleted media files
        self.verify_media = os.getenv('VERIFY_MEDIA', 'false').lower() == 'true'
        
        # Real-time listener mode
        # When enabled, runs a background listener that catches message edits and deletions
        # in real-time instead of batch-checking on each backup run
        self.enable_listener = os.getenv('ENABLE_LISTENER', 'false').lower() == 'true'
        
        # Listener granular controls (only apply when ENABLE_LISTENER=true)
        # LISTEN_EDITS: Apply text edits to backed up messages (safe, just updates text)
        self.listen_edits = os.getenv('LISTEN_EDITS', 'true').lower() == 'true'
        
        # LISTEN_DELETIONS: Delete messages from backup when deleted on Telegram
        # ⚠️ DEFAULT FALSE - Enabling defeats the purpose of having a backup!
        # Only enable if you explicitly want to mirror Telegram exactly
        self.listen_deletions = os.getenv('LISTEN_DELETIONS', 'true').lower() == 'true'
        
        # LISTEN_NEW_MESSAGES: Save new messages to backup in real-time
        # When enabled, new messages are saved immediately instead of waiting for scheduled backup
        # This provides true real-time backup but may increase API usage
        self.listen_new_messages = os.getenv('LISTEN_NEW_MESSAGES', 'true').lower() == 'true'
        
        # LISTEN_NEW_MESSAGES_MEDIA: Also download media in real-time (not just text)
        # When disabled (default), media is marked for download on next scheduled backup
        # When enabled, media is downloaded immediately - more API usage but instant availability
        self.listen_new_messages_media = os.getenv('LISTEN_NEW_MESSAGES_MEDIA', 'false').lower() == 'true'
        
        # LISTEN_CHAT_ACTIONS: Track chat photo changes, member joins/leaves, title changes
        # When enabled, updates to chat metadata are captured in real-time
        self.listen_chat_actions = os.getenv('LISTEN_CHAT_ACTIONS', 'true').lower() == 'true'
        
        # LISTEN_ALBUMS: Group media uploads together as albums
        # When enabled, grouped photos/videos are detected and stored together
        self.listen_albums = os.getenv('LISTEN_ALBUMS', 'true').lower() == 'true'
        
        # =====================================================================
        # MEDIA DEDUPLICATION
        # =====================================================================
        # DEDUPLICATE_MEDIA: Use symlinks to avoid storing duplicate files
        # When enabled (default), files shared across multiple chats are stored once
        # in a _shared directory and symlinked from chat directories.
        # Saves significant disk space when same media is shared across chats.
        self.deduplicate_media = os.getenv('DEDUPLICATE_MEDIA', 'true').lower() == 'true'
        
        # =====================================================================
        # ZERO-FOOTPRINT MASS OPERATION PROTECTION
        # =====================================================================
        # Operations are BUFFERED before being applied. If a burst is detected,
        # the ENTIRE buffer is discarded - ZERO changes written to your backup.
        #
        # THRESHOLD: How many operations trigger protection (default: 10 - aggressive!)
        # WINDOW: Time window for counting operations (default: 30 seconds)
        # BUFFER_DELAY: How long ops wait before applying (default: 2.0 seconds)
        #
        # Example: If >10 deletions arrive within 30s, all are discarded
        self.mass_operation_threshold = int(os.getenv('MASS_OPERATION_THRESHOLD', '10'))
        self.mass_operation_window_seconds = int(os.getenv('MASS_OPERATION_WINDOW_SECONDS', '30'))
        self.mass_operation_buffer_delay = float(os.getenv('MASS_OPERATION_BUFFER_DELAY', '2.0'))
        
        # Display chat IDs - restrict viewer to specific chats only
        # Useful for sharing public channel viewers without exposing other chats
        self.display_chat_ids = self._parse_id_list(os.getenv('DISPLAY_CHAT_IDS', ''))
        
        # Timezone configuration for viewer display
        # Defaults to Europe/Madrid if not specified
        self.viewer_timezone = os.getenv('VIEWER_TIMEZONE', 'Europe/Madrid')
        
        # Viewer notifications (internal use, prefer PUSH_NOTIFICATIONS)
        self.enable_notifications = os.getenv('ENABLE_NOTIFICATIONS', 'false').lower() == 'true'
        
        # Push notifications mode: 'off', 'basic', 'full'
        # - off: No notifications
        # - basic: In-browser notifications only (tab must be open)
        # - full: Web Push notifications (work even with browser closed, persistent subscriptions)
        push_mode = os.getenv('PUSH_NOTIFICATIONS', 'basic').lower()
        self.push_notifications = push_mode if push_mode in ('off', 'basic', 'full') else 'basic'
        
        # VAPID keys for Web Push (auto-generated if not provided)
        # Generate your own with: npx web-push generate-vapid-keys
        self.vapid_private_key = os.getenv('VAPID_PRIVATE_KEY', '')
        self.vapid_public_key = os.getenv('VAPID_PUBLIC_KEY', '')
        self.vapid_contact = os.getenv('VAPID_CONTACT', 'mailto:admin@example.com')
        
        # Stats calculation schedule
        # Daily calculation of statistics (chat counts, message counts, etc.)
        # Default: 03:00 (3am) in the configured viewer timezone
        self.stats_calculation_hour = int(os.getenv('STATS_CALCULATION_HOUR', '3'))
        
        # Avatar refresh interval in hours
        # Re-check and download avatars if existing file is older than this
        # Set to 0 to disable periodic refresh (only download new/changed photos)
        # Default: 24 hours
        self.avatar_refresh_hours = int(os.getenv('AVATAR_REFRESH_HOURS', '24'))
        
        # Show stats in viewer UI
        # When disabled, hides the stats dropdown next to "Telegram Archive" title
        # Useful for restricted viewers where you don't want to expose total counts
        self.show_stats = os.getenv('SHOW_STATS', 'true').lower() == 'true'
        
        logger.info("Configuration loaded successfully")
        logger.debug(f"Backup path: {self.backup_path}")
        logger.debug(f"Download media: {self.download_media}")
        logger.debug(f"Chat types: {self.chat_types}")
        logger.debug(f"Schedule: {self.schedule}")
        if self.sync_deletions_edits:
            logger.warning("SYNC_DELETIONS_EDITS enabled - this will check ALL messages for deletions/edits (expensive!)")
        if self.verify_media:
            logger.info("VERIFY_MEDIA enabled - will check for missing/corrupted media files and re-download them")
        if self.enable_listener:
            logger.info("ENABLE_LISTENER enabled - will catch message edits/deletions in real-time")
            logger.info(f"  LISTEN_EDITS: {self.listen_edits}")
            if self.listen_deletions:
                logger.warning("  ⚠️ LISTEN_DELETIONS: true - Messages will be DELETED from backup!")
            else:
                logger.info("  LISTEN_DELETIONS: false (backup protected)")
            if self.listen_new_messages:
                logger.info("  LISTEN_NEW_MESSAGES: true - New messages saved in real-time!")
            else:
                logger.info("  LISTEN_NEW_MESSAGES: false (messages saved on scheduled backup)")
            if self.listen_chat_actions:
                logger.info("  LISTEN_CHAT_ACTIONS: true - Chat metadata changes tracked!")
            if self.listen_albums:
                logger.info("  LISTEN_ALBUMS: true - Grouped media detected!")
            logger.info(f"  Mass operation protection: block if >{self.mass_operation_threshold} ops in {self.mass_operation_window_seconds}s")
        if self.display_chat_ids:
            logger.info(f"Display mode: Viewer restricted to chat IDs {self.display_chat_ids}")
    
    def _parse_id_list(self, id_str: str) -> set:
        """Parse comma-separated ID string into a set of integers."""
        if not id_str or not id_str.strip():
            return set()
        return {int(id.strip()) for id in id_str.split(',') if id.strip()}
    
    def _get_required_env(self, key: str, value_type: type):
        """
        Get a required environment variable and convert to specified type.
        
        Args:
            key: Environment variable name
            value_type: Type to convert the value to (int or str)
            
        Returns:
            Converted environment variable value
            
        Raises:
            ValueError: If environment variable is not set
        """
        value = os.getenv(key)
        if value is None or value == '':
            raise ValueError(
                f"Required environment variable '{key}' is not set. "
                f"Please set it in your .env file or environment."
            )
        
        try:
            if value_type == int:
                return int(value)
            return value
        except ValueError as e:
            raise ValueError(
                f"Environment variable '{key}' must be a valid {value_type.__name__}: {e}"
            )
    
    def _validate_chat_types(self):
        """Validate that chat types are valid options.
        
        Empty chat_types list is allowed - this enables "whitelist-only" mode
        where only explicitly included chat IDs are backed up.
        """
        valid_types = {'private', 'groups', 'channels'}
        invalid_types = set(self.chat_types) - valid_types
        
        if invalid_types:
            raise ValueError(
                f"Invalid chat types: {invalid_types}. "
                f"Valid options are: {valid_types}"
            )
    
    def _ensure_directories(self):
        """Create necessary directories if they don't exist."""
        os.makedirs(self.backup_path, exist_ok=True)
        os.makedirs(self.session_dir, exist_ok=True)
        
        # Ensure database directory exists
        db_dir = os.path.dirname(self.database_path)
        os.makedirs(db_dir, exist_ok=True)
        
        if self.download_media:
            os.makedirs(self.media_path, exist_ok=True)
    
    def should_backup_chat_type(self, is_user: bool, is_group: bool, is_channel: bool) -> bool:
        """
        Determine if a chat should be backed up based on its type.
        
        Args:
            is_user: True if chat is a private conversation
            is_group: True if chat is a group
            is_channel: True if chat is a channel
            
        Returns:
            True if chat should be backed up, False otherwise
        """
        if is_user and 'private' in self.chat_types:
            return True
        if is_group and 'groups' in self.chat_types:
            return True
        if is_channel and 'channels' in self.chat_types:
            return True
        return False
    
    def should_backup_chat(self, chat_id: int, is_user: bool, is_group: bool, is_channel: bool) -> bool:
        """
        Determine if a chat should be backed up based on its ID and type.
        
        Filtering logic (Priority Order):
        1. Global Exclude (Blacklist) -> Skip
        2. Type-Specific Exclude -> Skip
        3. Global Include (Whitelist) -> Backup
        4. Type-Specific Include -> Backup
        5. Chat Type Filter (CHAT_TYPES) -> Backup if matches
        
        Args:
            chat_id: Telegram chat ID
            is_user: True if chat is a private conversation
            is_group: True if chat is a group
            is_channel: True if chat is a channel
            
        Returns:
            True if chat should be backed up, False otherwise
        """
        # 1. Global Exclude
        if chat_id in self.global_exclude_ids:
            return False
            
        # 2. Type-Specific Exclude
        if is_user and chat_id in self.private_exclude_ids:
            return False
        if is_group and chat_id in self.groups_exclude_ids:
            return False
        if is_channel and chat_id in self.channels_exclude_ids:
            return False
            
        # 3. Global Include
        if chat_id in self.global_include_ids:
            return True
            
        # 4. Type-Specific Include
        if is_user and chat_id in self.private_include_ids:
            return True
        if is_group and chat_id in self.groups_include_ids:
            return True
        if is_channel and chat_id in self.channels_include_ids:
            return True
            
        # 5. Chat Type Filter
        return self.should_backup_chat_type(is_user, is_group, is_channel)
    
    def get_max_media_size_bytes(self) -> int:
        """Get maximum media file size in bytes."""
        return self.max_media_size_mb * 1024 * 1024

    def validate_credentials(self):
        """Ensure Telegram credentials are present."""
        if not all([self.api_id, self.api_hash, self.phone]):
            raise ValueError(
                "Missing required Telegram credentials (TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE). "
                "Please set them in your .env file."
            )


def setup_logging(config: Config):
    """
    Configure logging for the application.
    
    Args:
        config: Configuration object with log level
    """
    logging.basicConfig(
        level=config.log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Set Telethon logging to WARNING to reduce noise
    logging.getLogger('telethon').setLevel(logging.WARNING)


if __name__ == '__main__':
    # Test configuration loading
    try:
        config = Config()
        setup_logging(config)
        logger.info("Configuration test successful")
        logger.info(f"API ID: {config.api_id}")
        logger.info(f"Phone: {config.phone}")
        logger.info(f"Schedule: {config.schedule}")
        logger.info(f"Chat types: {config.chat_types}")
    except ValueError as e:
        print(f"Configuration error: {e}")
