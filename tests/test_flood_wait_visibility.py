"""Flood-wait visibility (upstream PR #124).

Goal: make Telethon flood-waits visible in the scheduler log so a long silent
pause during backfill can be diagnosed instead of mistaken for a hang.

Three things under test:
1. Config exposes ``flood_sleep_threshold=0`` in the shared client kwargs so
   Telethon always raises ``FloodWaitError`` instead of sleeping silently.
2. A thin retry wrapper around ``client.iter_messages`` catches the error,
   logs the wait (above ``FLOOD_WAIT_LOG_THRESHOLD``), resumes iteration from
   the last yielded message id, and gives up after ``MAX_FLOOD_RETRIES``
   consecutive flood-waits without progress.
3. ``e.seconds`` is clamped at ``MAX_FLOOD_WAIT_SECONDS`` so a pathologically
   large value cannot hang the process for hours.
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


def _patch_db_module(monkeypatch):
    """Stub ``src.db`` so we can import telegram_backup without a real adapter.

    Reloads ``src.connection`` and ``src.telegram_backup`` against the stub so
    they pick up the fake module. Tests that don't import telegram_backup
    don't need this — see ``test_config_kwargs_include_flood_sleep_threshold_zero``.
    """
    fake_db_module = types.ModuleType("src.db")
    fake_db_module.DatabaseAdapter = object
    fake_db_module.create_adapter = AsyncMock()
    fake_db_module.get_db_manager = AsyncMock()
    monkeypatch.setitem(sys.modules, "src.db", fake_db_module)

    import src.connection
    import src.telegram_backup

    importlib.reload(src.connection)
    importlib.reload(src.telegram_backup)


@pytest.fixture
def fake_db(monkeypatch):
    """Opt-in fixture for tests that import src.telegram_backup."""
    _patch_db_module(monkeypatch)
    yield
    if "src.db" in sys.modules:
        import src.connection
        import src.telegram_backup

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
async def test_connection_passes_flood_sleep_threshold_to_client(fake_db):
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
async def test_iter_with_flood_retry_logs_and_resumes_after_partial_progress(caplog, fake_db):
    """First call yields id=1 then raises; second call resumes at min_id=1."""
    from src import telegram_backup

    calls = {"n": 0}

    async def seeded_iter(entity, min_id=0, reverse=True, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            yield SimpleNamespace(id=1)
            raise FloodWaitError(request=None, capture=15)
        assert min_id == 1
        for i in (2, 3):
            yield SimpleNamespace(id=i)

    fake_client = SimpleNamespace(iter_messages=seeded_iter)
    collected: list[int] = []

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [1, 2, 3]
    assert calls["n"] == 2
    assert any("FloodWait" in r.getMessage() and "15" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_iter_with_flood_retry_handles_flood_before_any_yield(caplog, fake_db):
    """FloodWait on the very first call (no progress) — resume_from must stay
    at the original min_id and iteration must continue once the wait clears."""
    from src import telegram_backup

    calls = {"n": 0}

    async def first_call_floods(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            assert min_id == 100
            raise FloodWaitError(request=None, capture=15)
            yield  # unreachable; satisfies async-generator contract
        assert min_id == 100  # still the original min_id, not advanced
        for i in (101, 102):
            yield SimpleNamespace(id=i)

    fake_client = SimpleNamespace(iter_messages=first_call_floods)
    collected: list[int] = []

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=100, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [101, 102]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_iter_with_flood_retry_survives_consecutive_floods(caplog, fake_db):
    """Three consecutive FloodWaitErrors before success — common in production."""
    from src import telegram_backup

    calls = {"n": 0}

    async def thrice_floods(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise FloodWaitError(request=None, capture=15)
            yield  # unreachable
        for i in (1, 2):
            yield SimpleNamespace(id=i)

    fake_client = SimpleNamespace(iter_messages=thrice_floods)
    collected: list[int] = []

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [1, 2]
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_iter_with_flood_retry_resets_counter_on_progress(caplog, fake_db):
    """Each successful yield must reset the retry counter so a long backfill
    that hits one flood-wait per chunk doesn't trip the cap."""
    from src import telegram_backup

    calls = {"n": 0}

    async def alternating(entity, min_id=0, **_):
        calls["n"] += 1
        # Yield one message, then flood. Repeat enough times that without a
        # counter reset, MAX_FLOOD_RETRIES (5) would be exceeded.
        if calls["n"] <= 7:
            yield SimpleNamespace(id=calls["n"])
            raise FloodWaitError(request=None, capture=15)
        # Final call: drain to completion
        yield SimpleNamespace(id=99)

    fake_client = SimpleNamespace(iter_messages=alternating)
    collected: list[int] = []

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [1, 2, 3, 4, 5, 6, 7, 99]


