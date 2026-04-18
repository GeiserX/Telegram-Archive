import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.config import Config, build_telegram_client_kwargs, build_telegram_proxy_from_env


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


class TestTelegramProxyConfig(unittest.TestCase):
    """Test TELEGRAM_PROXY_* configuration parsing."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_proxy_defaults_to_none(self):
        """Proxy is disabled when TELEGRAM_PROXY_* vars are absent."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertIsNone(config.telegram_proxy)
            self.assertEqual(config.get_telegram_client_kwargs(), {})
            self.assertEqual(build_telegram_client_kwargs(), {})

    def test_proxy_parses_complete_socks5_config(self):
        """Complete SOCKS5 env vars produce a Telethon proxy dict."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
            "TELEGRAM_PROXY_USERNAME": "alice",
            "TELEGRAM_PROXY_PASSWORD": "secret",
            "TELEGRAM_PROXY_RDNS": "false",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()

        self.assertEqual(
            config.telegram_proxy,
            {
                "proxy_type": "socks5",
                "addr": "127.0.0.1",
                "port": 1080,
                "username": "alice",
                "password": "secret",
                "rdns": False,
            },
        )
        self.assertEqual(config.get_telegram_client_kwargs(), {"proxy": config.telegram_proxy})

    def test_proxy_requires_required_fields(self):
        """Partial proxy configuration should fail fast."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            Config()

        self.assertIn("Telegram proxy configuration is incomplete", str(ctx.exception))

    def test_proxy_rejects_invalid_port(self):
        """Proxy port must be numeric."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "bad-port",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            build_telegram_proxy_from_env()

        self.assertIn("TELEGRAM_PROXY_PORT must be a valid integer", str(ctx.exception))

    def test_proxy_rejects_port_zero(self):
        """Proxy port 0 is outside the valid TCP range."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "0",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            build_telegram_proxy_from_env()

        self.assertIn("TELEGRAM_PROXY_PORT must be between 1 and 65535", str(ctx.exception))

    def test_proxy_rejects_port_above_range(self):
        """Proxy port above 65535 should fail fast."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "65536",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            build_telegram_proxy_from_env()

        self.assertIn("TELEGRAM_PROXY_PORT must be between 1 and 65535", str(ctx.exception))

    def test_proxy_type_is_case_insensitive(self):
        """SOCKS5 should work regardless of input case."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "SOCKS5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            proxy = build_telegram_proxy_from_env()

        self.assertEqual(proxy["proxy_type"], "socks5")
        self.assertFalse(proxy["rdns"])

    def test_proxy_rejects_non_socks5_type(self):
        """Only SOCKS5 is supported by this config surface."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "http",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            build_telegram_proxy_from_env()

        self.assertIn("TELEGRAM_PROXY_TYPE must be 'socks5'", str(ctx.exception))

    def test_proxy_rejects_invalid_rdns(self):
        """Proxy RDNS must be a boolean-like value."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
            "TELEGRAM_PROXY_RDNS": "maybe",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            build_telegram_proxy_from_env()

        self.assertIn("TELEGRAM_PROXY_RDNS must be a boolean value", str(ctx.exception))

    def test_proxy_rejects_password_without_username(self):
        """Proxy auth requires username when password is set."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
            "TELEGRAM_PROXY_PASSWORD": "secret",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            Config()

        self.assertIn("TELEGRAM_PROXY_USERNAME and TELEGRAM_PROXY_PASSWORD", str(ctx.exception))

    def test_proxy_rejects_username_without_password(self):
        """Proxy auth requires password when username is set."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "127.0.0.1",
            "TELEGRAM_PROXY_PORT": "1080",
            "TELEGRAM_PROXY_USERNAME": "alice",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError) as ctx:
            Config()

        self.assertIn("TELEGRAM_PROXY_USERNAME and TELEGRAM_PROXY_PASSWORD", str(ctx.exception))


