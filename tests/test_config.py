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

if __name__ == '__main__':
    unittest.main()
