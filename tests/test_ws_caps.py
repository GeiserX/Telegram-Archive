"""WebSocket caps: one client must not grow server state without bound.

The viewer is documented as internet-exposed behind a reverse proxy (and
ALLOW_ANONYMOUS_VIEWER is a supported mode), so both the global connection
table and each socket's subscription set are bounded.
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_wscaps_"))

pytest.importorskip("fastapi")

from src.web.main import ConnectionManager  # noqa: E402


def _socket():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def _user():
    u = MagicMock()
    u.username = "cap-tester"
    return u


class TestConnectionCap:
    async def test_connect_past_cap_closes_with_1013(self):
        manager = ConnectionManager()
        with patch("src.web.main.MAX_WS_CONNECTIONS", 2):
            assert await manager.connect(_socket(), _user()) is True
            assert await manager.connect(_socket(), _user()) is True
            refused = _socket()
            assert await manager.connect(refused, _user()) is False

        refused.close.assert_awaited_once()
        assert refused.close.await_args.kwargs["code"] == 1013
        assert len(manager.active_connections) == 2
        assert refused not in manager.active_connections

    async def test_disconnect_frees_a_slot(self):
        manager = ConnectionManager()
        with patch("src.web.main.MAX_WS_CONNECTIONS", 1):
            first = _socket()
            assert await manager.connect(first, _user()) is True
            manager.disconnect(first)
            assert await manager.connect(_socket(), _user()) is True


class TestSubscriptionCap:
    async def test_new_refs_past_cap_are_refused(self):
        manager = ConnectionManager()
        ws = _socket()
        await manager.connect(ws, _user())

        with patch("src.web.main.MAX_WS_SUBSCRIPTIONS_PER_CONNECTION", 3):
            for n in range(3):
                assert manager.subscribe(ws, f"ref-{n}") is True
            assert manager.subscribe(ws, "ref-overflow") is False
            assert len(manager.active_connections[ws]) == 3

    async def test_resubscribing_an_existing_ref_at_cap_stays_free(self):
        manager = ConnectionManager()
        ws = _socket()
        await manager.connect(ws, _user())

        with patch("src.web.main.MAX_WS_SUBSCRIPTIONS_PER_CONNECTION", 2):
            assert manager.subscribe(ws, "ref-a") is True
            assert manager.subscribe(ws, "ref-b") is True
            assert manager.subscribe(ws, "ref-a") is True  # idempotent, not counted

    async def test_unknown_socket_still_refused(self):
        manager = ConnectionManager()
        assert manager.subscribe(_socket(), "ref-a") is False
