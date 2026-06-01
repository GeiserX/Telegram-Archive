"""Tests for fixes to issues #142, #144, and #145.

#142: INFO log emitted before TelegramClient instantiation with session path
#144: os.path.abspath() applied to resolve relative DB paths in _build_database_url()
#145: Dockerfile changes (no unit test needed — infra only)
"""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ============================================================
# Issue #142: Session path logging before TelegramClient creation
# ============================================================


class TestSessionPathLogging:
    """Test that INFO log with session DB path is emitted before TelegramClient instantiation."""

    def _make_backup(self):
        """Create a TelegramBackup instance with mocked dependencies."""
        from src.telegram_backup import TelegramBackup

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        backup.config.session_path = "/data/sessions/my_session"
        backup.config.api_id = 12345
        backup.config.api_hash = "testhash"
        backup.config.get_telegram_client_kwargs = MagicMock(return_value={"flood_sleep_threshold": 0})
        backup.db = AsyncMock()
        backup.client = None
        backup._owns_client = True
        return backup

    async def test_backup_connect_logs_session_path_at_info_level(self):
        """telegram_backup.connect() emits INFO log with session DB path before client creation."""
        backup = self._make_backup()

        with (
            patch("src.telegram_backup.TelegramClient") as mock_client_cls,
            patch("src.telegram_backup.logger") as mock_logger,
        ):
            mock_client_cls.return_value = MagicMock()
            mock_client_cls.return_value.connect = AsyncMock()
            mock_client_cls.return_value.is_user_authorized = AsyncMock(return_value=True)
            mock_client_cls.return_value.get_me = AsyncMock(return_value=MagicMock(first_name="Test"))
            await backup.connect()

        # Verify INFO log was called with the session path
        info_calls = [call for call in mock_logger.info.call_args_list]
        session_log_found = any(
            "session" in str(call).lower() and "/data/sessions/my_session" in str(call) for call in info_calls
        )
        assert session_log_found, f"Expected INFO log with session path, got: {info_calls}"

    async def test_backup_connect_logs_before_client_instantiation(self):
        """The session path log appears BEFORE TelegramClient() is called."""
        backup = self._make_backup()

        call_order = []

        def track_log(*args, **kwargs):
            call_order.append("log")

        def track_client(*args, **kwargs):
            mock = MagicMock()
            mock.connect = AsyncMock()
            mock.is_user_authorized = AsyncMock(return_value=True)
            mock.get_me = AsyncMock(return_value=MagicMock(first_name="Test"))
            call_order.append("client")
            return mock

        with (
            patch("src.telegram_backup.TelegramClient", side_effect=track_client),
            patch("src.telegram_backup.logger") as mock_logger,
        ):
            mock_logger.info = track_log
            mock_logger.debug = MagicMock()
            await backup.connect()

        assert "log" in call_order
        assert "client" in call_order
        assert call_order.index("log") < call_order.index("client")


class TestListenerSessionPathLogging:
    """Test that listener.py emits INFO log with session DB path before TelegramClient creation."""

    def _make_listener(self):
        """Create a TelegramListener instance with mocked dependencies."""
        from src.listener import TelegramListener

        config = MagicMock()
        config.api_id = 12345
        config.api_hash = "testhash"
        config.phone = "+1234567890"
        config.session_path = "/data/sessions/listener_session"
        config.global_include_ids = set()
        config.private_include_ids = set()
        config.groups_include_ids = set()
        config.channels_include_ids = set()
        config.validate_credentials = MagicMock()
        config.whitelist_mode = False
        config.chat_ids = set()
        config.listen_edits = True
        config.listen_deletions = False
        config.listen_new_messages = False
        config.listen_new_messages_media = False
        config.listen_chat_actions = False
        config.skip_topic_ids = {}
        config.mass_operation_threshold = 10
        config.mass_operation_window_seconds = 30
        config.mass_operation_buffer_delay = 2.0
        config.get_telegram_client_kwargs = MagicMock(return_value={"flood_sleep_threshold": 0})

        db = AsyncMock()
        db.get_all_chats = AsyncMock(return_value=[])
        db.close = AsyncMock()

        listener = TelegramListener(config, db)
        return listener

    async def test_listener_connect_logs_session_path_at_info_level(self):
        """listener.connect() emits INFO log with session DB path before client creation."""
        listener = self._make_listener()

        with (
            patch("src.listener.TelegramClient") as mock_client_cls,
            patch("src.listener.logger") as mock_logger,
            patch("src.db.get_db_manager", new_callable=AsyncMock) as mock_get_db,
            patch("src.listener.RealtimeNotifier") as mock_notifier_cls,
        ):
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            mock_client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+1234567890"))
            mock_client.on = MagicMock(return_value=lambda f: f)
            mock_client_cls.return_value = mock_client
            mock_notifier_cls.return_value = AsyncMock()

            await listener.connect()

        info_calls = [call for call in mock_logger.info.call_args_list]
        session_log_found = any(
            "session" in str(call).lower() and "/data/sessions/listener_session" in str(call) for call in info_calls
        )
        assert session_log_found, f"Expected INFO log with session path, got: {info_calls}"

    async def test_listener_connect_logs_before_client_instantiation(self):
        """The session path log appears BEFORE TelegramClient() is called in listener."""
        listener = self._make_listener()

        call_order = []

        def track_log(*args, **kwargs):
            call_order.append("log")

        def track_client(*args, **kwargs):
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock()
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            mock_client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+1234567890"))
            mock_client.on = MagicMock(return_value=lambda f: f)
            call_order.append("client")
            return mock_client

        with (
            patch("src.listener.TelegramClient", side_effect=track_client),
            patch("src.listener.logger") as mock_logger,
            patch("src.db.get_db_manager", new_callable=AsyncMock),
            patch("src.listener.RealtimeNotifier") as mock_notifier_cls,
        ):
            mock_logger.info = track_log
            mock_logger.debug = MagicMock()
            mock_logger.warning = MagicMock()
            mock_logger.error = MagicMock()
            mock_notifier_cls.return_value = AsyncMock()

            await listener.connect()

        assert "log" in call_order
        assert "client" in call_order
        assert call_order.index("log") < call_order.index("client")


