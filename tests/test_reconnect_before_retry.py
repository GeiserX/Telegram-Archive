"""Reconnect before spending a retry on a disconnected client — #265.

Reported symptom: a few minutes of network loss produced

    Transient Error (ConnectionError): sleeping 3.19s before retrying
    _fetch_media_bytes_bounded (retry=1/5): Cannot send requests while disconnected

five times in a row, then "giving up" — "with no sign of any reconnection
attempt". Waiting cannot fix "cannot send requests while disconnected", so the
whole retry budget was spent on a client that could not possibly succeed, and
the log gave an operator no way to tell that from a stuck loop.

These tests pin both halves: the client is re-established before the next
attempt, and the attempt is visible in the log.
"""

import logging
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import TelegramClient
from telethon.errors import ChatIdInvalidError
from telethon.sessions import MemorySession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import telegram_backup


@pytest.fixture
def no_sleep(monkeypatch):
    """Make backoff instant and record the requested durations."""
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(telegram_backup.asyncio, "sleep", fake_sleep)
    return sleeps


def _disconnected_client(events: list[str] | None = None):
    """A Telethon-shaped client that is down until connect() is awaited."""
    client = MagicMock()
    state = {"connected": False}

    def is_connected():
        return state["connected"]

    async def connect():
        state["connected"] = True
        if events is not None:
            events.append("connect")

    client.is_connected = MagicMock(side_effect=is_connected)
    client.connect = AsyncMock(side_effect=connect)
    return client, state


class TestReconnectHappens:
    async def test_disconnected_client_is_reconnected_instead_of_retried_blindly(self, no_sleep):
        events: list[str] = []
        client, state = _disconnected_client(events)

        class Downloader:
            def __init__(self):
                self.client = client
                self.attempts = 0

            async def _fetch_media_bytes_bounded(self):
                self.attempts += 1
                events.append("attempt")
                if not state["connected"]:
                    raise ConnectionError("Cannot send requests while disconnected")
                return "downloaded"

        downloader = Downloader()
        result = await telegram_backup.call_with_flood_retry(downloader._fetch_media_bytes_bounded, max_retries=5)

        assert result == "downloaded"
        # One failed attempt, ONE reconnect, then success — not five blind retries.
        assert events == ["attempt", "connect", "attempt"]
        assert downloader.attempts == 2
        assert client.connect.await_count == 1

    async def test_reconnect_precedes_the_next_attempt(self, no_sleep):
        """Ordering is the whole point: reconnect must come before the retry."""
        events: list[str] = []
        client, state = _disconnected_client(events)

        class Owner:
            def __init__(self):
                self.client = client

            async def call(self):
                events.append("attempt")
                raise ConnectionError("Cannot send requests while disconnected")

        owner = Owner()
        with pytest.raises(ConnectionError):
            await telegram_backup.call_with_flood_retry(owner.call, max_retries=2)

        assert events == ["attempt", "connect", "attempt", "attempt"]

    async def test_client_bound_methods_resolve_the_client_itself(self, no_sleep):
        """``call_with_flood_retry(self.client.download_media, ...)`` — the
        retried callable IS a client method, so the client is the owner."""
        client, state = _disconnected_client()
        calls = {"n": 0}

        async def download_media():
            calls["n"] += 1
            if not state["connected"]:
                raise ConnectionError("Cannot send requests while disconnected")
            return "file"

        download_media.__self__ = client

        result = await telegram_backup.call_with_flood_retry(download_media, max_retries=3)
        assert result == "file"
        assert client.connect.await_count == 1

    async def test_explicit_client_argument_wins(self, no_sleep):
        client, state = _disconnected_client()

        async def plain_call():
            if not state["connected"]:
                raise ConnectionError("Cannot send requests while disconnected")
            return "ok"

        result = await telegram_backup.call_with_flood_retry(plain_call, max_retries=3, client=client)
        assert result == "ok"
        assert client.connect.await_count == 1