class TestSkipTopicIds(unittest.TestCase):
    """Test SKIP_TOPIC_IDS configuration for forum topic filtering."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skip_topic_ids_empty(self):
        """Skip topic IDs defaults to empty dict when not configured."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {})

    def test_skip_topic_ids_single(self):
        """Can configure single chat_id:topic_id pair."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-1001234567890: {42}})

    def test_skip_topic_ids_multiple_same_chat(self):
        """Multiple topics in same chat are grouped into one set."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42,-1001234567890:1337",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-1001234567890: {42, 1337}})

    def test_skip_topic_ids_multiple_chats(self):
        """Topics across different chats are separated by chat ID."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42,-1009876543210:7",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-1001234567890: {42}, -1009876543210: {7}})

    def test_skip_topic_ids_whitespace_handling(self):
        """Should handle whitespace in topic skip list correctly."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": " -100123:42 , -100456:7 ",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-100123: {42}, -100456: {7}})

    def test_skip_topic_ids_invalid_format_no_colon(self):
        """Raises ValueError for entries without colon separator."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError):
            Config()

    def test_skip_topic_ids_invalid_format_non_integer(self):
        """Raises ValueError for non-integer chat_id or topic_id."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "abc:def",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError):
            Config()

    def test_should_skip_topic_matches(self):
        """should_skip_topic returns True for configured pairs."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42,-1001234567890:1337",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_skip_topic(-1001234567890, 42))
            self.assertTrue(config.should_skip_topic(-1001234567890, 1337))

    def test_should_skip_topic_no_match(self):
        """should_skip_topic returns False for non-configured pairs."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_skip_topic(-1001234567890, 999))

    def test_should_skip_topic_none_topic(self):
        """should_skip_topic returns False when topic_id is None."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_skip_topic(-1001234567890, None))

    def test_should_skip_topic_empty_config(self):
        """should_skip_topic returns False when SKIP_TOPIC_IDS is not set."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_skip_topic(-1001234567890, 42))

    def test_should_skip_topic_wrong_chat(self):
        """should_skip_topic returns False when chat_id doesn't match."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890:42",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_skip_topic(-1009876543210, 42))

    # --- Edge cases for _parse_topic_skip_list ---

    def test_skip_topic_ids_duplicate_entries_are_deduplicated(self):
        """Duplicate chat_id:topic_id pairs should be silently deduplicated."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-100123:42,-100123:42,-100123:42",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-100123: {42}})

    def test_skip_topic_ids_extra_colons_raises_value_error(self):
        """Entry with multiple colons like chat_id:topic_id:extra raises ValueError."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-100123:42:extra",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Config()
            self.assertIn("must be integers", str(ctx.exception))

    def test_skip_topic_ids_very_large_ids(self):
        """Very large chat IDs and topic IDs should parse correctly."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1002701160643:999999",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-1002701160643: {999999}})
            self.assertTrue(config.should_skip_topic(-1002701160643, 999999))

    def test_skip_topic_ids_trailing_leading_commas(self):
        """Trailing, leading, and consecutive commas should be handled gracefully."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": ",-100123:42,,-100456:7,",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {-100123: {42}, -100456: {7}})

    def test_should_skip_topic_zero_topic_id(self):
        """should_skip_topic handles topic_id=0 as a valid integer match."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-100123:0",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            # topic_id=0 is falsy in Python, so should_skip_topic guard
            # "if topic_id is None" lets it through, but "topic_id in skip_set"
            # should match 0.
            self.assertTrue(config.should_skip_topic(-100123, 0))

    def test_skip_topic_ids_no_colon_error_message_includes_entry(self):
        """ValueError for missing colon should include the offending entry text."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "-1001234567890",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Config()
            self.assertIn("expected format chat_id:topic_id", str(ctx.exception))
            self.assertIn("-1001234567890", str(ctx.exception))

    def test_skip_topic_ids_non_integer_error_message_content(self):
        """ValueError for non-integer values should mention 'must be integers'."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "abc:def",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                Config()
            self.assertIn("must be integers", str(ctx.exception))
            self.assertIn("abc:def", str(ctx.exception))

    def test_skip_topic_ids_only_whitespace(self):
        """Whitespace-only SKIP_TOPIC_IDS should return empty dict."""
        env_vars = {
            "CHAT_TYPES": "private",
            "SKIP_TOPIC_IDS": "   ",
            "BACKUP_PATH": self.temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.skip_topic_ids, {})


