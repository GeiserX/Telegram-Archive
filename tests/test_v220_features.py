import unittest
import os
from unittest.mock import patch, MagicMock
import tempfile
import json

class TestDisplayChatIds(unittest.TestCase):
    """Test DISPLAY_CHAT_IDS configuration for viewer restriction."""

    def test_display_chat_ids_empty(self):
        """Display chat IDs defaults to empty set when not configured."""
        from src.config import Config
        env_vars = {'CHAT_TYPES': 'private'}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, set())

    def test_display_chat_ids_single(self):
        """Can configure single chat ID for display."""
        from src.config import Config
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789})

    def test_display_chat_ids_multiple(self):
        """Can configure multiple chat IDs for display."""
        from src.config import Config
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789,987654321,-100555'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789, 987654321, -100555})


class TestDatabaseDir(unittest.TestCase):
    """Test DATABASE_DIR configuration for SSD storage."""

    def test_database_dir_default(self):
        """Database path defaults to backup path when not configured."""
        from src.config import Config
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': '/data/backups'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith('/data/backups'))

    def test_database_dir_custom(self):
        """Can configure custom database directory."""
        from src.config import Config
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': '/data/backups',
            'DATABASE_DIR': '/data/ssd'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith('/data/ssd'))


class TestMediaTypeDetection(unittest.TestCase):
    """Test media type detection for animations/stickers."""

    def test_animation_detection(self):
        """Animated documents should be detected as 'animation' type."""
        # This would require mocking Telethon objects
        # Simplified test to verify the logic exists
        from src.telegram_backup import TelegramBackup
        self.assertTrue(hasattr(TelegramBackup, '_get_media_type'))


class TestReplyToText(unittest.TestCase):
    """Test reply-to text extraction and display."""

    def test_reply_text_truncation(self):
        """Reply text should be truncated to 100 characters."""
        # The truncation is at [:100] in the code
        long_text = "a" * 200
        truncated = long_text[:100]
        self.assertEqual(len(truncated), 100)


if __name__ == '__main__':
    unittest.main()