class TestReconnectIsObservable:
    async def test_reconnect_attempt_and_success_are_logged(self, no_sleep, caplog):
        client, state = _disconnected_client()

        async def plain_call():
            if not state["connected"]:
                raise ConnectionError("Cannot send requests while disconnected")
            return "ok"

        with caplog.at_level(logging.INFO, logger="src.telegram_backup"):
            await telegram_backup.call_with_flood_retry(plain_call, max_retries=3, client=client)

        messages = [record.getMessage() for record in caplog.records]
        assert any("reconnecting before retrying" in m for m in messages), messages
        assert any("Reconnected to Telegram" in m for m in messages), messages

    async def test_failed_reconnect_is_logged_and_never_escapes(self, no_sleep, caplog):
        """A still-down network must degrade to the old behaviour, loudly."""
        client = MagicMock()
        client.is_connected = MagicMock(return_value=False)
        client.connect = AsyncMock(side_effect=OSError("Network is unreachable"))

        async def plain_call():
            raise ConnectionError("Cannot send requests while disconnected")

        with (
            caplog.at_level(logging.WARNING, logger="src.telegram_backup"),
            pytest.raises(ConnectionError, match="Cannot send requests while disconnected"),
        ):
            await telegram_backup.call_with_flood_retry(plain_call, max_retries=2, client=client)

        messages = [record.getMessage() for record in caplog.records]
        assert any("Reconnect before retrying" in m and "failed" in m for m in messages), messages
        assert client.connect.await_count == 2

    async def test_log_lines_carry_no_identifiers(self, no_sleep, caplog):
        """PII rule: reconnect logging names the call, never a chat/user/message."""
        client, state = _disconnected_client()

        async def plain_call():
            if not state["connected"]:
                raise ConnectionError("Cannot send requests while disconnected")
            return "ok"

        with caplog.at_level(logging.INFO, logger="src.telegram_backup"):
            await telegram_backup.call_with_flood_retry(plain_call, max_retries=3, client=client)

        reconnect_messages = [
            record.getMessage() for record in caplog.records if "reconnect" in record.getMessage().lower()
        ]
        assert reconnect_messages
        for message in reconnect_messages:
            assert not any(char.isdigit() for char in message), message


class TestReconnectStaysOutOfTheWay:
    async def test_connected_client_is_never_reconnected(self, no_sleep):
        """Telethon's own reconnect may win the race; then this is a no-op."""
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.connect = AsyncMock()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = await telegram_backup.call_with_flood_retry(flaky, max_retries=5, client=client)
        assert result == "ok"
        assert client.connect.await_count == 0

    async def test_call_without_a_discoverable_client_behaves_as_before(self, no_sleep):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = await telegram_backup.call_with_flood_retry(flaky, max_retries=5)
        assert result == "ok"
        assert calls["n"] == 3
        assert len(no_sleep) == 2

    async def test_backoff_schedule_is_unchanged(self, no_sleep, monkeypatch):
        """The fix adds a reconnect; it must not alter the retry budget or waits."""
        monkeypatch.setattr(telegram_backup, "BACKOFF_MIN_SECONDS", 2.0)
        monkeypatch.setattr(telegram_backup, "BACKOFF_MAX_SECONDS", 300.0)
        monkeypatch.setattr(telegram_backup.random, "uniform", lambda a, b: 1.0)

        client, _ = _disconnected_client()
        client.connect = AsyncMock(side_effect=OSError("still down"))

        async def plain_call():
            raise ConnectionError("Cannot send requests while disconnected")

        with pytest.raises(ConnectionError):
            await telegram_backup.call_with_flood_retry(plain_call, max_retries=3, client=client)

        assert no_sleep == [3.0, 5.0, 9.0]


class TestRetryClientResolution:
    def test_prefers_explicit_client(self):
        explicit = object()
        assert telegram_backup._retry_client(lambda: None, explicit) is explicit

    def test_returns_none_for_a_plain_function(self):
        assert telegram_backup._retry_client(lambda: None, None) is None

    def test_returns_none_when_owner_has_no_client(self):
        class Owner:
            def method(self):
                return None

        assert telegram_backup._retry_client(Owner().method, None) is None

    async def test_reconnect_is_a_noop_without_a_client(self):
        assert await telegram_backup._reconnect_before_retry(None, "call") is False

    async def test_reconnect_is_a_noop_for_a_client_without_the_api(self):
        assert await telegram_backup._reconnect_before_retry(object(), "call") is False

    async def test_awaitable_is_connected_is_supported(self):
        client = MagicMock()
        client.is_connected = AsyncMock(return_value=True)
        client.connect = AsyncMock()
        assert await telegram_backup._reconnect_before_retry(client, "call") is False
        assert client.connect.await_count == 0


