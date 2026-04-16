"""Flood-wait visibility (yrru-mix3).

Goal: make Telethon flood-waits visible in the scheduler log so a long silent
pause during backfill can be diagnosed instead of mistaken for a hang.

Two things under test:
1. Config exposes ``flood_sleep_threshold=0`` in the shared client kwargs so
   Telethon always raises ``FloodWaitError`` instead of sleeping silently.
2. A thin retry wrapper around ``client.iter_messages`` catches the error,
   logs the wait, and resumes iteration from the last yielded message id.
"""

import importlib
import logging
import os
import sys
import tempfile
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    fake_db_module = types.ModuleType("src.db")
    fake_db_module.DatabaseAdapter = object
    fake_db_module.create_adapter = AsyncMock()
    fake_db_module.get_db_manager = AsyncMock()
    monkeypatch.setitem(sys.modules, "src.db", fake_db_module)

    import src.connection
    import src.telegram_backup

    importlib.reload(src.connection)
    importlib.reload(src.telegram_backup)

    yield

    if "src.db" in sys.modules:
        importlib.reload(src.connection)
        importlib.reload(src.telegram_backup)


def test_config_kwargs_include_flood_sleep_threshold_zero():
    from src.config import Config

    env = {
        "CHAT_TYPES": "private",
        "BACKUP_PATH": tempfile.mkdtemp(),
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "x",
        "TELEGRAM_PHONE": "+1",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config()

    kwargs = config.get_telegram_client_kwargs()
    assert kwargs.get("flood_sleep_threshold") == 0


@pytest.mark.asyncio
async def test_connection_passes_flood_sleep_threshold_to_client():
    from src.connection import TelegramConnection

    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.session_path = "/tmp/test-session"
    config.api_id = 12345
    config.api_hash = "hash"
    config.get_telegram_client_kwargs.return_value = {"flood_sleep_threshold": 0}

    client = AsyncMock()
    client.session = SimpleNamespace(_conn=None)
    client.is_user_authorized.return_value = True
    client.get_me.return_value = SimpleNamespace(first_name="Test", phone="123")

    with (
        patch("src.connection.TelegramClient", return_value=client) as client_cls,
        patch.object(TelegramConnection, "_session_has_auth", return_value=False),
        patch("src.connection.shutil.copy2"),
    ):
        connection = TelegramConnection(config)
        await connection.connect()

    _, kwargs = client_cls.call_args
    assert kwargs.get("flood_sleep_threshold") == 0


@pytest.mark.asyncio
async def test_iter_with_flood_retry_logs_and_resumes(caplog):
    from src import telegram_backup

    calls = {"n": 0}

    async def fake_iter(entity, min_id=0, reverse=True, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            assert min_id == 0
            raise FloodWaitError(request=None, capture=7)
        # Second call: resume from last yielded id (1) then yield 2, 3
        assert min_id == 1
        for i in (2, 3):
            yield SimpleNamespace(id=i)

    fake_client = SimpleNamespace(iter_messages=fake_iter)

    collected: list[int] = []

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        # Simulate: first fetch yields id=1, then FloodWait, then retry yields 2,3.
        # We need an additional pre-yielded message to seed last-id tracking.
        async def seeded_iter(entity, min_id=0, reverse=True, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                yield SimpleNamespace(id=1)
                raise FloodWaitError(request=None, capture=7)
            assert min_id == 1
            for i in (2, 3):
                yield SimpleNamespace(id=i)

        fake_client.iter_messages = seeded_iter
        calls["n"] = 0
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [1, 2, 3]
    assert calls["n"] == 2
    assert any(
        "FloodWait" in r.getMessage() and "7" in r.getMessage()
        for r in caplog.records
    )
