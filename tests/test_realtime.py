"""
Tests for the realtime notification module (src/realtime.py).
"""

import asyncio
import contextlib
import json
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.realtime import (
    NotificationType,
    RealtimeListener,
    RealtimeNotifier,
    _json_serializer,
    resolve_internal_push_secret,
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

    async def test_init_database_url_takes_precedence_over_db_type(self):
        """DATABASE_URL chooses transport before DB_TYPE fallback."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@host/db", "DB_TYPE": "sqlite"}, clear=True):
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

    async def test_notify_truncates_long_edit_new_text(self):
        """notify() truncates data["new_text"] for edit notifications (8KB guard)."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        long_text = "x" * 1000
        data = {"chat_id": 42, "message_id": 1, "new_text": long_text}

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.EDIT, 42, data)

            call_payload = mock_pg.call_args[0][0]
            assert len(call_payload["data"]["new_text"]) == 501  # 500 + ellipsis char

    async def test_notify_does_not_truncate_short_edit_new_text(self):
        """notify() preserves short data["new_text"] for edit notifications."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        data = {"chat_id": 42, "message_id": 1, "new_text": "short edit"}

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.EDIT, 42, data)

            call_payload = mock_pg.call_args[0][0]
            assert call_payload["data"]["new_text"] == "short edit"

    async def test_notify_does_not_mutate_original_edit_data(self):
        """notify() creates a copy when truncating edit data, not mutating original."""
        notifier = RealtimeNotifier()

        with patch.dict(os.environ, {"DB_TYPE": "sqlite"}, clear=True):
            await notifier.init()

        long_text = "x" * 1000
        original_data = {"chat_id": 42, "message_id": 1, "new_text": long_text}

        with patch.object(notifier, "_notify_http", new_callable=AsyncMock):
            await notifier.notify(NotificationType.EDIT, 42, original_data)

        assert len(original_data["new_text"]) == 1000  # Original unchanged


class TestRealtimeNotifierPostgres:
    """Tests for RealtimeNotifier._notify_postgres."""

    async def test_notify_postgres_without_db_manager_is_noop(self):
        """_notify_postgres returns early if no db_manager."""
        notifier = RealtimeNotifier()
        notifier._is_postgresql = True
        notifier._initialized = True

        # Should not raise
        await notifier._notify_postgres({"type": "test"})

    @staticmethod
    def _make_notifier_with_fake_session():
        """Build a RealtimeNotifier wired to an AsyncMock session.

        Returns ``(notifier, session)`` so tests can drive .notify() and
        then inspect what was sent through the session. The ``__aenter__``
        return_value is set so ``async with session as s:`` binds ``s`` back
        to the same mock — without this, AsyncMock returns a fresh child mock
        and the assertions miss the real call.
        """
        session = AsyncMock()
        session.__aenter__.return_value = session

        db_manager = MagicMock()
        db_manager.async_session_factory = MagicMock(return_value=session)
        db_manager._is_sqlite = False

        notifier = RealtimeNotifier(db_manager=db_manager)
        notifier._is_postgresql = True
        notifier._initialized = True
        return notifier, session

    async def test_notify_postgres_uses_pg_notify_with_bound_params(self):
        """_notify_postgres must call SELECT pg_notify(:channel, :payload) with bound params."""
        notifier, session = self._make_notifier_with_fake_session()

        await notifier._notify_postgres({"type": "new_message", "chat_id": 123})

        assert session.execute.await_count == 1
        assert session.commit.await_count == 1

        stmt = session.execute.await_args.args[0]
        assert "pg_notify" in stmt.text.lower()

        params = session.execute.await_args.args[1]
        assert params["channel"] == "telegram_updates"
        assert "new_message" in params["payload"]

    async def test_notify_does_not_interpolate_payload_into_sql(self):
        """Regression for upstream PR #123: payload must be a bound parameter,
        never embedded in the SQL string. The original implementation f-string-
        interpolated the JSON, which made asyncpg blow up whenever a message
        contained tokens like ``$1`` or ``$D`` (parsed as positional placeholders)."""
        notifier, session = self._make_notifier_with_fake_session()

        await notifier.notify(
            NotificationType.NEW_MESSAGE,
            chat_id=1011405549,
            data={"message": {"id": 1, "text": "this will $1 break $D asyncpg"}},
        )

        assert session.execute.await_count == 1
        assert session.commit.await_count == 1

        stmt = session.execute.await_args.args[0]
        assert "$1 break" not in stmt.text
        assert "$D" not in stmt.text
        assert "pg_notify" in stmt.text.lower()

        params = session.execute.await_args.args[1]
        assert params["channel"] == "telegram_updates"
        assert "$1 break $D asyncpg" in params["payload"]

    async def test_notify_handles_single_quotes_in_payload(self):
        """Single quotes inside the payload must round-trip cleanly through bound
        parameters — replacing the old manual ``.replace(\"'\", \"''\")`` escaping."""
        notifier, session = self._make_notifier_with_fake_session()

        await notifier.notify(
            NotificationType.NEW_MESSAGE,
            chat_id=42,
            data={"message": {"id": 1, "text": "Ryan's note: it's working"}},
        )

        assert session.execute.await_count == 1
        params = session.execute.await_args.args[1]
        assert "Ryan's note: it's working" in params["payload"]

    async def test_notify_survives_dollar_tokens_without_warning(self, caplog):
        """End-to-end: .notify() with dollar-tokens must not emit the old
        ``Failed to send realtime notification`` warning."""
        notifier, _session = self._make_notifier_with_fake_session()

        with caplog.at_level("WARNING", logger="src.realtime"):
            await notifier.notify(
                NotificationType.EDIT,
                chat_id=42,
                data={"chat_id": 42, "message_id": 1, "new_text": "sshhhh $1 will take LONGER! $O"},
            )

        messages = [r.getMessage() for r in caplog.records]
        assert not any("Failed to send realtime notification" in m for m in messages), messages


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

    async def test_init_database_url_takes_precedence_over_db_type(self):
        """Listener and notifier share DATABASE_URL-first transport detection."""
        listener = RealtimeListener()

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@host/db", "DB_TYPE": "sqlite"}, clear=True):
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


class TestPgCallbackTaskTracking(unittest.IsolatedAsyncioTestCase):
    """Tests for callback task bookkeeping in _pg_callback (task-leak fix).

    Previously ``asyncio.create_task(self._callback(data))`` was fired without
    keeping a reference, so the task could be garbage-collected mid-flight and
    any exception it raised was silently dropped.
    """

    async def test_callback_task_tracked_then_discarded_on_success(self):
        """A successful callback task is tracked while running, then discarded."""

        async def ok_callback(data):
            return None

        listener = RealtimeListener(callback=ok_callback)
        payload = json.dumps({"type": "new_message", "chat_id": 123})

        listener._pg_callback(None, 0, "telegram_updates", payload)
        self.assertEqual(len(listener._callback_tasks), 1)

        task = next(iter(listener._callback_tasks))
        await task
        await asyncio.sleep(0)  # let the done-callback (call_soon) run

        self.assertEqual(listener._callback_tasks, set())

    async def test_callback_task_exception_logged_without_payload(self):
        """A callback exception is logged (class/message only) and the task set
        is cleaned up -- the notification payload must never appear in the log
        (PII rule: no message content)."""

        async def failing_callback(data):
            raise ValueError("callback boom")

        listener = RealtimeListener(callback=failing_callback)
        secret_payload = json.dumps({"type": "new_message", "chat_id": 123, "message": {"text": "super secret text"}})

        with self.assertLogs("src.realtime", level="WARNING") as ctx:
            listener._pg_callback(None, 0, "telegram_updates", secret_payload)
            task = next(iter(listener._callback_tasks))
            with contextlib.suppress(ValueError):
                await task
            await asyncio.sleep(0)  # let the done-callback fire

        self.assertEqual(listener._callback_tasks, set())
        messages = ctx.output
        self.assertTrue(any("ValueError" in m and "callback boom" in m for m in messages))
        self.assertFalse(any("super secret text" in m for m in messages))


# ===========================================================================
# _notify_http non-200 response (line 149)
# ===========================================================================


class TestNotifyHttpNon200:
    """Tests for _notify_http when server returns non-200 (line 149)."""

    async def test_non_200_response_logs_warning(self):
        """_notify_http logs warning when response status is not 200."""
        notifier = RealtimeNotifier()
        notifier._http_endpoint = "http://localhost:8080/internal/push"

        mock_response = MagicMock()
        mock_response.status = 500

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


# ===========================================================================
# _listen_postgres (lines 218-242)
# ===========================================================================


class TestListenPostgres:
    """Tests for RealtimeListener._listen_postgres (lines 218-242)."""

    async def test_listen_postgres_connects_and_listens(self):
        """_listen_postgres connects to asyncpg and adds listener."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.database_url = "postgresql+asyncpg://user:pass@localhost/db"

        listener = RealtimeListener(db_manager=mock_db, callback=AsyncMock())
        listener._is_postgresql = True
        listener._running = True

        mock_conn = AsyncMock()
        mock_conn.add_listener = AsyncMock()
        mock_conn.remove_listener = AsyncMock()
        mock_conn.close = AsyncMock()

        call_count = 0

        async def stop_after_one(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                listener._running = False

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("asyncio.sleep", side_effect=stop_after_one),
        ):
            await listener._listen_postgres()

        mock_asyncpg.connect.assert_awaited_once()
        mock_conn.add_listener.assert_awaited_once()
        mock_conn.remove_listener.assert_awaited_once()
        mock_conn.close.assert_awaited_once()

    async def test_listen_postgres_reconnects_on_error(self):
        """_listen_postgres retries on connection error (lines 240-242)."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.database_url = "postgresql+asyncpg://user:pass@localhost/db"

        listener = RealtimeListener(db_manager=mock_db, callback=AsyncMock())
        listener._is_postgresql = True
        listener._running = True

        attempt = [0]

        mock_asyncpg = MagicMock()

        async def fail_then_stop(*args, **kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                raise Exception("connection refused")
            # Second attempt: stop running
            listener._running = False
            mock_conn = AsyncMock()
            mock_conn.add_listener = AsyncMock()
            mock_conn.remove_listener = AsyncMock()
            mock_conn.close = AsyncMock()
            return mock_conn

        mock_asyncpg.connect = AsyncMock(side_effect=fail_then_stop)

        async def fake_sleep(seconds):
            pass

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await listener._listen_postgres()

        assert attempt[0] >= 2

    async def test_listen_postgres_closes_connection_on_exception_after_connect(self):
        """Regression: an exception raised after connect() succeeds (inside the
        "keep connection alive" loop) must still close the connection. Previously
        remove_listener/close only ran on the normal-completion path, so a
        flapping DB leaked one connection per 5s retry."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.database_url = "postgresql+asyncpg://user:pass@localhost/db"

        listener = RealtimeListener(db_manager=mock_db, callback=AsyncMock())
        listener._is_postgresql = True
        listener._running = True

        mock_conn = AsyncMock()
        mock_conn.add_listener = AsyncMock()
        mock_conn.remove_listener = AsyncMock()
        mock_conn.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        call_count = 0

        async def sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First sleep is the inner "keep connection alive" sleep(1);
                # simulate something going wrong while the connection is open.
                raise Exception("keep-alive loop broke")
            # Second sleep is the outer retry-backoff sleep(5); stop the loop.
            listener._running = False

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("asyncio.sleep", side_effect=sleep_side_effect),
        ):
            await listener._listen_postgres()

        mock_conn.remove_listener.assert_awaited_once()
        mock_conn.close.assert_awaited_once()

    async def test_listen_postgres_propagates_cancelled_error(self):
        """CancelledError must escape, never turn into a normal exit — a task
        that swallows it reads as cleanly finished to stop() and to any future
        asyncio.timeout/TaskGroup wrapper (see TestListenPostgresShutdownDiscipline)."""
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.database_url = "postgresql+asyncpg://user:pass@localhost/db"

        listener = RealtimeListener(db_manager=mock_db, callback=AsyncMock())
        listener._is_postgresql = True
        listener._running = True

        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(side_effect=asyncio.CancelledError)

        with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}), pytest.raises(asyncio.CancelledError):
            await listener._listen_postgres()


class TestNotifierAccountIdPayload:
    """#315: the payload names the capturing account so the viewer can scope its lookup."""

    async def test_notify_includes_account_id(self) -> None:
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        notifier = RealtimeNotifier(db_manager=mock_db)
        await notifier.init()

        with patch.object(notifier, "_notify_postgres", new_callable=AsyncMock) as mock_pg:
            await notifier.notify(NotificationType.NEW_MESSAGE, 123, {}, account_id=7)

        assert mock_pg.call_args[0][0]["account_id"] == 7

    async def test_notify_defaults_account_id_to_none(self) -> None:
        """Legacy senders: the key is present and None, keeping the viewer ambiguity guard."""
        notifier = RealtimeNotifier()
        with patch.dict(os.environ, {"DB_TYPE": "sqlite"}, clear=True):
            await notifier.init()

        with patch.object(notifier, "_notify_http", new_callable=AsyncMock) as mock_http:
            await notifier.notify(NotificationType.PIN, 789, {"msg_id": 5})

        assert mock_http.call_args[0][0]["account_id"] is None


class TestListenPostgresShutdownDiscipline:
    """stop() must terminate promptly and honestly even on a wedged connection.

    The old loop caught CancelledError and ``break``-ed — the task finished
    NORMALLY instead of cancelled — and its finally awaited remove_listener()
    and close() unbounded, so a dead TCP socket hung viewer shutdown past its
    grace period into SIGKILL.
    """

    def _listener(self):
        mock_db = MagicMock()
        mock_db._is_sqlite = False
        mock_db.database_url = "postgresql+asyncpg://user:pass@localhost/db"
        listener = RealtimeListener(db_manager=mock_db, callback=AsyncMock())
        listener._is_postgresql = True
        listener._running = True
        return listener

    async def test_cancellation_is_reraised_not_swallowed(self):
        listener = self._listener()
        mock_conn = AsyncMock()
        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        with patch.dict("sys.modules", {"asyncpg": mock_asyncpg}):
            task = asyncio.create_task(listener._listen_postgres())
            # Let it connect and enter the keepalive sleep.
            for _ in range(10):
                await asyncio.sleep(0)
                if mock_conn.add_listener.await_count:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert task.cancelled(), "the listen task must end CANCELLED, not as a normal exit"
        mock_conn.close.assert_awaited_once()

    async def test_stop_completes_when_close_hangs(self):
        listener = self._listener()

        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        mock_conn = AsyncMock()
        mock_conn.remove_listener = AsyncMock(side_effect=hang)
        mock_conn.close = AsyncMock(side_effect=hang)
        mock_asyncpg = MagicMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("src.realtime._TEARDOWN_TIMEOUT_SECONDS", 0.05),
        ):
            listener._task = asyncio.create_task(listener._listen_postgres())
            for _ in range(10):
                await asyncio.sleep(0)
                if mock_conn.add_listener.await_count:
                    break
            # A wedged socket must not hang shutdown: 2s is the tripwire, the
            # bounded teardown finishes in ~0.1s. asyncio.wait — NOT wait_for —
            # because wait_for cancels stop() on timeout, stop()'s own
            # CancelledError guard swallows that cancel, and wait_for then
            # returns "success": a tripwire that cannot fire.
            stop_task = asyncio.ensure_future(listener.stop())
            done, pending = await asyncio.wait([stop_task], timeout=2)
            assert stop_task in done, "stop() hung on the wedged connection"
            # Retrieve stop()'s outcome and pin the teardown contract: both
            # calls must have been attempted (bounded), not skipped outright.
            await stop_task
            mock_conn.remove_listener.assert_awaited_once()
            mock_conn.close.assert_awaited_once()
            for task in pending:
                task.cancel()


class TestResolveInternalPushSecret:
    """The stock two-container SQLite stack must live-update with ZERO config:
    the secret the viewer demands from non-loopback pushers is auto-shared
    through the volume both containers already mount — the same trust boundary
    the secret protects. Explicit env always wins; any trouble means None,
    which keeps the old locked-down behavior."""

    def _clear_env(self, monkeypatch):
        monkeypatch.delenv("INTERNAL_PUSH_SECRET", raising=False)

    def test_explicit_env_always_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INTERNAL_PUSH_SECRET", "operator-chosen")
        url = f"sqlite:///{tmp_path / 'db.sqlite'}"
        assert resolve_internal_push_secret(url) == "operator-chosen"
        assert not (tmp_path / ".push-secret").exists()

    def test_sqlite_url_creates_file_and_both_sides_agree(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        sender_url = f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"
        viewer_url = f"sqlite:///{tmp_path / 'db.sqlite'}"

        sender_secret = resolve_internal_push_secret(sender_url)
        viewer_secret = resolve_internal_push_secret(viewer_url)

        assert sender_secret and sender_secret == viewer_secret
        secret_file = tmp_path / ".push-secret"
        assert secret_file.exists()
        assert (secret_file.stat().st_mode & 0o777) == 0o600
        assert secret_file.read_text().strip() == sender_secret

    def test_existing_file_from_the_other_container_is_read(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        (tmp_path / ".push-secret").write_text("already-minted\n")
        assert resolve_internal_push_secret(f"sqlite:///{tmp_path / 'db.sqlite'}") == "already-minted"

    def test_non_sqlite_memory_and_mock_urls_resolve_to_none(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        assert resolve_internal_push_secret("postgresql+asyncpg://u:p@host/db") is None
        assert resolve_internal_push_secret("sqlite+aiosqlite://") is None
        assert resolve_internal_push_secret(None) is None
        assert resolve_internal_push_secret(MagicMock()) is None

    def test_unwritable_directory_keeps_the_old_locked_down_behavior(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            assert resolve_internal_push_secret(f"sqlite:///{locked / 'db.sqlite'}") is None
        finally:
            locked.chmod(0o700)