# ---------------------------------------------------------------------------
# Real-client tests.
#
# A MagicMock whose is_connected() returns False proves nothing here: the whole
# defect is a REAL Telethon client that reports itself disconnected while the
# app still believes it is connected. Everything below runs against a real
# TelegramClient (memory session, never connected to the network).
# ---------------------------------------------------------------------------


def _dead_client() -> TelegramClient:
    """A real client in the exact state Telethon leaves after it gives up.

    ``MTProtoSender._reconnect`` retries 5 times (~1s apart, each bounded by a
    10s connect timeout) and then calls ``_disconnect``, which sets
    ``_user_connected = False`` and ``_connection = None``. From that point
    ``send()`` raises ``ConnectionError('Cannot send requests while
    disconnected')`` unconditionally and ``_start_reconnect`` is guarded by
    ``if self._user_connected`` — so Telethon never retries by itself again
    until someone calls ``connect()``. An outage longer than ~55s lands here,
    which is why the report showed no reconnection attempt at all.
    """
    client = TelegramClient(MemorySession(), 1, "test-api-hash")
    client._sender._user_connected = False
    client._sender._connection = None
    return client


class TestRealDeadSenderState:
    async def test_the_failure_mode_is_what_we_think_it_is(self):
        """Pin the premise with real library code, not a mock."""
        client = _dead_client()
        assert not client.is_connected()
        with pytest.raises(ConnectionError, match="Cannot send requests while disconnected"):
            await client.get_me()

    async def test_real_dead_client_is_reconnected_before_the_next_attempt(self, no_sleep):
        """End-to-end on a real client: the first attempt raises the real
        ConnectionError from the real sender, and the retry is preceded by a
        real ``connect()``."""
        client = _dead_client()
        events: list[str] = []

        async def fake_connect():
            # What a successful connect() achieves: the sender is usable again.
            events.append("connect")
            client._sender._user_connected = True

        client.connect = fake_connect

        async def probe(self):
            events.append("attempt")
            if not self.is_connected():
                await self.get_me()  # real Telethon raises from the dead sender
            return "ok"

        # A genuine bound method of the real client — the same resolution path
        # as ``call_with_flood_retry(self.client.download_media, ...)``.
        bound = types.MethodType(probe, client)
        assert bound.__self__ is client

        result = await telegram_backup.call_with_flood_retry(bound, max_retries=5)

        assert result == "ok"
        assert events == ["attempt", "connect", "attempt"]
        assert client.is_connected()

    async def test_connect_is_attempted_on_every_retry_while_the_network_stays_down(self, no_sleep):
        client = _dead_client()
        attempts = {"connect": 0}

        async def fake_connect():
            attempts["connect"] += 1
            raise OSError("Network is unreachable")

        client.connect = fake_connect

        with pytest.raises(ConnectionError, match="Cannot send requests while disconnected"):
            await telegram_backup.call_with_flood_retry(client.get_me, max_retries=3)

        assert attempts["connect"] == 3