@pytest.mark.asyncio
async def test_iter_with_flood_retry_gives_up_after_max_retries(caplog, fake_db):
    """Flood-wait without progress past MAX_FLOOD_RETRIES must raise."""
    from src import telegram_backup

    calls = {"n": 0}

    async def always_floods(entity, min_id=0, **_):
        calls["n"] += 1
        raise FloodWaitError(request=None, capture=15)
        yield  # unreachable

    fake_client = SimpleNamespace(iter_messages=always_floods)

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.ERROR, logger="src.telegram_backup"),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
        pytest.raises(FloodWaitError),
    ):
        async for _msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            pass

    assert calls["n"] == telegram_backup.MAX_FLOOD_RETRIES + 1
    assert any("exceeded" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_iter_with_flood_retry_caps_sleep_seconds(fake_db):
    """An e.seconds value above MAX_FLOOD_WAIT_SECONDS must be clamped."""
    from src import telegram_backup

    calls = {"n": 0}

    async def one_huge_flood(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=86400)  # 1 day
            yield
        yield SimpleNamespace(id=1)

    fake_client = SimpleNamespace(iter_messages=one_huge_flood)
    sleeps: list[float] = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    with patch.object(telegram_backup.asyncio, "sleep", record_sleep):
        async for _msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            pass

    assert sleeps == [telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1]


@pytest.mark.asyncio
async def test_iter_with_flood_retry_preserves_max_id_kwarg(fake_db):
    """Gap-fill call sites pass ``max_id`` via **kwargs; it must be forwarded
    on the post-flood retry too, otherwise the gap fetch turns into a full scan."""
    from src import telegram_backup

    seen_kwargs: list[dict] = []
    calls = {"n": 0}

    async def capture_kwargs(entity, min_id=0, **kwargs):
        calls["n"] += 1
        seen_kwargs.append({"min_id": min_id, **kwargs})
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=15)
            yield
        yield SimpleNamespace(id=42)

    fake_client = SimpleNamespace(iter_messages=capture_kwargs)

    async def fast_sleep(_):
        return None

    with patch.object(telegram_backup.asyncio, "sleep", fast_sleep):
        async for _msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=10, max_id=100, reverse=True
        ):
            pass

    assert len(seen_kwargs) == 2
    for kw in seen_kwargs:
        assert kw["max_id"] == 100
        assert kw["reverse"] is True


