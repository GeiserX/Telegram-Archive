import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import auth_noninteractive, restore_chat
from src.setup_auth import setup_authentication

fake_db_module = types.ModuleType("src.db")
fake_db_module.DatabaseAdapter = object
fake_db_module.create_adapter = AsyncMock()
fake_db_module.get_db_manager = AsyncMock()
sys.modules.setdefault("src.db", fake_db_module)

from src.connection import TelegramConnection
from src.listener import TelegramListener
from src.telegram_backup import TelegramBackup


@pytest.mark.asyncio
async def test_connection_passes_proxy_kwargs():
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.get_telegram_client_kwargs.return_value = {"proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}}

    client = AsyncMock()
    client.session = SimpleNamespace(_conn=None)
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", phone="123")

    with patch("src.connection.TelegramClient", return_value=client) as client_cls:
        with patch.object(TelegramConnection, "_session_has_auth", return_value=False):
            with patch("src.connection.shutil.copy2"):
                connection = TelegramConnection(config)
                await connection.connect()

    client_cls.assert_called_once_with(
        "/tmp/test-session",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )


@pytest.mark.asyncio
async def test_connection_omits_proxy_when_not_configured():
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.get_telegram_client_kwargs.return_value = {}

    client = AsyncMock()
    client.session = SimpleNamespace(_conn=None)
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", phone="123")

    with patch("src.connection.TelegramClient", return_value=client) as client_cls:
        with patch.object(TelegramConnection, "_session_has_auth", return_value=False):
            with patch("src.connection.shutil.copy2"):
                connection = TelegramConnection(config)
                await connection.connect()

    client_cls.assert_called_once_with("/tmp/test-session", 12345, "hash")


@pytest.mark.asyncio
async def test_backup_connect_passes_proxy_kwargs():
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.phone = "+123456789"
    config.get_telegram_client_kwargs.return_value = {"proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}}

    db = AsyncMock()
    backup = TelegramBackup(config, db)

    client = AsyncMock()
    client.session = SimpleNamespace(_conn=None)
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", phone="123")

    with patch("src.telegram_backup.TelegramClient", return_value=client) as client_cls:
        await backup.connect()

    client_cls.assert_called_once_with(
        "/tmp/test-session",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )


@pytest.mark.asyncio
async def test_listener_connect_passes_proxy_kwargs():
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.phone = "+123456789"
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.whitelist_mode = False
    config.chat_ids = set()
    config.listen_edits = True
    config.listen_deletions = False
    config.listen_new_messages = True
    config.listen_new_messages_media = False
    config.listen_chat_actions = True
    config.mass_operation_threshold = 10
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    config.get_telegram_client_kwargs.return_value = {
        "proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}
    }

    db = AsyncMock()
    db.get_all_chats.return_value = []
    listener = TelegramListener(config, db)

    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", phone="123")

    notifier = AsyncMock()
    with patch("src.listener.TelegramClient", return_value=client) as client_cls:
        with patch("src.db.get_db_manager", AsyncMock(return_value=object())):
            with patch("src.listener.RealtimeNotifier", return_value=notifier):
                with patch.object(TelegramListener, "_register_handlers"):
                    await listener.connect()

    client_cls.assert_called_once_with(
        "/tmp/test-session",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )


@pytest.mark.asyncio
async def test_setup_authentication_passes_proxy_kwargs():
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.phone = "+123456789"
    config.get_telegram_client_kwargs.return_value = {"proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}}

    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", last_name=None, username="tester", phone="123")

    with patch("src.config.Config", return_value=config):
        with patch("src.config.setup_logging"):
            with patch("src.setup_auth.TelegramClient", return_value=client) as client_cls:
                result = await setup_authentication()

    assert result is True
    client_cls.assert_called_once_with(
        "/tmp/test-session",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )


@pytest.mark.asyncio
async def test_auth_noninteractive_passes_proxy_kwargs():
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", username="tester")

    env = {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash",
        "TELEGRAM_PHONE": "+123456789",
        "BACKUP_PATH": "/tmp/backups",
    }

    with patch.dict(os.environ, env, clear=True):
        with patch.object(auth_noninteractive.sys, "argv", ["auth_noninteractive.py", "send"]):
            with patch("scripts.auth_noninteractive.build_telegram_client_kwargs", return_value={"proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}}):
                with patch("scripts.auth_noninteractive.TelegramClient", return_value=client) as client_cls:
                    await auth_noninteractive.main()

    client_cls.assert_called_once_with(
        "/tmp/session/telegram_backup",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )


@pytest.mark.asyncio
async def test_restore_chat_client_passes_proxy_kwargs():
    client = AsyncMock()
    client.is_user_authorized.return_value = True

    env = {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash",
        "SESSION_PATH": "/tmp/custom-session",
    }

    with patch.dict(os.environ, env, clear=True):
        with patch("scripts.restore_chat.build_telegram_client_kwargs", return_value={"proxy": {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}}):
            with patch("scripts.restore_chat.TelegramClient", return_value=client) as client_cls:
                result = await restore_chat.get_telegram_client()

    assert result is client
    client_cls.assert_called_once_with(
        "/tmp/custom-session",
        12345,
        "hash",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080},
    )