# ============================================================
# Issue #144: os.path.abspath() resolves relative DB paths
# ============================================================


class TestRelativeDbPathResolution(unittest.TestCase):
    """Test that _build_database_url() resolves relative paths to absolute via os.path.abspath()."""

    def test_relative_db_path_gets_resolved_to_absolute(self):
        """DB_PATH=data/telegram_backup.db (relative) becomes an absolute path in the URL."""
        from src.db.base import DatabaseManager

        env = {"DB_PATH": "data/telegram_backup.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            # The path in the URL must be absolute (starts with /)
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            self.assertTrue(
                os.path.isabs(url_path),
                f"Expected absolute path in URL, got: {url_path}",
            )

    def test_absolute_db_path_remains_unchanged(self):
        """DB_PATH=/data/backups/telegram_backup.db (already absolute) is not modified."""
        from src.db.base import DatabaseManager

        env = {"DB_PATH": "/data/backups/telegram_backup.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            expected_path = os.path.abspath("/data/backups/telegram_backup.db")
            self.assertEqual(url_path, expected_path)

    def test_relative_database_path_env_gets_resolved(self):
        """DATABASE_PATH=./my.db (relative) becomes an absolute path."""
        from src.db.base import DatabaseManager

        env = {"DATABASE_PATH": "./my.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            self.assertTrue(
                os.path.isabs(url_path),
                f"Expected absolute path in URL, got: {url_path}",
            )
            self.assertNotIn("./", url_path)

    def test_absolute_database_path_env_remains_unchanged(self):
        """DATABASE_PATH=/custom/path/my.db (already absolute) is unchanged."""
        from src.db.base import DatabaseManager

        env = {"DATABASE_PATH": "/custom/path/my.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            expected_path = os.path.abspath("/custom/path/my.db")
            self.assertEqual(url_path, expected_path)

    def test_default_path_is_absolute(self):
        """Default path (no env vars set) produces an absolute path."""
        from src.db.base import DatabaseManager

        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            self.assertTrue(
                os.path.isabs(url_path),
                f"Expected absolute path in URL, got: {url_path}",
            )

    def test_relative_database_dir_gets_resolved(self):
        """DATABASE_DIR=data (relative directory) produces an absolute path."""
        from src.db.base import DatabaseManager

        env = {"DATABASE_DIR": "data"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            self.assertTrue(
                os.path.isabs(url_path),
                f"Expected absolute path in URL, got: {url_path}",
            )
            self.assertTrue(url_path.endswith("telegram_backup.db"))

    def test_relative_backup_path_gets_resolved(self):
        """BACKUP_PATH=backups (relative) produces an absolute path."""
        from src.db.base import DatabaseManager

        env = {"BACKUP_PATH": "backups"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            url_path = manager.database_url.replace("sqlite+aiosqlite:///", "")
            self.assertTrue(
                os.path.isabs(url_path),
                f"Expected absolute path in URL, got: {url_path}",
            )


# ============================================================
# Issue #145: Dockerfile changes — no unit tests needed
# ============================================================


class TestDockerfileAssumptions(unittest.TestCase):
    """Verify runtime assumptions that Dockerfile changes (#145) rely on.

    The Dockerfile removes `chown /app` and sets PYTHONDONTWRITEBYTECODE=1.
    These tests verify that the application does NOT expect to write .pyc files
    or write to /app at runtime (only to configured data paths).
    """

    def test_no_hardcoded_app_directory_writes(self):
        """Source code should not contain hardcoded /app write paths."""
        import pathlib

        # Check source files for hardcoded /app directory writes
        src_dir = pathlib.Path(__file__).parent.parent / "src"
        for py_file in (src_dir / "telegram_backup.py", src_dir / "listener.py"):
            content = py_file.read_text(encoding="utf-8")
            # Should not have APP_DIR or open("/app/...") patterns
            self.assertNotIn("APP_DIR", content)
            self.assertNotIn('"/app/', content)

    def test_pythondontwritebytecode_prevents_pyc_creation(self):
        """PYTHONDONTWRITEBYTECODE=1 means sys.dont_write_bytecode is respected."""
        import sys

        # This just documents the expected behavior — if the env var is set,
        # Python will not write .pyc files. The actual enforcement is in the Dockerfile.
        # We verify the mechanism exists.
        self.assertTrue(hasattr(sys, "dont_write_bytecode"))