@pytest.mark.asyncio
async def test_iter_with_flood_retry_suppresses_short_wait_logs(caplog, fake_db):
    """FLOOD_WAIT_LOG_THRESHOLD must silence routine short waits."""
    from src import telegram_backup

    calls = {"n": 0}

    async def short_then_done(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=3)
            yield
        yield SimpleNamespace(id=1)

    fake_client = SimpleNamespace(iter_messages=short_then_done)

    async def fast_sleep(_):
        return None

    with (
        caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
        patch.dict(os.environ, {"FLOOD_WAIT_LOG_THRESHOLD": "10"}),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        async for _msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            pass

    flood_logs = [r for r in caplog.records if "FloodWait" in r.getMessage()]
    assert flood_logs == [], f"Short wait should be silent, got {[r.getMessage() for r in flood_logs]}"


@pytest.mark.asyncio
async def test_iter_with_flood_retry_rejects_non_reverse(fake_db):
    """Wrapper must reject calls without reverse=True to prevent silent data corruption."""
    from src import telegram_backup

    with pytest.raises(ValueError, match="reverse=True"):
        async for _ in telegram_backup.iter_messages_with_flood_retry(
            None, "chat", min_id=0
        ):
            pass


@pytest.mark.asyncio
async def test_iter_with_flood_retry_clamps_negative_sleep(fake_db):
    """Negative e.seconds must be clamped to 0 — never pass a negative to asyncio.sleep."""
    from src import telegram_backup

    calls = {"n": 0}

    async def negative_flood(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=-5)
            yield
        yield SimpleNamespace(id=1)

    fake_client = SimpleNamespace(iter_messages=negative_flood)
    sleeps: list[float] = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    with patch.object(telegram_backup.asyncio, "sleep", record_sleep):
        async for _msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            pass

    assert sleeps == [1], f"Expected sleep(0+1=1) for negative e.seconds, got {sleeps}"


@pytest.mark.asyncio
async def test_iter_with_flood_retry_tolerates_bad_log_threshold_env(fake_db):
    """Invalid FLOOD_WAIT_LOG_THRESHOLD must fall back to default 10, not crash."""
    from src import telegram_backup

    calls = {"n": 0}

    async def one_flood(entity, min_id=0, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=15)
            yield
        yield SimpleNamespace(id=1)

    fake_client = SimpleNamespace(iter_messages=one_flood)

    async def fast_sleep(_):
        return None

    with (
        patch.dict(os.environ, {"FLOOD_WAIT_LOG_THRESHOLD": "not_a_number"}),
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
    ):
        collected = []
        async for msg in telegram_backup.iter_messages_with_flood_retry(
            fake_client, "chat", min_id=0, reverse=True
        ):
            collected.append(msg.id)

    assert collected == [1]


@pytest.mark.asyncio
async def test_call_with_flood_retry_retries_and_succeeds(fake_db):
    """call_with_flood_retry must retry on FloodWaitError then return the result."""
    from src import telegram_backup

    calls = {"n": 0}

    async def flaky_get_me():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise FloodWaitError(request=None, capture=5)
        return SimpleNamespace(first_name="Test", phone="123")

    async def fast_sleep(_):
        return None

    with patch.object(telegram_backup.asyncio, "sleep", fast_sleep):
        result = await telegram_backup.call_with_flood_retry(flaky_get_me)

    assert result.first_name == "Test"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_call_with_flood_retry_gives_up(fake_db):
    """call_with_flood_retry must raise after exceeding max retries."""
    from src import telegram_backup

    async def always_floods():
        raise FloodWaitError(request=None, capture=5)

    async def fast_sleep(_):
        return None

    with (
        patch.object(telegram_backup.asyncio, "sleep", fast_sleep),
        pytest.raises(FloodWaitError),
    ):
        await telegram_backup.call_with_flood_retry(always_floods)


@pytest.mark.asyncio
async def test_call_with_flood_retry_clamps_negative_sleep(fake_db):
    """Negative e.seconds in call_with_flood_retry must be clamped to 0."""
    from src import telegram_backup

    calls = {"n": 0}
    sleeps: list[float] = []

    async def negative_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FloodWaitError(request=None, capture=-10)
        return "ok"

    async def record_sleep(seconds):
        sleeps.append(seconds)

    with patch.object(telegram_backup.asyncio, "sleep", record_sleep):
        result = await telegram_backup.call_with_flood_retry(negative_then_ok)

    assert result == "ok"
    assert sleeps == [1], f"Expected sleep(0+1=1) for negative e.seconds, got {sleeps}"