class TestEveryCallShapeResolvesTheClient:
    """One assertion per call shape this repo actually uses, against a real client."""

    def test_bound_method_of_the_client(self):
        """``call_with_flood_retry(self.client.download_media, ...)`` — the shape
        in the reported log, and ``get_entity``/``get_messages``/``get_dialogs``."""
        client = _dead_client()
        assert telegram_backup._retry_client(client.download_media, None) is client
        assert telegram_backup._retry_client(client.get_entity, None) is client
        assert telegram_backup._retry_client(client.get_messages, None) is client

    def test_the_client_itself_as_the_callable(self):
        """``call_with_flood_retry(self.client, GetContactsRequest(hash=0))`` —
        Telethon invokes a raw request by calling the client, so there is no
        ``__self__`` to inspect."""
        client = _dead_client()
        assert telegram_backup._retry_client(client, None) is client

    def test_bound_method_of_a_component_that_owns_the_client(self):
        """``call_with_flood_retry(self._fetch_media_bytes_bounded, ...)``."""
        client = _dead_client()

        class Backup:
            def __init__(self):
                self.client = client

            async def _fetch_media_bytes_bounded(self):
                return None

        assert telegram_backup._retry_client(Backup()._fetch_media_bytes_bounded, None) is client

    def test_local_closure_passes_the_client_explicitly(self):
        """``_refresh_message_for_media`` wraps a closure, so it must pass
        ``client=``; without it the closure resolves to nothing."""
        client = _dead_client()

        async def _get_messages_once():
            return None

        assert telegram_backup._retry_client(_get_messages_once, None) is None
        assert telegram_backup._retry_client(_get_messages_once, client) is client

    async def test_raw_request_call_reconnects_end_to_end(self, no_sleep, monkeypatch):
        """The ``client(request)`` shape must actually reconnect, not just resolve.

        Before the fix this shape resolved to no client at all, so raw requests
        (``GetContactsRequest``, ``GetForumTopicsRequest``,
        ``GetCustomEmojiDocumentsRequest``) never reconnected.
        """
        client = _dead_client()
        connects = {"n": 0}

        async def fake_connect():
            connects["n"] += 1
            client._sender._user_connected = True

        client.connect = fake_connect
        calls = {"n": 0}

        async def fake_call(self, request, *args, **kwargs):
            calls["n"] += 1
            if not self.is_connected():
                raise ConnectionError("Cannot send requests while disconnected")
            return "contacts"

        # ``client(request)`` resolves __call__ on the type, so patch it there.
        monkeypatch.setattr(TelegramClient, "__call__", fake_call)

        result = await telegram_backup.call_with_flood_retry(client, object())

        assert result == "contacts"
        assert connects["n"] == 1
        assert calls["n"] == 2


class TestMediaRefreshPassesItsClient:
    async def test_refresh_message_for_media_reconnects(self, no_sleep):
        """``_refresh_message_for_media`` runs on the media path that reported
        #265; its retried callable is a closure, so the client must be passed."""
        from src.telegram_backup import TelegramBackup

        client = _dead_client()
        connects = {"n": 0}

        async def fake_connect():
            connects["n"] += 1
            client._sender._user_connected = True

        client.connect = fake_connect
        calls = {"n": 0}

        async def get_messages(chat_id, ids=None):
            calls["n"] += 1
            if not client.is_connected():
                raise ConnectionError("Cannot send requests while disconnected")
            return [SimpleNamespace(id=7)]

        client.get_messages = get_messages

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.client = client
        backup.config = MagicMock()

        fresh = await backup._refresh_message_for_media(-100123, SimpleNamespace(id=7))

        assert fresh is not None
        assert connects["n"] == 1
        assert calls["n"] == 2


class TestIterMessagesReconnects:
    """#265 for long iterations: a disconnect used to abort the whole chat."""

    async def _drain(self, client):
        return [m.id async for m in telegram_backup.iter_messages_with_flood_retry(client, "chat", reverse=True)]

    async def test_disconnect_mid_iteration_reconnects_and_resumes(self, no_sleep):
        client = _dead_client()
        events: list[str] = []

        async def fake_connect():
            events.append("connect")
            client._sender._user_connected = True

        client.connect = fake_connect
        calls = {"n": 0}

        async def iter_messages(entity, min_id=0, **_):
            calls["n"] += 1
            events.append(f"iter(min_id={min_id})")
            if calls["n"] == 1:
                yield SimpleNamespace(id=11)
                raise ConnectionError("Cannot send requests while disconnected")
            for i in (12, 13):
                yield SimpleNamespace(id=i)

        client.iter_messages = iter_messages

        assert await self._drain(client) == [11, 12, 13]
        # Reconnect happens before the resumed iteration, which starts after the
        # last yielded id instead of restarting the chat.
        assert events == ["iter(min_id=0)", "connect", "iter(min_id=11)"]

    async def test_iteration_gives_up_after_the_bounded_retries(self, no_sleep):
        client = _dead_client()
        connects = {"n": 0}

        async def fake_connect():
            connects["n"] += 1  # still down: never restores the sender

        client.connect = fake_connect

        async def iter_messages(entity, min_id=0, **_):
            raise ConnectionError("Cannot send requests while disconnected")
            yield  # pragma: no cover — satisfies the async-generator contract

        client.iter_messages = iter_messages

        with pytest.raises(ConnectionError, match="Cannot send requests while disconnected"):
            await self._drain(client)

        assert connects["n"] == telegram_backup.MAX_FLOOD_RETRIES

    async def test_permanent_rpc_errors_still_propagate_untouched(self, no_sleep):
        """Only connection-shaped failures are retried; a permanent Telegram
        error must not spend five backoffs first."""
        client = _dead_client()
        connects = {"n": 0}

        async def fake_connect():
            connects["n"] += 1

        client.connect = fake_connect

        async def iter_messages(entity, min_id=0, **_):
            raise ChatIdInvalidError(request=None)
            yield  # pragma: no cover

        client.iter_messages = iter_messages

        with pytest.raises(ChatIdInvalidError):
            await self._drain(client)

        assert connects["n"] == 0
        assert no_sleep == []


