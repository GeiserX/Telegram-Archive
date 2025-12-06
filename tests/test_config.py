import unittest
import os
from unittest.mock import patch
from src.config import Config

class TestConfig(unittest.TestCase):
    def setUp(self):
        # Clear relevant env vars
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_init_defaults(self):
        """Test configuration defaults when no env vars are set."""
        # We need to set at least one chat type or it raises ValueError
        with patch.dict(os.environ, {'CHAT_TYPES': 'private'}):
            config = Config()
            
            # Check if __init__ completed successfully (attributes exist)
            self.assertTrue(hasattr(config, 'log_level'))
            self.assertTrue(hasattr(config, 'backup_path'))
            self.assertTrue(hasattr(config, 'schedule'))
            
            # Check default values
            self.assertIsNone(config.api_id)
            self.assertIsNone(config.api_hash)
            self.assertIsNone(config.phone)

    def test_validate_credentials_missing(self):
        """Test validation fails when credentials are missing."""
        with patch.dict(os.environ, {'CHAT_TYPES': 'private'}):
            config = Config()
            with self.assertRaises(ValueError):
                config.validate_credentials()

    def test_validate_credentials_present(self):
        """Test validation passes when credentials are present."""
        env_vars = {
            'TELEGRAM_API_ID': '12345',
            'TELEGRAM_API_HASH': 'abcdef',
            'TELEGRAM_PHONE': '+1234567890',
            'CHAT_TYPES': 'private'
        }
        with patch.dict(os.environ, env_vars):
            config = Config()
            try:
                config.validate_credentials()
            except ValueError:
                self.fail("validate_credentials() raised ValueError unexpectedly!")

class TestDisplayChatIds(unittest.TestCase):
    """Test DISPLAY_CHAT_IDS configuration for viewer restriction."""

    def test_display_chat_ids_empty(self):
        """Display chat IDs defaults to empty set when not configured."""
        env_vars = {'CHAT_TYPES': 'private'}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, set())

    def test_display_chat_ids_single(self):
        """Can configure single chat ID for display."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789})

    def test_display_chat_ids_multiple(self):
        """Can configure multiple chat IDs for display."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789,987654321,-100555'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789, 987654321, -100555})


class TestDatabaseDir(unittest.TestCase):
    """Test DATABASE_DIR configuration for storage location."""

    def test_database_dir_default(self):
        """Database path defaults to backup path when not configured."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': '/data/backups'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith('/data/backups'))

    def test_database_dir_custom(self):
        """Can configure custom database directory."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': '/data/backups',
            'DATABASE_DIR': '/data/ssd'
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith('/data/ssd'))


if __name__ == '__main__':
    unittest.main()
