import unittest
import os
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from src.config import Config

class TestConfig(unittest.TestCase):
    def setUp(self):
        # Create a temp directory for safe file operations
        self.temp_dir = tempfile.mkdtemp()
        
        # Clear relevant env vars but set safe defaults for paths
        self.env_patcher = patch.dict(os.environ, {
            'BACKUP_PATH': self.temp_dir,
            'DATABASE_DIR': self.temp_dir
        }, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_defaults(self):
        """Test configuration defaults when no env vars are set."""
        # We need to set at least one chat type or it raises ValueError
        # We also need to unset BACKUP_PATH/DATABASE_DIR to test defaults, 
        # BUT we must mock makedirs to avoid PermissionError on /data
        with patch('os.makedirs'):
            with patch.dict(os.environ, {'CHAT_TYPES': 'private'}, clear=True):
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
        # Config init will try to create dirs, so we rely on setUp's temp paths
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

class TestChatTypes(unittest.TestCase):
    """Test CHAT_TYPES configuration for filtering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_chat_types_empty_for_whitelist_mode(self):
        """Empty CHAT_TYPES should work for whitelist-only mode (issue #5)."""
        env_vars = {
            'CHAT_TYPES': '',  # Empty = whitelist-only mode
            'GROUPS_INCLUDE_CHAT_IDS': '-1001234567',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.chat_types, [])
            self.assertEqual(config.groups_include_ids, {-1001234567})
            # Should not backup any chat type by default
            self.assertFalse(config.should_backup_chat_type(is_user=True, is_group=False, is_channel=False))
            self.assertFalse(config.should_backup_chat_type(is_user=False, is_group=True, is_channel=False))
            self.assertFalse(config.should_backup_chat_type(is_user=False, is_group=False, is_channel=True))

    def test_chat_types_whitelist_only_backup_included_ids(self):
        """With empty CHAT_TYPES, should backup explicitly included IDs."""
        env_vars = {
            'CHAT_TYPES': '',
            'GROUPS_INCLUDE_CHAT_IDS': '-1001234567',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should backup the explicitly included group
            self.assertTrue(config.should_backup_chat(-1001234567, is_user=False, is_group=True, is_channel=False))
            # Should NOT backup other groups
            self.assertFalse(config.should_backup_chat(-1009999999, is_user=False, is_group=True, is_channel=False))

    def test_chat_types_invalid_raises_error(self):
        """Invalid chat types should raise ValueError."""
        env_vars = {
            'CHAT_TYPES': 'invalid,types',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Config()
            self.assertIn('Invalid chat types', str(ctx.exception))


class TestDisplayChatIds(unittest.TestCase):
    """Test DISPLAY_CHAT_IDS configuration for viewer restriction."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_display_chat_ids_empty(self):
        """Display chat IDs defaults to empty set when not configured."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, set())

    def test_display_chat_ids_single(self):
        """Can configure single chat ID for display."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789})

    def test_display_chat_ids_multiple(self):
        """Can configure multiple chat IDs for display."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'DISPLAY_CHAT_IDS': '123456789,987654321,-100555',
            'BACKUP_PATH': self.temp_dir
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789, 987654321, -100555})


class TestDatabaseDir(unittest.TestCase):
    """Test DATABASE_DIR configuration for storage location."""

    def test_database_dir_default(self):
        """Database path defaults to backup path when not configured."""
        # For this test we want to assert it DEFAULTS to /data/backups (or whatever default is)
        # So we must NOT set BACKUP_PATH in env, but we MUST mock makedirs to prevent error
        
        env_vars = {
            'CHAT_TYPES': 'private'
        }
        with patch('os.makedirs') as mock_makedirs:
            with patch.dict(os.environ, env_vars, clear=True):
                config = Config()
                # Verify it picked up the default
                self.assertTrue(config.database_path.startswith('/data/backups'))

    def test_database_dir_custom(self):
        """Can configure custom database directory."""
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': '/data/backups',
            'DATABASE_DIR': '/data/ssd'
        }
        with patch('os.makedirs') as mock_makedirs:
            with patch.dict(os.environ, env_vars, clear=True):
                config = Config()
                self.assertTrue(config.database_path.startswith('/data/ssd'))


if __name__ == '__main__':
    unittest.main()
