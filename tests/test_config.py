import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.config import Config


class TestConfig(unittest.TestCase):
    def setUp(self):
        # Create a temp directory for safe file operations
        self.temp_dir = tempfile.mkdtemp()

        # Clear relevant env vars but set safe defaults for paths
        self.env_patcher = patch.dict(
            os.environ, {"BACKUP_PATH": self.temp_dir, "DATABASE_DIR": self.temp_dir}, clear=True
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_defaults(self):
        """Test configuration defaults when no env vars are set."""
        # We need to set at least one chat type or it raises ValueError
        # We also need to unset BACKUP_PATH/DATABASE_DIR to test defaults,
        # BUT we must mock makedirs to avoid PermissionError on /data
        with patch("os.makedirs"), patch.dict(os.environ, {"CHAT_TYPES": "private"}, clear=True):
            config = Config()

            # Check if __init__ completed successfully (attributes exist)
            self.assertTrue(hasattr(config, "log_level"))
            self.assertTrue(hasattr(config, "backup_path"))
            self.assertTrue(hasattr(config, "schedule"))

            # Check default values
            self.assertIsNone(config.api_id)
            self.assertIsNone(config.api_hash)
            self.assertIsNone(config.phone)

    def test_validate_credentials_missing(self):
        """Test validation fails when credentials are missing."""
        # Config init will try to create dirs, so we rely on setUp's temp paths
        with patch.dict(os.environ, {"CHAT_TYPES": "private"}):
            config = Config()
            with self.assertRaises(ValueError):
                config.validate_credentials()

    def test_validate_credentials_present(self):
        """Test validation passes when credentials are present."""
        env_vars = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "CHAT_TYPES": "private",
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
            "CHAT_TYPES": "",  # Empty = whitelist-only mode
            "GROUPS_INCLUDE_CHAT_IDS": "-1001234567",
            "BACKUP_PATH": self.temp_dir,
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
        env_vars = {"CHAT_TYPES": "", "GROUPS_INCLUDE_CHAT_IDS": "-1001234567", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should backup the explicitly included group
            self.assertTrue(config.should_backup_chat(-1001234567, is_user=False, is_group=True, is_channel=False))
            # Should NOT backup other groups
            self.assertFalse(config.should_backup_chat(-1009999999, is_user=False, is_group=True, is_channel=False))

    def test_chat_types_invalid_raises_error(self):
        """Invalid chat types should raise ValueError."""
        env_vars = {"CHAT_TYPES": "invalid,types", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Config()
            self.assertIn("Invalid chat types", str(ctx.exception))

    def test_chat_types_not_set_uses_default(self):
        """When CHAT_TYPES is not set at all, should use default (all types)."""
        env_vars = {
            "BACKUP_PATH": self.temp_dir
            # CHAT_TYPES deliberately NOT set
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should default to all three types
            self.assertEqual(set(config.chat_types), {"private", "groups", "channels"})
            # Should backup all types
            self.assertTrue(config.should_backup_chat_type(is_user=True, is_group=False, is_channel=False))
            self.assertTrue(config.should_backup_chat_type(is_user=False, is_group=True, is_channel=False))
            self.assertTrue(config.should_backup_chat_type(is_user=False, is_group=False, is_channel=True))


class TestDisplayChatIds(unittest.TestCase):
    """Test DISPLAY_CHAT_IDS configuration for viewer restriction."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_display_chat_ids_empty(self):
        """Display chat IDs defaults to empty set when not configured."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, set())

    def test_display_chat_ids_single(self):
        """Can configure single chat ID for display."""
        env_vars = {"CHAT_TYPES": "private", "DISPLAY_CHAT_IDS": "123456789", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.display_chat_ids, {123456789})

    def test_display_chat_ids_multiple(self):
        """Can configure multiple chat IDs for display."""
        env_vars = {
            "CHAT_TYPES": "private",
            "DISPLAY_CHAT_IDS": "123456789,987654321,-100555",
            "BACKUP_PATH": self.temp_dir,
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

        env_vars = {"CHAT_TYPES": "private"}
        with patch("os.makedirs") as mock_makedirs, patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Verify it picked up the default
            self.assertTrue(config.database_path.startswith("/data/backups"))

    def test_database_dir_custom(self):
        """Can configure custom database directory."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": "/data/backups", "DATABASE_DIR": "/data/ssd"}
        with patch("os.makedirs") as mock_makedirs, patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith("/data/ssd"))


class TestSkipMediaChatIds(unittest.TestCase):
    """Test SKIP_MEDIA_CHAT_IDS configuration for media filtering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skip_media_chat_ids_empty(self):
        """Skip media chat IDs defaults to empty set when not configured."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_media_chat_ids, set())

    def test_skip_media_chat_ids_single(self):
        """Can configure single chat ID to skip media."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_MEDIA_CHAT_IDS": "-1001234567890",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_media_chat_ids, {-1001234567890})

    def test_skip_media_chat_ids_multiple(self):
        """Can configure multiple chat IDs to skip media."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_MEDIA_CHAT_IDS": "-1001234567890,-1009876543210,123456",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_media_chat_ids, {-1001234567890, -1009876543210, 123456})

    def test_should_download_media_for_chat_normal(self):
        """Should download media for chats not in skip list."""
        env_vars = {
            "CHAT_TYPES": "private",
            "DOWNLOAD_MEDIA": "true",
            "SKIP_MEDIA_CHAT_IDS": "-1001234567890",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should download for chats not in skip list
            self.assertTrue(config.should_download_media_for_chat(123456))
            self.assertTrue(config.should_download_media_for_chat(-1009999999))

    def test_should_download_media_for_chat_skipped(self):
        """Should NOT download media for chats in skip list."""
        env_vars = {
            "CHAT_TYPES": "private",
            "DOWNLOAD_MEDIA": "true",
            "SKIP_MEDIA_CHAT_IDS": "-1001234567890,-1009876543210",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should NOT download for chats in skip list
            self.assertFalse(config.should_download_media_for_chat(-1001234567890))
            self.assertFalse(config.should_download_media_for_chat(-1009876543210))

    def test_should_download_media_respects_global_flag(self):
        """Should respect DOWNLOAD_MEDIA=false even if not in skip list."""
        env_vars = {
            "CHAT_TYPES": "private",
            "DOWNLOAD_MEDIA": "false",
            "SKIP_MEDIA_CHAT_IDS": "-1001234567890",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # Should NOT download for ANY chat when global flag is false
            self.assertFalse(config.should_download_media_for_chat(123456))
            self.assertFalse(config.should_download_media_for_chat(-1009999999))
            self.assertFalse(config.should_download_media_for_chat(-1001234567890))

    def test_skip_media_chat_ids_whitespace_handling(self):
        """Should handle whitespace in chat ID list correctly."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_MEDIA_CHAT_IDS": " -1001234567890 , -1009876543210 , 123456 ",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_media_chat_ids, {-1001234567890, -1009876543210, 123456})

    def test_skip_media_delete_existing_defaults_true(self):
        """SKIP_MEDIA_DELETE_EXISTING defaults to true when not set."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.skip_media_delete_existing)

    def test_skip_media_delete_existing_can_be_disabled(self):
        """Can disable SKIP_MEDIA_DELETE_EXISTING to keep existing media."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_MEDIA_DELETE_EXISTING": "false",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.skip_media_delete_existing)

    def test_skip_media_delete_existing_explicit_true(self):
        """Can explicitly enable SKIP_MEDIA_DELETE_EXISTING."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_MEDIA_DELETE_EXISTING": "true",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.skip_media_delete_existing)


class TestCheckpointInterval(unittest.TestCase):
    """Test CHECKPOINT_INTERVAL configuration for backup progress saving."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_checkpoint_interval_default(self):
        """CHECKPOINT_INTERVAL defaults to 1 when not set."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.checkpoint_interval, 1)

    def test_checkpoint_interval_custom(self):
        """Can configure a custom checkpoint interval."""
        env_vars = {"CHAT_TYPES": "private", "CHECKPOINT_INTERVAL": "5", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.checkpoint_interval, 5)

    def test_checkpoint_interval_minimum_one(self):
        """CHECKPOINT_INTERVAL is clamped to minimum of 1."""
        env_vars = {"CHAT_TYPES": "private", "CHECKPOINT_INTERVAL": "0", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.checkpoint_interval, 1)

    def test_checkpoint_interval_negative_clamped(self):
        """Negative CHECKPOINT_INTERVAL is clamped to 1."""
        env_vars = {"CHAT_TYPES": "private", "CHECKPOINT_INTERVAL": "-3", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.checkpoint_interval, 1)


if __name__ == "__main__":
    unittest.main()
