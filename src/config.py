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
        # Required Telegram API credentials
        self.api_id = self._get_required_env('TELEGRAM_API_ID', int)
        self.api_hash = self._get_required_env('TELEGRAM_API_HASH', str)
        self.phone = self._get_required_env('TELEGRAM_PHONE', str)
        
        # Backup schedule (cron format)
        self.schedule = os.getenv('SCHEDULE', '0 */6 * * *')
        
        # Backup options
        self.backup_path = os.getenv('BACKUP_PATH', '/data/backups')
        self.download_media = os.getenv('DOWNLOAD_MEDIA', 'true').lower() == 'true'
        self.max_media_size_mb = int(os.getenv('MAX_MEDIA_SIZE_MB', '100'))
        
        # Batch processing configuration
        self.batch_size = int(os.getenv('BATCH_SIZE', '100'))
        
        # Chat type filters
        chat_types_str = os.getenv('CHAT_TYPES', 'private,groups,channels')
        self.chat_types = [ct.strip().lower() for ct in chat_types_str.split(',')]
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
        
        # Session configuration
        self.session_name = os.getenv('SESSION_NAME', 'telegram_backup')
        
        # Logging
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        self.log_level = getattr(logging, log_level, logging.INFO)
        
        # Derived paths
        # Store session in a separate directory from backups
        # If BACKUP_PATH is /data/backups, session goes to /data/session
        backup_parent = os.path.dirname(self.backup_path.rstrip('/\\'))
        self.session_dir = os.getenv('SESSION_DIR', os.path.join(backup_parent, 'session'))
        self.session_path = os.path.join(self.session_dir, f'{self.session_name}.session')
        
        self.database_path = os.path.join(self.backup_path, 'telegram_backup.db')
        self.media_path = os.path.join(self.backup_path, 'media')
        
        # Ensure directories exist
        self._ensure_directories()
        
        logger.info("Configuration loaded successfully")
        logger.debug(f"Backup path: {self.backup_path}")
        logger.debug(f"Download media: {self.download_media}")
        logger.debug(f"Chat types: {self.chat_types}")
        logger.debug(f"Schedule: {self.schedule}")
    
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
        """Validate that chat types are valid options."""
        valid_types = {'private', 'groups', 'channels'}
        invalid_types = set(self.chat_types) - valid_types
        
        if invalid_types:
            raise ValueError(
                f"Invalid chat types: {invalid_types}. "
                f"Valid options are: {valid_types}"
            )
        
        if not self.chat_types:
            raise ValueError("At least one chat type must be specified in CHAT_TYPES")
    
    def _ensure_directories(self):
        """Create necessary directories if they don't exist."""
        os.makedirs(self.backup_path, exist_ok=True)
        os.makedirs(self.session_dir, exist_ok=True)
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
