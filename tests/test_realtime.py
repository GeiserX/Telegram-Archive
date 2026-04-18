"""
Tests for the realtime notification module (src/realtime.py).
"""

import asyncio
import json
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.realtime import (
    NotificationType,
    RealtimeListener,
    RealtimeNotifier,
    _json_serializer,
)


class TestJsonSerializer(unittest.TestCase):
    """Tests for the _json_serializer helper."""

    def test_serializes_datetime_to_isoformat(self):
        """_json_serializer converts datetime objects to ISO format strings."""
        dt = datetime(2025, 6, 15, 12, 30, 45)
        result = _json_serializer(dt)
        self.assertEqual(result, "2025-06-15T12:30:45")

    def test_raises_type_error_for_unsupported_type(self):
        """_json_serializer raises TypeError for non-datetime objects."""
        with self.assertRaises(TypeError) as ctx:
            _json_serializer(set())
        self.assertIn("set", str(ctx.exception))

    def test_works_with_json_dumps(self):
        """_json_serializer integrates correctly with json.dumps."""
        data = {"timestamp": datetime(2025, 1, 1, 0, 0, 0)}
        result = json.dumps(data, default=_json_serializer)
        self.assertIn("2025-01-01T00:00:00", result)


class TestNotificationType(unittest.TestCase):
    """Tests for NotificationType enum."""

    def test_notification_type_values(self):
        """NotificationType enum has expected string values."""
        self.assertEqual(NotificationType.NEW_MESSAGE.value, "new_message")
        self.assertEqual(NotificationType.EDIT.value, "edit")
        self.assertEqual(NotificationType.DELETE.value, "delete")
        self.assertEqual(NotificationType.CHAT_UPDATE.value, "chat_update")
        self.assertEqual(NotificationType.PIN.value, "pin")

    def test_notification_type_is_string_enum(self):
        """NotificationType members are also strings."""
        self.assertIsInstance(NotificationType.NEW_MESSAGE, str)
        self.assertEqual(str(NotificationType.NEW_MESSAGE), "NotificationType.NEW_MESSAGE")


class TestRealtimeNotifierInit(unittest.TestCase):
    """Tests for RealtimeNotifier.__init__."""

    def test_init_defaults(self):
        """RealtimeNotifier initializes with correct defaults."""
        notifier = RealtimeNotifier()
        self.assertIsNone(notifier._db_manager)
        self.assertFalse(notifier._is_postgresql)
        self.assertIsNone(notifier._http_endpoint)
        self.assertFalse(notifier._initialized)

    def test_init_with_db_manager(self):
        """RealtimeNotifier stores the db_manager."""
        mock_db = MagicMock()
        notifier = RealtimeNotifier(db_manager=mock_db)
        self.assertIs(notifier._db_manager, mock_db)


class TestRealtimeNotifierInitMethod:
    """Tests for RealtimeNotifier.init (async)."""

    async def test_init_detects_postgresql_from_db_manager(self):
        """init() detects PostgreSQL when db_manager._is_sqlite is False."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)

        await notifier.init()

        assert notifier._is_postgresql is True
        assert notifier._initialized is True

    async def test_init_detects_sqlite_from_db_manager(self):
        """init() detects SQLite when db_manager._is_sqlite is True."""
        mock_db = MagicMock()
        mock_db._is_sqlite = True
        notifier = RealtimeNotifier(db_manager=mock_db)

        await notifier.init()

        assert notifier._is_postgresql is False
        assert notifier._http_endpoint is not None
        assert notifier._initialized is True

    async def test_init_detects_postgresql_from_env(self):
        """init() detects PostgreSQL from DB_TYPE env var."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "postgresql"}, clear=False):
            await notifier.init()

        assert notifier._is_postgresql is True

    async def test_init_detects_postgres_alias_from_env(self):
        """init() recognizes 'postgres' as PostgreSQL."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "postgres"}, clear=False):
            await notifier.init()

        assert notifier._is_postgresql is True

    async def test_init_defaults_to_sqlite_from_env(self):
        """init() defaults to SQLite mode when DB_TYPE is not set."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {}, clear=True):
            await notifier.init()

        assert notifier._is_postgresql is False
        assert notifier._http_endpoint == "http://localhost:8080/internal/push"

    async def test_init_uses_custom_viewer_host_and_port(self):
        """init() uses VIEWER_HOST and VIEWER_PORT env vars for HTTP endpoint."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "sqlite", "VIEWER_HOST": "myhost", "VIEWER_PORT": "9090"}, clear=True):
            await notifier.init()

        assert notifier._http_endpoint == "http://myhost:9090/internal/push"

    async def test_init_is_idempotent(self):
        """init() only runs once even if called multiple times."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {}, clear=True):
            await notifier.init()

        notifier._is_postgresql = True  # Mutate state
        await notifier.init()  # Should not re-init

        assert notifier._is_postgresql is True  # Should stay mutated