class TestParseBoolTrueValues(unittest.TestCase):
    """Test _parse_bool returns True for truthy string values (line 24)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parse_bool_returns_true_for_yes(self):
        """_parse_bool returns True for 'yes' input."""
        from src.config import _parse_bool

        self.assertTrue(_parse_bool("yes"))

    def test_parse_bool_returns_true_for_on(self):
        """_parse_bool returns True for 'on' input."""
        from src.config import _parse_bool

        self.assertTrue(_parse_bool("on"))

    def test_parse_bool_returns_true_for_1(self):
        """_parse_bool returns True for '1' input."""
        from src.config import _parse_bool

        self.assertTrue(_parse_bool("1"))

    def test_parse_bool_returns_true_for_true(self):
        """_parse_bool returns True for 'true' input."""
        from src.config import _parse_bool

        self.assertTrue(_parse_bool("true"))


class TestBuildTelegramClientKwargsWithProxy(unittest.TestCase):
    """Test build_telegram_client_kwargs returns proxy dict when configured (line 95)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_returns_proxy_dict_when_configured(self):
        """build_telegram_client_kwargs returns proxy key when proxy env vars set."""
        env_vars = {
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_ADDR": "10.0.0.1",
            "TELEGRAM_PROXY_PORT": "9050",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            result = build_telegram_client_kwargs()
            self.assertIn("proxy", result)
            self.assertEqual(result["proxy"]["addr"], "10.0.0.1")
            self.assertEqual(result["proxy"]["port"], 9050)


class TestLogLevelWarnAlias(unittest.TestCase):
    """Test LOG_LEVEL=WARN is normalized to WARNING (line 200)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_warn_alias_maps_to_warning(self):
        """LOG_LEVEL=WARN should be treated as WARNING."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "LOG_LEVEL": "WARN"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.log_level, logging.WARNING)


class TestDatabasePathOverride(unittest.TestCase):
    """Test DATABASE_PATH env var takes priority over DATABASE_DIR (line 217)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_path_env_takes_priority(self):
        """DATABASE_PATH overrides both DATABASE_DIR and default."""
        custom_path = os.path.join(self.temp_dir, "custom", "mydb.sqlite")
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "DATABASE_PATH": custom_path,
            "DATABASE_DIR": "/should/be/ignored",
        }
        with patch.dict(os.environ, env_vars, clear=True), patch("os.makedirs"):
            config = Config()
            self.assertEqual(config.database_path, custom_path)


class TestWhitelistModeLogging(unittest.TestCase):
    """Test whitelist mode log paths when CHAT_IDS is set (lines 338-339)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_whitelist_mode_enabled_when_chat_ids_set(self):
        """Setting CHAT_IDS activates whitelist mode and populates chat_ids."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "CHAT_IDS": "-1001234567890,-1009876543210",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.whitelist_mode)
            self.assertEqual(config.chat_ids, {-1001234567890, -1009876543210})


class TestSyncDeletionsEditsLogging(unittest.TestCase):
    """Test SYNC_DELETIONS_EDITS warning log path (line 345)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sync_deletions_edits_enabled(self):
        """SYNC_DELETIONS_EDITS=true triggers the warning log path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "SYNC_DELETIONS_EDITS": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.sync_deletions_edits)


class TestVerifyMediaLogging(unittest.TestCase):
    """Test VERIFY_MEDIA info log path (line 349)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_verify_media_enabled(self):
        """VERIFY_MEDIA=true triggers the info log path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "VERIFY_MEDIA": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.verify_media)


class TestListenerLogging(unittest.TestCase):
    """Test ENABLE_LISTENER log paths (lines 351-363)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_listener_enabled_with_deletions(self):
        """ENABLE_LISTENER=true with LISTEN_DELETIONS=true covers deletion warning path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_DELETIONS": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertTrue(config.listen_deletions)

    def test_listener_enabled_without_deletions(self):
        """ENABLE_LISTENER=true with LISTEN_DELETIONS=false covers protected path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_DELETIONS": "false",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertFalse(config.listen_deletions)

    def test_listener_enabled_with_new_messages(self):
        """ENABLE_LISTENER=true with LISTEN_NEW_MESSAGES=true covers new messages path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_NEW_MESSAGES": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertTrue(config.listen_new_messages)

    def test_listener_enabled_without_new_messages(self):
        """ENABLE_LISTENER=true with LISTEN_NEW_MESSAGES=false covers disabled path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_NEW_MESSAGES": "false",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertFalse(config.listen_new_messages)

    def test_listener_enabled_with_chat_actions(self):
        """ENABLE_LISTENER=true with LISTEN_CHAT_ACTIONS=true covers chat actions path."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_CHAT_ACTIONS": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertTrue(config.listen_chat_actions)


class TestGetRequiredEnv(unittest.TestCase):
    """Test _get_required_env method (lines 445-456)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_required_env_returns_int(self):
        """_get_required_env converts value to int when requested."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "MY_INT_VAR": "42"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            result = config._get_required_env("MY_INT_VAR", int)
            self.assertEqual(result, 42)

    def test_get_required_env_returns_str(self):
        """_get_required_env returns string value when str type requested."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "MY_STR_VAR": "hello"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            result = config._get_required_env("MY_STR_VAR", str)
            self.assertEqual(result, "hello")

    def test_get_required_env_raises_when_not_set(self):
        """_get_required_env raises ValueError when env var is missing."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            with self.assertRaises(ValueError) as ctx:
                config._get_required_env("NONEXISTENT_VAR", str)
            self.assertIn("Required environment variable", str(ctx.exception))

    def test_get_required_env_raises_when_empty(self):
        """_get_required_env raises ValueError when env var is empty string."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "EMPTY_VAR": ""}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            with self.assertRaises(ValueError) as ctx:
                config._get_required_env("EMPTY_VAR", int)
            self.assertIn("Required environment variable", str(ctx.exception))

    def test_get_required_env_raises_on_invalid_int(self):
        """_get_required_env raises ValueError when int conversion fails."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "BAD_INT": "not_a_number"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            with self.assertRaises(ValueError) as ctx:
                config._get_required_env("BAD_INT", int)
            self.assertIn("must be a valid", str(ctx.exception))


class TestShouldBackupChatTypeBots(unittest.TestCase):
    """Test should_backup_chat_type with bots (line 496)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_bots_backed_up_when_in_chat_types(self):
        """should_backup_chat_type returns True for bots when 'bots' in CHAT_TYPES."""
        env_vars = {"CHAT_TYPES": "private,bots", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(
                config.should_backup_chat_type(is_user=False, is_group=False, is_channel=False, is_bot=True)
            )

    def test_bots_not_backed_up_when_not_in_chat_types(self):
        """should_backup_chat_type returns False for bots when 'bots' not in CHAT_TYPES."""
        env_vars = {"CHAT_TYPES": "private,groups", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(
                config.should_backup_chat_type(is_user=False, is_group=False, is_channel=False, is_bot=True)
            )


class TestShouldBackupChatFiltering(unittest.TestCase):
    """Test should_backup_chat with various filter modes (lines 539-570)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_whitelist_mode_includes_listed_chat(self):
        """In whitelist mode, chats in CHAT_IDS are backed up."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "CHAT_IDS": "100,200,300"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(200, is_user=True, is_group=False, is_channel=False))

    def test_whitelist_mode_excludes_unlisted_chat(self):
        """In whitelist mode, chats NOT in CHAT_IDS are excluded."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "CHAT_IDS": "100,200,300"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_backup_chat(999, is_user=True, is_group=False, is_channel=False))

    def test_global_exclude_takes_priority(self):
        """Global exclude blocks a chat even if type matches."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "GLOBAL_EXCLUDE_CHAT_IDS": "555",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_backup_chat(555, is_user=True, is_group=False, is_channel=False))

    def test_private_exclude_blocks_user_chat(self):
        """Per-type private exclude blocks a user chat."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "PRIVATE_EXCLUDE_CHAT_IDS": "777",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_backup_chat(777, is_user=True, is_group=False, is_channel=False))

    def test_private_exclude_blocks_bot_chat(self):
        """Per-type private exclude also blocks bot chats."""
        env_vars = {
            "CHAT_TYPES": "private,bots",
            "BACKUP_PATH": self.temp_dir,
            "PRIVATE_EXCLUDE_CHAT_IDS": "888",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(
                config.should_backup_chat(888, is_user=False, is_group=False, is_channel=False, is_bot=True)
            )

    def test_groups_exclude_blocks_group_chat(self):
        """Per-type groups exclude blocks a group chat."""
        env_vars = {
            "CHAT_TYPES": "groups",
            "BACKUP_PATH": self.temp_dir,
            "GROUPS_EXCLUDE_CHAT_IDS": "-100111",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_backup_chat(-100111, is_user=False, is_group=True, is_channel=False))

    def test_channels_exclude_blocks_channel_chat(self):
        """Per-type channels exclude blocks a channel chat."""
        env_vars = {
            "CHAT_TYPES": "channels",
            "BACKUP_PATH": self.temp_dir,
            "CHANNELS_EXCLUDE_CHAT_IDS": "-100222",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertFalse(config.should_backup_chat(-100222, is_user=False, is_group=False, is_channel=True))

    def test_global_include_acts_as_whitelist(self):
        """When GLOBAL_INCLUDE_CHAT_IDS is set, only listed chats pass."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "GLOBAL_INCLUDE_CHAT_IDS": "10,20",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(10, is_user=True, is_group=False, is_channel=False))
            self.assertFalse(config.should_backup_chat(99, is_user=True, is_group=False, is_channel=False))

    def test_private_include_limits_user_chats(self):
        """PRIVATE_INCLUDE_CHAT_IDS limits which user chats pass."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "PRIVATE_INCLUDE_CHAT_IDS": "50,60",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(50, is_user=True, is_group=False, is_channel=False))
            self.assertFalse(config.should_backup_chat(99, is_user=True, is_group=False, is_channel=False))

    def test_groups_include_limits_group_chats(self):
        """GROUPS_INCLUDE_CHAT_IDS limits which group chats pass."""
        env_vars = {
            "CHAT_TYPES": "groups",
            "BACKUP_PATH": self.temp_dir,
            "GROUPS_INCLUDE_CHAT_IDS": "-100500",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(-100500, is_user=False, is_group=True, is_channel=False))
            self.assertFalse(config.should_backup_chat(-100999, is_user=False, is_group=True, is_channel=False))

    def test_channels_include_limits_channel_chats(self):
        """CHANNELS_INCLUDE_CHAT_IDS limits which channel chats pass."""
        env_vars = {
            "CHAT_TYPES": "channels",
            "BACKUP_PATH": self.temp_dir,
            "CHANNELS_INCLUDE_CHAT_IDS": "-100600",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(-100600, is_user=False, is_group=False, is_channel=True))
            self.assertFalse(config.should_backup_chat(-100999, is_user=False, is_group=False, is_channel=True))

    def test_falls_through_to_chat_type_filter(self):
        """Without include/exclude lists, falls through to type-based filter."""
        env_vars = {"CHAT_TYPES": "private,groups", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.should_backup_chat(1, is_user=True, is_group=False, is_channel=False))
            self.assertTrue(config.should_backup_chat(2, is_user=False, is_group=True, is_channel=False))
            self.assertFalse(config.should_backup_chat(3, is_user=False, is_group=False, is_channel=True))


class TestGetMaxMediaSizeBytes(unittest.TestCase):
    """Test get_max_media_size_bytes conversion (line 574)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_100mb_in_bytes(self):
        """Default 100MB converts correctly to bytes."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.get_max_media_size_bytes(), 100 * 1024 * 1024)

    def test_custom_media_size_in_bytes(self):
        """Custom MAX_MEDIA_SIZE_MB converts correctly to bytes."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "MAX_MEDIA_SIZE_MB": "50"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.get_max_media_size_bytes(), 50 * 1024 * 1024)


class TestSetupLogging(unittest.TestCase):
    """Test setup_logging function (lines 618-625)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_setup_logging_sets_root_level(self):
        """setup_logging configures root logger and sets telethon to WARNING."""
        from src.config import setup_logging

        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "LOG_LEVEL": "DEBUG"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            setup_logging(config)
            telethon_logger = logging.getLogger("telethon")
            self.assertEqual(telethon_logger.level, logging.WARNING)

    def test_setup_logging_with_info_level(self):
        """setup_logging works with INFO level."""
        from src.config import setup_logging

        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "LOG_LEVEL": "INFO"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            setup_logging(config)
            self.assertEqual(config.log_level, logging.INFO)


class TestMainBlock(unittest.TestCase):
    """Test __main__ block execution path (lines 630-639)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_main_block_success(self):
        """Running config.py as __main__ with valid env succeeds."""
        import subprocess

        env = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            ["python3", "-m", "src.config"],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)

    def test_main_block_config_error(self):
        """Running config.py as __main__ with invalid config prints error."""
        import subprocess

        env = {
            "CHAT_TYPES": "invalid_type",
            "BACKUP_PATH": self.temp_dir,
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            ["python3", "-m", "src.config"],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=10,
        )
        self.assertIn("Configuration error", result.stdout)


class TestProxyMissingAddr(unittest.TestCase):
    """Test proxy validation when TELEGRAM_PROXY_ADDR is missing (line 48)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_proxy_missing_addr_in_error_message(self):
        """Missing TELEGRAM_PROXY_ADDR appears in the error message."""
        env_vars = {
            "TELEGRAM_PROXY_TYPE": "socks5",
            "TELEGRAM_PROXY_PORT": "1080",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with self.assertRaises(ValueError) as ctx:
                build_telegram_proxy_from_env()
            self.assertIn("TELEGRAM_PROXY_ADDR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