class TestListenerRestartAfterAGiveUp:
    """The listener half of #265: the scheduler restarted it into a dead client
    every 5 seconds forever, because the gate reads an app-level flag."""

    def _scheduler(self, connection):
        with patch("src.scheduler.signal.signal"):
            from src.scheduler import BackupScheduler

            config = MagicMock()
            config.enable_listener = True
            scheduler = BackupScheduler(config)
        scheduler._connection = connection
        return scheduler

    def _connection(self, client, *, ensure_raises=None):
        """A stand-in for TelegramConnection with its real flag semantics: the
        app-level ``is_connected`` stays True after Telethon gave up."""
        state = {"ensure": 0}

        class Connection:
            def __init__(self):
                self.client = client
                self._connected = True

            @property
            def is_connected(self):
                return self._connected and self.client is not None

            async def ensure_connected(self):
                state["ensure"] += 1
                if ensure_raises is not None:
                    self._connected = False
                    raise ensure_raises
                if not self.client.is_connected():
                    await self.client.connect()
                return self.client

        return Connection(), state

    async def test_real_listener_guard_rejects_a_dead_shared_client(self):
        """Why the restart loop never ended, straight from listener.py."""
        from src.listener import TelegramListener

        client = _dead_client()
        with pytest.raises(RuntimeError, match="Shared client is not connected"):
            await TelegramListener.connect(SimpleNamespace(client=client, _owns_client=False))

    async def test_start_listener_reconnects_the_dead_client_first(self):
        client = _dead_client()

        async def fake_connect():
            client._sender._user_connected = True

        client.connect = fake_connect
        connection, state = self._connection(client)
        scheduler = self._scheduler(connection)

        # The stale app-level flag says "connected" while the client is dead —
        # exactly the state the old gate waved through.
        assert connection.is_connected is True
        assert not client.is_connected()

        seen = {}

        class StubListener:
            @classmethod
            async def create(cls, config, client=None, *, account_id):
                listener = cls()
                listener.client = client
                return listener

            async def connect(self):
                # Mirrors the real guard (src/listener.py, TelegramListener.connect).
                seen["connected_at_connect"] = self.client.is_connected()
                if not self.client.is_connected():
                    raise RuntimeError("Shared client is not connected")

            async def run(self):
                return None

        with patch("src.listener.TelegramListener", StubListener):
            await scheduler._start_listener()

        assert state["ensure"] == 1
        assert seen["connected_at_connect"] is True
        assert scheduler._listener is not None
        assert scheduler._listener_task is not None
        scheduler._listener_task.cancel()

    async def test_start_listener_gives_up_cleanly_when_the_network_is_still_down(self, caplog):
        client = _dead_client()
        connection, state = self._connection(client, ensure_raises=OSError("Network is unreachable"))
        scheduler = self._scheduler(connection)

        with caplog.at_level(logging.WARNING, logger="src.scheduler"):
            await scheduler._start_listener()

        assert state["ensure"] == 1
        assert scheduler._listener is None
        assert scheduler._listener_task is None
        assert any("Cannot start listener" in r.getMessage() for r in caplog.records)