class TestRealtimeNotifierNotify:
    """Tests for RealtimeNotifier.notify."""

    async def test_notify_auto_initializes_if_not_initialized(self):
        """notify() calls init() if not yet initialized."""
        notifier = RealtimeNotifier()

        with (
            patch.dict(os.environ, {"DB_TYPE": "sqlite"}, clear=True),
            patch.object(notifier, "_notify_http", new_callable=AsyncMock) as mock_http,
        ):
            await notifier.notify(NotificationType.NEW_MESSAGE, 123, {"text": "hello"})

        assert notifier._initialized is True

    async def test_notify_truncates_long_message_text(self):
        """notify() truncates message text longer than 500 chars."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        long_text = "x" * 1000
        data = {"message": {"text": long_text, "id": 1}}

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.NEW_MESSAGE, 123, data)

            call_payload = mock_pg.call_args[0][0]
            assert len(call_payload["data"]["message"]["text"]) == 501  # 500 + ellipsis char

    async def test_notify_does_not_truncate_short_message_text(self):
        """notify() preserves message text shorter than 500 chars."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        data = {"message": {"text": "short text", "id": 1}}

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.EDIT, 123, data)

            call_payload = mock_pg.call_args[0][0]
            assert call_payload["data"]["message"]["text"] == "short text"

    async def test_notify_does_not_truncate_when_no_message_key(self):
        """notify() handles data without 'message' key gracefully."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        data = {"chat_name": "Test"}

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.CHAT_UPDATE, 123, data)

            call_payload = mock_pg.call_args[0][0]
            assert call_payload["data"]["chat_name"] == "Test"

    async def test_notify_routes_to_postgres(self):
        """notify() routes to _notify_postgres when PostgreSQL detected."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.DELETE, 456, {"msg_id": 1})

            mock_pg.assert_called_once()
            payload = mock_pg.call_args[0][0]
            assert payload["type"] == "delete"
            assert payload["chat_id"] == 456

    async def test_notify_routes_to_http(self):
        """notify() routes to _notify_http when SQLite detected."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "sqlite"}, clear=True):
            await notifier.init()

        with patch.object(notifier, "_notify_http", new_callable=AsyncMock) as mock_http:
            await notifier.notify(NotificationType.PIN, 789, {"msg_id": 5})

            mock_http.assert_called_once()
            payload = mock_http.call_args[0][0]
            assert payload["type"] == "pin"
            assert payload["chat_id"] == 789

    async def test_notify_catches_notification_failures(self):
        """notify() catches and logs exceptions without raising."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock, side_effect=Exception("db error")):
            # Should NOT raise
            await notifier.notify(NotificationType.NEW_MESSAGE, 123, {})

    async def test_notify_does_not_mutate_original_data(self):
        """notify() creates a copy when truncating, not mutating original."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "sqlite"}, clear=True):
            await notifier.init()

        long_text = "x" * 1000
        original_data = {"message": {"text": long_text, "id": 1}}

        with patch.object(notifier, "_notify_http", new_callable=AsyncMock):
            await notifier.notify(NotificationType.NEW_MESSAGE, 123, original_data)

        assert len(original_data["message"]["text"]) == 1000  # Original unchanged


class TestRealtimeNotifierPostgres:
    """Tests for RealtimeNotifier._notify_postgres."""

    async def test_notify_postgres_without_db_manager_is_noop(self):
        """_notify_postgres returns early if no db_manager."""
        notifier = RealtimeNotifier()
        notifier._is_postgresql = True
        notifier._initialized = True

        # Should not raise
        await notifier._notify_postgres({"type": "test"})

    async def test_notify_postgres_executes_notify_command(self):
        """_notify_postgres sends NOTIFY via SQL."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.async_session_factory.return_value = mock_session

        notifier = RealtimeNotifier(db_manager=mock_db)
        notifier._is_postgresql = True
        notifier._initialized = True

        payload = {"type": "new_message", "chat_id": 123}

        await notifier._notify_postgres(payload)

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()


