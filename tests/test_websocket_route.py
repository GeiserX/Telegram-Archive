"""End-to-end tests for the @app.websocket("/ws/updates") route in src/web/main.py.

Before this file, only ConnectionManager's plain-async methods had unit coverage
(tests/test_web_main.py); nothing exercised the actual route handler -- origin
checks, cookie/session auth gating, or the subscribe/unsubscribe message loop.

Uses FastAPI's synchronous TestClient.websocket_connect, following the same
module-reload + mock-db pattern as tests/test_multi_user_auth.py so that
AUTH_ENABLED reflects real env vars rather than mocked module attributes.
"""

import importlib
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

# Config() creates BACKUP_PATH on import (default /data/backups, read-only in
# this sandbox). Must be set before src.web.main is ever imported, including
# by the first module-level import below and every later importlib.reload().
os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_ws_"))

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _WS_AVAILABLE = True
except Exception:
    _WS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _WS_AVAILABLE, reason="fastapi/starlette TestClient import failed")


def _make_mock_db():
    db = AsyncMock()
    db.get_all_chats = AsyncMock(return_value=[])
    db.get_session = AsyncMock(return_value=None)
    db.delete_session = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _reset_sessions():
    import src.web.main as main_mod

    main_mod._sessions.clear()
    yield
    main_mod._sessions.clear()


@pytest.fixture
def auth_env():
    """Real VIEWER_USERNAME/PASSWORD so AUTH_ENABLED is True after module reload."""
    with patch.dict(
        os.environ,
        {"VIEWER_USERNAME": "admin", "VIEWER_PASSWORD": "test@value/here", "SECURE_COOKIES": "false"},
    ):
        yield


def _get_client():
    """Create a fresh TestClient by reloading the module with current env, like test_multi_user_auth.py."""
    import src.web.main as main_mod

    importlib.reload(main_mod)
    main_mod.db = _make_mock_db()
    return TestClient(main_mod.app, raise_server_exceptions=False), main_mod


class TestWebSocketAuthGate:
    """The route validates auth from the cookie during the WS upgrade, before accept()."""

    def test_authenticated_connect_succeeds_and_round_trips_ping(self, auth_env):
        """A valid session cookie is accepted; the connection is live (ping -> pong)."""
        client, mod = _get_client()
        token = "ws-valid-session"
        mod._sessions[token] = mod.SessionData(username="admin", role="master", created_at=time.time())
        client.cookies.set("viewer_auth", token)

        with client.websocket_connect("/ws/updates") as ws:
            ws.send_json({"action": "ping"})
            reply = ws.receive_json()

        assert reply == {"type": "pong"}

    def test_unauthenticated_connect_is_rejected(self, auth_env):
        """No cookie + AUTH_ENABLED closes the socket with 4001 before it is ever accepted."""
        client, _mod = _get_client()

        with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws/updates"):
            pass

        assert exc_info.value.code == 4001

    def test_expired_session_is_rejected(self, auth_env):
        """A session older than AUTH_SESSION_SECONDS is rejected the same way as no cookie."""
        client, mod = _get_client()
        token = "ws-expired-session"
        mod._sessions[token] = mod.SessionData(
            username="admin",
            role="master",
            created_at=time.time() - mod.AUTH_SESSION_SECONDS - 100,
        )
        client.cookies.set("viewer_auth", token)

        with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws/updates"):
            pass

        assert exc_info.value.code == 4001


class TestWebSocketSubscribeFlow:
    """Once connected, the route accepts subscribe/unsubscribe action messages."""

    def test_subscribe_and_unsubscribe_round_trip(self, auth_env):
        client, mod = _get_client()
        token = "ws-subscribe-session"
        mod._sessions[token] = mod.SessionData(username="admin", role="master", created_at=time.time())
        client.cookies.set("viewer_auth", token)

        with client.websocket_connect("/ws/updates") as ws:
            ws.send_json({"action": "subscribe", "chat_id": 42})
            assert ws.receive_json() == {"type": "subscribed", "chat_id": 42}

            ws.send_json({"action": "unsubscribe", "chat_id": 42})
            assert ws.receive_json() == {"type": "unsubscribed", "chat_id": 42}

    def test_subscribe_denied_for_chat_outside_allowed_ids(self, auth_env):
        """A viewer session restricted to specific chats gets subscribe_denied for others."""
        client, mod = _get_client()
        token = "ws-restricted-viewer"
        mod._sessions[token] = mod.SessionData(
            username="v1", role="viewer", allowed_chat_ids={1, 2}, created_at=time.time()
        )
        client.cookies.set("viewer_auth", token)

        with client.websocket_connect("/ws/updates") as ws:
            ws.send_json({"action": "subscribe", "chat_id": 999})
            assert ws.receive_json() == {"type": "subscribe_denied", "chat_id": 999}
