import logging
import os
import shutil
import sys
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
            self.assertTrue(config.database_path.startswith(os.path.abspath("/data/backups")))

    def test_database_dir_custom(self):
        """Can configure custom database directory."""
        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": "/data/backups", "DATABASE_DIR": "/data/ssd"}
        with patch("os.makedirs") as mock_makedirs, patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.database_path.startswith(os.path.abspath("/data/ssd")))


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
            self.assertEqual(config.get_telegram_client_kwargs(), {"flood_sleep_threshold": 0})
            self.assertEqual(build_telegram_client_kwargs(), {"flood_sleep_threshold": 0})

    def test_proxy_rdns_false_alone_does_not_enable_proxy(self):
        """Regression for #193: stock docker-compose injects
        TELEGRAM_PROXY_RDNS=false. On its own (no host/port) this must NOT be
        treated as a proxy request, so a default install with only API creds
        does not raise "incomplete proxy configuration"."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_RDNS": "false",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertIsNone(config.telegram_proxy)
            self.assertIsNone(build_telegram_proxy_from_env())
            self.assertEqual(config.get_telegram_client_kwargs(), {"flood_sleep_threshold": 0})

    def test_proxy_rdns_true_alone_does_not_enable_proxy(self):
        """rdns is a modifier, not an enabler: rdns=true with no host/port
        leaves the proxy disabled rather than raising for missing fields."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "TELEGRAM_PROXY_RDNS": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            self.assertIsNone(build_telegram_proxy_from_env())

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
        self.assertEqual(
            config.get_telegram_client_kwargs(),
            {"flood_sleep_threshold": 0, "proxy": config.telegram_proxy},
        )

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
            self.assertIn("flood_sleep_threshold", result)
            self.assertEqual(result["flood_sleep_threshold"], 0)


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

    def test_listener_deletions_default_false(self):
        """LISTEN_DELETIONS defaults to false to preserve archive data."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertTrue(config.enable_listener)
            self.assertFalse(config.listen_deletions)
            self.assertEqual(config.deletion_mode, "hard")

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
            self.assertEqual(config.deletion_mode, "hard")

    def test_deletion_mode_soft(self):
        """DELETION_MODE=soft marks deleted messages instead of hard deleting them."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "ENABLE_LISTENER": "true",
            "LISTEN_DELETIONS": "true",
            "DELETION_MODE": "soft",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.deletion_mode, "soft")

    def test_deletion_mode_invalid_raises(self):
        """DELETION_MODE only accepts soft or hard."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "DELETION_MODE": "archive",
        }
        with patch.dict(os.environ, env_vars, clear=True), self.assertRaises(ValueError):
            Config()

    def test_deletion_mode_normalizes_case_and_whitespace(self):
        """DELETION_MODE is stripped and lower-cased before validation."""
        env_vars = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "DELETION_MODE": "  Soft  ",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.deletion_mode, "soft")

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

    def test_zero_means_no_limit(self):
        """0 disables the cap — the meaning it carries everywhere else in this
        config. It used to mean 'skip every file with a nonzero size'."""
        import sys

        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "MAX_MEDIA_SIZE_MB": "0"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.get_max_media_size_bytes(), sys.maxsize)

    def test_negative_means_no_limit(self):
        import sys

        env_vars = {"CHAT_TYPES": "private", "BACKUP_PATH": self.temp_dir, "MAX_MEDIA_SIZE_MB": "-5"}
        with patch.dict(os.environ, env_vars, clear=True):
            config = Config()
            self.assertEqual(config.get_max_media_size_bytes(), sys.maxsize)


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
            [sys.executable, "-m", "src.config"],
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
            [sys.executable, "-m", "src.config"],
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


def _get_base_env(temp_dir: str) -> dict:
    return {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": temp_dir,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "abcdef",
        "TELEGRAM_PHONE": "+1234567890",
    }


class TestMediaFloodSleepThreshold(unittest.TestCase):
    """MEDIA_FLOOD_SLEEP_THRESHOLD (#232) parsing, and the #124 kwargs pin."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_is_60(self):
        with patch.dict(os.environ, _get_base_env(self.temp_dir), clear=True):
            config = Config()
        self.assertEqual(config.media_flood_sleep_threshold, 60)

    def test_env_override(self):
        env = _get_base_env(self.temp_dir) | {"MEDIA_FLOOD_SLEEP_THRESHOLD": "300"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.media_flood_sleep_threshold, 300)

    def test_env_zero_restores_raise_immediately(self):
        env = _get_base_env(self.temp_dir) | {"MEDIA_FLOOD_SLEEP_THRESHOLD": "0"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.media_flood_sleep_threshold, 0)

    def test_client_kwargs_keep_flood_sleep_threshold_zero(self):
        """#124 pin: the media threshold must never leak into the client kwargs —
        absorption is scoped to absorb_media_floods, not the whole client."""
        env = _get_base_env(self.temp_dir) | {"MEDIA_FLOOD_SLEEP_THRESHOLD": "300"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            self.assertEqual(config.get_telegram_client_kwargs().get("flood_sleep_threshold"), 0)
            self.assertEqual(build_telegram_client_kwargs().get("flood_sleep_threshold"), 0)


class TestDialogFloodSleepThreshold(unittest.TestCase):
    """DIALOG_FLOOD_SLEEP_THRESHOLD (#295) parsing, and the #124 kwargs pin."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_is_60(self):
        with patch.dict(os.environ, _get_base_env(self.temp_dir), clear=True):
            config = Config()
        self.assertEqual(config.dialog_flood_sleep_threshold, 60)

    def test_env_override(self):
        env = _get_base_env(self.temp_dir) | {"DIALOG_FLOOD_SLEEP_THRESHOLD": "300"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.dialog_flood_sleep_threshold, 300)

    def test_env_zero_restores_raise_immediately(self):
        env = _get_base_env(self.temp_dir) | {"DIALOG_FLOOD_SLEEP_THRESHOLD": "0"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.dialog_flood_sleep_threshold, 0)

    def test_client_kwargs_keep_flood_sleep_threshold_zero(self):
        """#124 pin: the dialog threshold must never leak into the client kwargs —
        absorption is scoped to absorb_media_floods, not the whole client."""
        env = _get_base_env(self.temp_dir) | {"DIALOG_FLOOD_SLEEP_THRESHOLD": "300"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            self.assertEqual(config.get_telegram_client_kwargs().get("flood_sleep_threshold"), 0)
            self.assertEqual(build_telegram_client_kwargs().get("flood_sleep_threshold"), 0)


class TestWhitelistResolveDialogLimit(unittest.TestCase):
    """WHITELIST_RESOLVE_DIALOG_LIMIT (#234) parsing."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_is_1000(self):
        with patch.dict(os.environ, _get_base_env(self.temp_dir), clear=True):
            config = Config()
        self.assertEqual(config.whitelist_resolve_dialog_limit, 1000)

    def test_env_override(self):
        env = _get_base_env(self.temp_dir) | {"WHITELIST_RESOLVE_DIALOG_LIMIT": "250"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.whitelist_resolve_dialog_limit, 250)

    def test_env_zero_disables(self):
        env = _get_base_env(self.temp_dir) | {"WHITELIST_RESOLVE_DIALOG_LIMIT": "0"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.whitelist_resolve_dialog_limit, 0)


def _account_triple(index: int) -> dict:
    """A complete TG_ACCOUNT_<index>_* credential triple with obviously-fake values."""
    return {
        f"TG_ACCOUNT_{index}_API_ID": str(10000 + index),
        f"TG_ACCOUNT_{index}_API_HASH": f"test-hash-account-{index}",
        f"TG_ACCOUNT_{index}_PHONE_NUMBER": f"+3460000000{index}",
    }


class TestMultiAccountConfig(unittest.TestCase):
    """TG_ACCOUNT_<N>_* parsing (v8.0.0 multi-account) and the zero-config upgrade.

    Two rules run through every test here:
    - Zero-config (no TG_ACCOUNT_* set) must be byte-identical to 7.x: one
      synthesized account with the legacy session resolution, no new log lines.
    - Error messages and reprs name VARIABLES, never values — phone numbers are
      PII and the hash is a credential (#272), so each error test also asserts
      the offending VALUE is absent from the exception text.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # SESSION_DIR keeps Config._ensure_directories inside the temp dir and
        # makes session_path assertions independent of BACKUP_PATH's parent.
        self.session_dir = os.path.join(self.temp_dir, "session")
        self.base_env = {
            "CHAT_TYPES": "private",
            "BACKUP_PATH": self.temp_dir,
            "SESSION_DIR": self.session_dir,
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Zero-config synthesis (the 7.x upgrade path)
    # ------------------------------------------------------------------

    def test_legacy_only_env_synthesizes_one_account_with_legacy_session(self):
        """No TG_ACCOUNT_*: exactly one account from TELEGRAM_*, same session file."""
        env = self.base_env | {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(len(config.accounts), 1)
        account = config.accounts[0]
        self.assertEqual(account.index, 1)
        self.assertEqual(account.api_id, 12345)
        self.assertEqual(account.api_hash, "abcdef")
        self.assertEqual(account.phone, "+1234567890")
        self.assertEqual(account.label, "default")
        self.assertEqual(account.session_name, "telegram_backup")
        self.assertEqual(account.session_name, config.session_name)
        # String-identical to config.session_path: same session file, no re-login.
        self.assertEqual(account.session_path, config.session_path)
        self.assertFalse(config._indexed_accounts)

    def test_legacy_only_env_honors_session_name_env(self):
        env = self.base_env | {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
            "SESSION_NAME": "my_session",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.accounts[0].session_name, "my_session")
        self.assertEqual(config.accounts[0].session_path, config.session_path)

    def test_viewer_without_credentials_still_gets_one_account(self):
        """The viewer builds Config with no credentials at all; the synthesized
        account carries Nones rather than the parse raising."""
        with patch.dict(os.environ, dict(self.base_env), clear=True):
            config = Config()
        self.assertEqual(len(config.accounts), 1)
        account = config.accounts[0]
        self.assertIsNone(account.api_id)
        self.assertIsNone(account.api_hash)
        self.assertIsNone(account.phone)
        self.assertEqual(account.session_name, "telegram_backup")

    # ------------------------------------------------------------------
    # Indexed accounts: session-name and label chains
    # ------------------------------------------------------------------

    def test_two_indexed_accounts_default_sessions_and_labels(self):
        env = self.base_env | _account_triple(1) | _account_triple(2)
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertTrue(config._indexed_accounts)
        self.assertEqual([a.index for a in config.accounts], [1, 2])
        one, two = config.accounts
        # Account 1 keeps the legacy default so no existing deployment re-logins.
        self.assertEqual(one.session_name, "telegram_backup")
        self.assertEqual(two.session_name, "telegram_backup_account2")
        self.assertEqual(one.label, "default")
        self.assertEqual(two.label, "account2")
        self.assertEqual(one.api_id, 10001)
        self.assertEqual(two.api_id, 10002)
        for account in config.accounts:
            self.assertEqual(account.session_path, os.path.join(config.session_dir, account.session_name))

    def test_account_one_explicit_session_name_wins(self):
        env = self.base_env | _account_triple(1) | _account_triple(2) | {"TG_ACCOUNT_1_SESSION_NAME": "primary"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.accounts[0].session_name, "primary")

    def test_account_one_falls_back_to_session_name_env(self):
        """Full legacy chain for account 1: TG_ACCOUNT_1_SESSION_NAME →
        SESSION_NAME env → 'telegram_backup'. Accounts 2+ never read SESSION_NAME."""
        env = self.base_env | _account_triple(1) | _account_triple(2) | {"SESSION_NAME": "legacy_sess"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(config.accounts[0].session_name, "legacy_sess")
        self.assertEqual(config.accounts[1].session_name, "telegram_backup_account2")

    def test_labels_come_from_env_when_set(self):
        env = (
            self.base_env
            | _account_triple(1)
            | _account_triple(2)
            | {"TG_ACCOUNT_1_LABEL": "personal", "TG_ACCOUNT_2_LABEL": "work"}
        )
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual([a.label for a in config.accounts], ["personal", "work"])

    # ------------------------------------------------------------------
    # Error matrix — every message names variables, never values
    # ------------------------------------------------------------------

    def _assert_no_credential_values(self, message: str, *envs: dict):
        """#272: no phone number or hash VALUE may appear in exception text."""
        for env in envs:
            for key, value in env.items():
                if value and ("PHONE_NUMBER" in key or "API_HASH" in key or key.startswith("TELEGRAM_")):
                    self.assertNotIn(value, message, f"value of {key} leaked into the error text")

    def test_index_gap_is_a_loud_error_naming_the_missing_variable(self):
        env = self.base_env | _account_triple(1) | _account_triple(3)
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        message = str(ctx.exception)
        self.assertIn("contiguous", message)
        self.assertIn("TG_ACCOUNT_2_API_ID", message)
        self._assert_no_credential_values(message, env)

    def test_indexes_must_start_at_one(self):
        env = self.base_env | _account_triple(2)
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        message = str(ctx.exception)
        self.assertIn("contiguous", message)
        self.assertIn("TG_ACCOUNT_1_API_ID", message)

    def test_partial_triple_is_a_loud_error_naming_the_variables(self):
        env = self.base_env | _account_triple(1) | {"TG_ACCOUNT_2_API_ID": "10002"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        message = str(ctx.exception)
        self.assertIn("Account 2 is incomplete", message)
        self.assertIn("TG_ACCOUNT_2_API_HASH", message)
        self.assertIn("TG_ACCOUNT_2_PHONE_NUMBER", message)
        self._assert_no_credential_values(message, env)

    def test_duplicate_phone_is_a_loud_error_without_the_number(self):
        env = self.base_env | _account_triple(1) | _account_triple(2)
        env["TG_ACCOUNT_2_PHONE_NUMBER"] = env["TG_ACCOUNT_1_PHONE_NUMBER"]
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        message = str(ctx.exception)
        self.assertIn("TG_ACCOUNT_2_PHONE_NUMBER", message)
        self.assertIn("TG_ACCOUNT_1_PHONE_NUMBER", message)
        self._assert_no_credential_values(message, env)

    def test_non_integer_api_id_error_never_echoes_the_value(self):
        env = self.base_env | _account_triple(1)
        env["TG_ACCOUNT_1_API_ID"] = "not-an-int-credential"
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        message = str(ctx.exception)
        self.assertIn("TG_ACCOUNT_1_API_ID must be an integer", message)
        self.assertNotIn("not-an-int-credential", message)
        # ``raise ... from None``: the chained int() text (which echoes the
        # value) must not ride along as __cause__/__context__.
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_unrecognized_variable_shapes_raise(self):
        for bad_key in ("TG_ACCOUNT_2_APIHASH", "TG_ACCOUNT_0_API_ID", "TG_ACCOUNT_01_API_ID"):
            with self.subTest(bad_key=bad_key):
                env = self.base_env | _account_triple(1) | {bad_key: "anything"}
                with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
                    Config()
                message = str(ctx.exception)
                self.assertIn("Unrecognized account variable", message)
                self.assertIn(bad_key, message)

    def test_session_collision_is_a_loud_error(self):
        """Account 2 explicitly naming account 1's resolved default ('telegram_backup')
        would put two clients on one Telethon SQLite session."""
        env = self.base_env | _account_triple(1) | _account_triple(2)
        env["TG_ACCOUNT_2_SESSION_NAME"] = "telegram_backup"
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError) as ctx:
            Config()
        self.assertIn("same session file", str(ctx.exception))
        self.assertIn("TG_ACCOUNT_2_SESSION_NAME", str(ctx.exception))

    def test_empty_values_are_treated_as_absent(self):
        """docker-compose's ${VAR:-} idiom injects empty strings; an index whose
        variables are all empty is undeclared, and an empty LABEL falls back."""
        env = self.base_env | _account_triple(1) | _account_triple(2)
        env |= {
            "TG_ACCOUNT_3_API_ID": "",
            "TG_ACCOUNT_3_API_HASH": "",
            "TG_ACCOUNT_3_PHONE_NUMBER": "",
            "TG_ACCOUNT_2_LABEL": "",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertEqual(len(config.accounts), 2)
        self.assertEqual(config.accounts[1].label, "account2")

    # ------------------------------------------------------------------
    # Precedence, validate_credentials, logging, repr
    # ------------------------------------------------------------------

    def test_indexed_wins_over_legacy_but_config_triple_reflects_legacy(self):
        """Both set: accounts come from TG_ACCOUNT_*; config.api_id/api_hash/phone
        still reflect TELEGRAM_* (and must not be read for capture)."""
        env = (
            self.base_env
            | {
                "TELEGRAM_API_ID": "12345",
                "TELEGRAM_API_HASH": "abcdef",
                "TELEGRAM_PHONE": "+1234567890",
            }
            | _account_triple(1)
            | _account_triple(2)
        )
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        self.assertTrue(config._indexed_accounts)
        self.assertEqual([a.api_id for a in config.accounts], [10001, 10002])
        self.assertEqual(config.api_id, 12345)
        self.assertEqual(config.api_hash, "abcdef")
        self.assertEqual(config.phone, "+1234567890")

    def test_validate_credentials_returns_immediately_in_indexed_mode(self):
        """Indexed triples were proven whole at parse time, so the legacy
        TELEGRAM_* check must not fire even with those variables absent."""
        env = self.base_env | _account_triple(1) | _account_triple(2)
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            config.validate_credentials()  # must not raise

    def test_indexed_mode_logs_count_only(self):
        env = self.base_env | _account_triple(1) | _account_triple(2)
        with patch.dict(os.environ, env, clear=True), self.assertLogs("src.config", level="INFO") as captured:
            Config()
        multi = [m for m in captured.output if "Multi-account" in m]
        self.assertEqual(len(multi), 1)
        self.assertIn("Multi-account: using 2 configured account(s)", multi[0])
        self._assert_no_credential_values("\n".join(captured.output), env)

    def test_zero_config_logs_nothing_new(self):
        """The 7.x-byte-identical claim: no 'Multi-account' line without TG_ACCOUNT_*."""
        env = self.base_env | {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_PHONE": "+1234567890",
        }
        with patch.dict(os.environ, env, clear=True), self.assertLogs("src.config", level="DEBUG") as captured:
            Config()
        self.assertFalse([m for m in captured.output if "Multi-account" in m])

    def test_account_config_repr_hides_credentials_phone_and_label(self):
        """Reprs travel into logs and exception text: the credential fields and
        the label must be excluded, so logging the dataclass is safe."""
        env = self.base_env | _account_triple(1) | {"TG_ACCOUNT_1_LABEL": "very-private-label"}
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        text = repr(config.accounts[0])
        self.assertNotIn("test-hash-account-1", text)
        self.assertNotIn("+34600000001", text)
        self.assertNotIn("very-private-label", text)
        self.assertNotIn("10001", text)
        self.assertIn("index=1", text)


class TestSchedulerConfigValidation(unittest.TestCase):
    """VIEWER_TIMEZONE and STATS_CALCULATION_HOUR are validated at construction.

    Stored verbatim, a misspelled tz name or an out-of-range hour raised inside
    the stats scheduler's hourly catch-all — logged, slept, and retried forever
    while the viewer kept serving first-launch counts.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.env_patcher = patch.dict(
            os.environ, {"BACKUP_PATH": self.temp_dir, "DATABASE_DIR": self.temp_dir}, clear=True
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **env):
        with patch.dict(os.environ, env):
            return Config()

    def test_misspelled_timezone_falls_back_to_utc_with_warning(self):
        with self.assertLogs("src.config", level="WARNING") as captured:
            config = self._config(VIEWER_TIMEZONE="Europe/Madird")
        self.assertEqual(config.viewer_timezone, "UTC")
        self.assertTrue(any("VIEWER_TIMEZONE" in line for line in captured.output))

    def test_valid_timezone_is_kept(self):
        config = self._config(VIEWER_TIMEZONE="Europe/Madrid")
        self.assertEqual(config.viewer_timezone, "Europe/Madrid")

    def test_hour_out_of_range_falls_back_to_default(self):
        with self.assertLogs("src.config", level="WARNING") as captured:
            config = self._config(STATS_CALCULATION_HOUR="24")
        self.assertEqual(config.stats_calculation_hour, 3)
        self.assertTrue(any("STATS_CALCULATION_HOUR" in line for line in captured.output))

    def test_hour_not_a_number_falls_back_to_default(self):
        with self.assertLogs("src.config", level="WARNING"):
            config = self._config(STATS_CALCULATION_HOUR="midnight")
        self.assertEqual(config.stats_calculation_hour, 3)

    def test_hour_bounds_are_accepted(self):
        self.assertEqual(self._config(STATS_CALCULATION_HOUR="0").stats_calculation_hour, 0)
        self.assertEqual(self._config(STATS_CALCULATION_HOUR="23").stats_calculation_hour, 23)


class TestMassOperationGuardrails(unittest.TestCase):
    """Non-positive limiter settings invert the protection instead of degrading
    it: window<=0 prunes each operation before counting (limiter permanently
    dark), threshold<=0 blocks everything after the first. Both fail loudly."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra):
        env = _get_base_env(self.temp_dir) | extra
        with patch.dict(os.environ, env, clear=True):
            return Config()

    def test_valid_values_pass(self):
        config = self._config(MASS_OPERATION_THRESHOLD="1", MASS_OPERATION_WINDOW_SECONDS="1")
        self.assertEqual(config.mass_operation_threshold, 1)
        self.assertEqual(config.mass_operation_window_seconds, 1)

    def test_zero_window_raises(self):
        with self.assertRaisesRegex(ValueError, "MASS_OPERATION_WINDOW_SECONDS"):
            self._config(MASS_OPERATION_WINDOW_SECONDS="0")

    def test_negative_window_raises(self):
        with self.assertRaisesRegex(ValueError, "MASS_OPERATION_WINDOW_SECONDS"):
            self._config(MASS_OPERATION_WINDOW_SECONDS="-30")

    def test_zero_threshold_raises(self):
        with self.assertRaisesRegex(ValueError, "MASS_OPERATION_THRESHOLD"):
            self._config(MASS_OPERATION_THRESHOLD="0")

    def test_negative_threshold_raises(self):
        with self.assertRaisesRegex(ValueError, "MASS_OPERATION_THRESHOLD"):
            self._config(MASS_OPERATION_THRESHOLD="-1")


class TestEventWebhookConfig(unittest.TestCase):
    """EVENT_WEBHOOK_* parsing (#336): warn + force-disable, never abort.

    The webhook is a side-channel, so every sub-option failure logs one
    warning naming the variable (never echoing the value — the URL is a
    capability secret) and disables the webhook; only the master bool follows
    the raise-on-garbage vocabulary (covered by test_config_bool_vocab).
    """

    URL = "https://hooks.example.test/secret-token-path"

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra):
        env = _get_base_env(self.temp_dir) | {"EVENT_WEBHOOK_ENABLED": "true", "EVENT_WEBHOOK_URL": self.URL} | extra
        with patch.dict(os.environ, env, clear=True):
            return Config()

    def test_disabled_by_default_and_inert(self):
        with patch.dict(os.environ, _get_base_env(self.temp_dir), clear=True):
            config = Config()
        self.assertFalse(config.event_webhook_enabled)
        self.assertEqual(config.event_webhook_url, "")

    def test_enabled_defaults(self):
        config = self._config()
        self.assertTrue(config.event_webhook_enabled)
        self.assertEqual(config.event_webhook_method, "POST")
        self.assertEqual(config.event_webhook_events, {"message_edited", "message_deleted"})
        self.assertEqual(config.event_webhook_chat_ids, set())
        self.assertEqual(config.event_webhook_body_template, "")
        # The default template's content type is declared when none is given.
        self.assertEqual(config.event_webhook_headers, {"Content-Type": "application/json; charset=utf-8"})

    def test_missing_url_disables(self):
        env = _get_base_env(self.temp_dir) | {"EVENT_WEBHOOK_ENABLED": "true"}
        with (
            patch.dict(os.environ, env, clear=True),
            self.assertLogs("src.config", level="WARNING") as logs,
        ):
            config = Config()
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_URL" in line for line in logs.output))

    def test_bad_scheme_disables_without_echoing_value(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_URL="ftp://files.example.test/x")
        self.assertFalse(config.event_webhook_enabled)
        joined = "\n".join(logs.output)
        self.assertIn("EVENT_WEBHOOK_URL", joined)
        self.assertNotIn("ftp://files.example.test", joined)

    def test_bare_scheme_without_hostname_disables(self):
        """ "https://" satisfies a prefix check but names no host — the sender
        would fire doomed requests forever. Hostname is required."""
        for url in ("https://", "http://", "https:///path"):
            with self.assertLogs("src.config", level="WARNING") as logs:
                config = self._config(EVENT_WEBHOOK_URL=url)
            self.assertFalse(config.event_webhook_enabled)
            self.assertTrue(any("EVENT_WEBHOOK_URL" in line for line in logs.output))

    def test_bad_method_disables(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_METHOD="PATCH")
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_METHOD" in line for line in logs.output))

    def test_headers_bad_json_disables(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_HEADERS="{not json")
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_HEADERS" in line for line in logs.output))

    def test_headers_non_string_values_disable(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_HEADERS='{"X-Retry": 3}')
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_HEADERS" in line for line in logs.output))

    def test_headers_kept_and_content_type_not_overridden(self):
        config = self._config(
            EVENT_WEBHOOK_HEADERS='{"Authorization": "Bearer test@value/here", "content-type": "text/plain"}'
        )
        self.assertTrue(config.event_webhook_enabled)
        self.assertEqual(
            config.event_webhook_headers,
            {"Authorization": "Bearer test@value/here", "content-type": "text/plain"},
        )

    def test_events_subset_and_normalization(self):
        config = self._config(EVENT_WEBHOOK_EVENTS=" Message_Deleted ")
        self.assertEqual(config.event_webhook_events, {"message_deleted"})

    def test_unknown_event_disables(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_EVENTS="message_deleted,message_pinned")
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_EVENTS" in line for line in logs.output))

    def test_chat_ids_parsed(self):
        config = self._config(EVENT_WEBHOOK_CHAT_IDS="-1001, -1002")
        self.assertEqual(config.event_webhook_chat_ids, {-1001, -1002})

    def test_garbage_chat_ids_disable(self):
        with self.assertLogs("src.config", level="WARNING") as logs:
            config = self._config(EVENT_WEBHOOK_CHAT_IDS="-1001,abc")
        self.assertFalse(config.event_webhook_enabled)
        self.assertTrue(any("EVENT_WEBHOOK_CHAT_IDS" in line for line in logs.output))

    def test_combination_warnings(self):
        """Startup names the exact reason a selected event can never fire."""
        with self.assertLogs("src.config", level="WARNING") as logs:
            self._config()  # ENABLE_LISTENER unset -> false
        self.assertTrue(any("ENABLE_LISTENER=false" in line for line in logs.output))

        with self.assertLogs("src.config", level="WARNING") as logs:
            self._config(ENABLE_LISTENER="true")  # LISTEN_DELETIONS defaults false
        joined = "\n".join(logs.output)
        self.assertIn("message_deleted webhooks will never fire", joined)

        with self.assertLogs("src.config", level="WARNING") as logs:
            self._config(ENABLE_LISTENER="true", LISTEN_DELETIONS="true", LISTEN_EDITS="false")
        joined = "\n".join(logs.output)
        self.assertIn("message_edited webhooks will never fire", joined)

    def test_startup_log_never_contains_url(self):
        with self.assertLogs("src.config", level="INFO") as logs:
            self._config(ENABLE_LISTENER="true", LISTEN_DELETIONS="true")
        joined = "\n".join(logs.output)
        self.assertNotIn(self.URL, joined)
        self.assertNotIn("secret-token-path", joined)
        self.assertIn("EVENT_WEBHOOK enabled", joined)


class TestNamedNumericEnvParsing(unittest.TestCase):
    """Numeric env knobs fail BY NAME (the bool-vocabulary convention extended):
    a bare int() crash read "invalid literal for int() with base 10" and left
    the user guessing which of ~20 variables carried the typo. Blank means
    default (compose's ${VAR:-} injects empty strings for unset variables)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _config(self, **extra):
        env = _get_base_env(self.temp_dir) | extra
        with patch.dict(os.environ, env, clear=True):
            return Config()

    def test_placeholder_api_id_fails_with_an_actionable_named_error(self):
        """.env.example ships your_api_id_here; the crash must name the
        variable and point at my.telegram.org, not read like a Python bug."""
        with self.assertRaises(ValueError) as ctx:
            self._config(TELEGRAM_API_ID="your_api_id_here")
        message = str(ctx.exception)
        self.assertIn("TELEGRAM_API_ID", message)
        self.assertIn("my.telegram.org", message)
        self.assertNotIn("invalid literal", message)

    def test_unicode_digit_api_id_gets_the_same_named_error(self):
        """ "²".isdigit() is True yet int("²") raises — an isdigit() pre-check
        would let it through to the bare crash this fix removes."""
        with self.assertRaises(ValueError) as ctx:
            self._config(TELEGRAM_API_ID="²")
        self.assertIn("TELEGRAM_API_ID", str(ctx.exception))
        self.assertNotIn("invalid literal", str(ctx.exception))

    def test_blank_numeric_value_falls_back_to_default(self):
        config = self._config(MAX_MEDIA_SIZE_MB="", BATCH_SIZE="   ")
        self.assertEqual(config.max_media_size_mb, 100)
        self.assertEqual(config.batch_size, 100)

    def test_garbage_int_names_the_variable(self):
        with self.assertRaisesRegex(ValueError, "BATCH_SIZE"):
            self._config(BATCH_SIZE="lots")

    def test_garbage_float_names_the_variable(self):
        with self.assertRaisesRegex(ValueError, "REACTION_DEBOUNCE_SECONDS"):
            self._config(REACTION_DEBOUNCE_SECONDS="fast")

    def test_nonfinite_float_names_the_variable(self):
        with self.assertRaisesRegex(ValueError, "REACTION_DEBOUNCE_SECONDS"):
            self._config(REACTION_DEBOUNCE_SECONDS="nan")

    def test_valid_values_still_parse(self):
        config = self._config(MAX_MEDIA_SIZE_MB="250", REACTION_DEBOUNCE_SECONDS="2.5")
        self.assertEqual(config.max_media_size_mb, 250)
        self.assertEqual(config.reaction_debounce_seconds, 2.5)

    def test_database_timeout_keeps_the_never_abort_contract(self):
        """#378 made src/db/base.py tolerate garbage here; the backup
        container must not crash where the viewer shrugs."""
        config = self._config(DATABASE_TIMEOUT="forever")
        self.assertEqual(config.database_timeout, 60.0)