class TestRealtimeNotifierHttp:
    """Tests for RealtimeNotifier._notify_http."""

    async def test_notify_http_without_endpoint_is_noop(self):
        """_notify_http returns early if no endpoint configured."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = None

        # Should not raise
        await notifier._notify_http({"type": "test"})

    async def test_notify_http_sends_post_with_aiohttp(self):
        """_notify_http sends POST request via aiohttp."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        # aiohttp uses nested async context managers:
        # async with aiohttp.ClientSession() as session, session.post(...) as response:
        mock_response = MagicMock()
        mock_response.status = 200

        mock_post_cm = AsyncMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session_cm
        mock_aiohttp.ClientTimeout = MagicMock()

        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            await notifier._notify_http({"type": "test"})

        mock_session.post.assert_called_once()

    async def test_notify_http_includes_auth_header_when_secret_set(self):
        """_notify_http includes Authorization header when INTERNAL_PUSH_SECRET is set."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        mock_response = MagicMock()
        mock_response.status = 200

        mock_post_cm = AsyncMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session_cm
        mock_aiohttp.ClientTimeout = MagicMock()

        with (
            patch.dict(os.environ, {"INTERNAL_PUSH_SECRET": "my-secret"}, clear=False),
            patch.dict("sys.modules", {"aiohttp": mock_aiohttp}),
        ):
            await notifier._notify_http({"type": "test"})

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer my-secret"

    async def test_notify_http_falls_back_to_httpx_when_aiohttp_unavailable(self):
        """_notify_http falls back to httpx when aiohttp is not installed."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        mock_httpx_client = AsyncMock()
        mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
        mock_httpx_client.__aexit__ = AsyncMock(return_value=False)

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("no aiohttp")
            return real_import(name, *args, **kwargs)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("builtins.__import__", side_effect=mock_import),
            patch("httpx.AsyncClient", return_value=mock_httpx_client),
        ):
            await notifier._notify_http({"type": "test"})

            mock_httpx_client.post.assert_called_once()

    async def test_notify_http_logs_warning_when_neither_library_available(self):
        """_notify_http logs warning when neither aiohttp nor httpx is available."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name in ("aiohttp", "httpx"):
                raise ImportError(f"no {name}")
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {}, clear=True), patch("builtins.__import__", side_effect=mock_import):
            # Should not raise
            await notifier._notify_http({"type": "test"})

    async def test_notify_http_handles_connection_error(self):
        """_notify_http handles network errors gracefully."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.side_effect = Exception("connection refused")
        mock_aiohttp.ClientTimeout = MagicMock()

        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            # Should not raise
            await notifier._notify_http({"type": "test"})


class TestRealtimeListenerInit(unittest.TestCase):
    """Tests for RealtimeListener.__init__."""

    def test_init_defaults(self):
        """RealtimeListener initializes with correct defaults."""
        listener = RealtimeListener()
        self.assertIsNone(listener._db_manager)
        self.assertIsNone(listener._callback)
        self.assertFalse(listener._is_postgresql)
        self.assertFalse(listener._running)
        self.assertIsNone(listener._task)

    def test_init_with_db_manager_and_callback(self):
        """RealtimeListener stores db_manager and callback."""
        mock_db = MagicMock()
        mock_cb = AsyncMock()
        listener = RealtimeListener(db_manager=mock_db, callback=mock_cb)
        self.assertIs(listener._db_manager, mock_db)
        self.assertIs(listener._callback, mock_cb)


class TestRealtimeListenerInitMethod:
    """Tests for RealtimeListener.init (async)."""

    async def test_init_detects_postgresql_from_db_manager(self):
        """init() detects PostgreSQL from db_manager."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        listener = RealtimeListener(db_manager=mock_db)

        await listener.init()

        assert listener._is_postgresql is True

    async def test_init_detects_sqlite_from_db_manager(self):
        """init() detects SQLite from db_manager."""
        mock_db = MagicMock()
        mock_db._is_sqlite = True
        listener = RealtimeListener(db_manager=mock_db)

        await listener.init()

        assert listener._is_postgresql is False

    async def test_init_detects_postgresql_from_env(self):
        """init() detects PostgreSQL from DB_TYPE env var."""
        listener = RealtimeListener()

        with patch.dict(os.environ, {"DB_TYPE": "postgresql"}, clear=True):
            await listener.init()

        assert listener._is_postgresql is True

    async def test_init_defaults_to_sqlite(self):
        """init() defaults to SQLite when DB_TYPE not set."""
        listener = RealtimeListener()

        with patch.dict(os.environ, {}, clear=True):
            await listener.init()

        assert listener._is_postgresql is False


class TestRealtimeListenerStart:
    """Tests for RealtimeListener.start."""

    async def test_start_is_noop_for_sqlite(self):
        """start() does nothing when in SQLite mode."""
        listener = RealtimeListener()
        listener._is_postgresql = False

        await listener.start()

        assert listener._task is None
        assert listener._running is False

    async def test_start_creates_task_for_postgresql(self):
        """start() creates a background task for PostgreSQL LISTEN."""
        listener = RealtimeListener()
        listener._is_postgresql = True

        with patch("src.realtime.asyncio.create_task") as mock_task:
            mock_task.return_value = MagicMock()
            await listener.start()

            assert listener._running is True
            mock_task.assert_called_once()


class TestRealtimeListenerStop:
    """Tests for RealtimeListener.stop."""

    async def test_stop_cancels_running_task(self):
        """stop() cancels the running task."""
        listener = RealtimeListener()
        listener._running = True

        # Create a real future that raises CancelledError when awaited
        loop = asyncio.get_event_loop()
        mock_task = loop.create_future()
        mock_task.cancel()
        listener._task = mock_task

        await listener.stop()

        assert listener._running is False

    async def test_stop_when_no_task_is_safe(self):
        """stop() is safe when no task is running."""
        listener = RealtimeListener()
        listener._running = True
        listener._task = None

        await listener.stop()

        assert listener._running is False


class TestRealtimeListenerPgCallback(unittest.TestCase):
    """Tests for RealtimeListener._pg_callback."""

    def test_pg_callback_parses_json_and_schedules_callback(self):
        """_pg_callback parses JSON payload and creates async task."""
        mock_cb = AsyncMock()
        listener = RealtimeListener(callback=mock_cb)

        payload = json.dumps({"type": "new_message", "chat_id": 123})

        with patch("src.realtime.asyncio.create_task") as mock_task:
            listener._pg_callback(None, 0, "telegram_updates", payload)

            mock_task.assert_called_once()

    def test_pg_callback_handles_invalid_json(self):
        """_pg_callback handles invalid JSON gracefully."""
        mock_cb = AsyncMock()
        listener = RealtimeListener(callback=mock_cb)

        # Should not raise
        listener._pg_callback(None, 0, "telegram_updates", "not-valid-json{{{")

    def test_pg_callback_does_nothing_without_callback(self):
        """_pg_callback is a no-op when no callback is set."""
        listener = RealtimeListener()

        # Should not raise
        listener._pg_callback(None, 0, "telegram_updates", '{"type": "test"}')


class TestRealtimeListenerHttpPush:
    """Tests for RealtimeListener.handle_http_push."""

    async def test_handle_http_push_calls_callback(self):
        """handle_http_push passes payload to the callback."""
        mock_cb = AsyncMock()
        listener = RealtimeListener(callback=mock_cb)

        payload = {"type": "new_message", "chat_id": 123}
        await listener.handle_http_push(payload)

        mock_cb.assert_called_once_with(payload)

    async def test_handle_http_push_without_callback_is_noop(self):
        """handle_http_push does nothing when no callback is set."""
        listener = RealtimeListener()

        # Should not raise
        await listener.handle_http_push({"type": "test"})
