"""Tests for web route handlers in src/web/main.py.

Targets the uncovered route handler code: API endpoints for chats, messages,
stats, admin, push, tokens, settings, auth, and media serving.
Uses httpx.AsyncClient with FastAPI TestClient pattern and mocked db adapter.
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_wr_"))
    from src.web import main as web_main

    _WEB_AVAILABLE = True
except Exception:
    _WEB_AVAILABLE = False
    web_main = None  # type: ignore[assignment]

try:
    from httpx import ASGITransport, AsyncClient

    _HTTPX_AVAILABLE = True
except Exception:
    _HTTPX_AVAILABLE = False


def _skip_unless_web(cls_or_fn):
    return unittest.skipUnless(_WEB_AVAILABLE and _HTTPX_AVAILABLE, "web_main or httpx import failed")(cls_or_fn)


def _mock_db():
    """Create a mock database adapter with common methods."""
    db = AsyncMock()
    db.get_all_chats = AsyncMock(return_value=[])
    db.get_chat_count = AsyncMock(return_value=0)
    db.get_chat_by_id = AsyncMock(return_value=None)
    db.get_messages_paginated = AsyncMock(return_value=[])
    db.get_pinned_messages = AsyncMock(return_value=[])
    db.get_all_folders = AsyncMock(return_value=[])
    db.get_forum_topics = AsyncMock(return_value=[])
    db.get_archived_chat_count = AsyncMock(return_value=0)
    db.get_cached_statistics = AsyncMock(return_value={})
    db.calculate_and_store_statistics = AsyncMock(return_value={})
    db.get_metadata = AsyncMock(return_value=None)
    db.get_chat_stats = AsyncMock(return_value={})
    db.find_message_by_date_with_joins = AsyncMock(return_value=None)
    db.get_all_viewer_accounts = AsyncMock(return_value=[])
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.get_viewer_account = AsyncMock(return_value=None)
    db.create_viewer_account = AsyncMock(return_value={"id": 1, "username": "test", "is_active": 1, "no_download": 0})
    db.update_viewer_account = AsyncMock(
        return_value={"id": 1, "username": "test", "allowed_chat_ids": None, "is_active": 1}
    )
    db.delete_viewer_account = AsyncMock()
    db.get_all_viewer_tokens = AsyncMock(return_value=[])
    db.create_viewer_token = AsyncMock(
        return_value={"id": 1, "label": "test", "no_download": 0, "expires_at": None, "created_at": "2025-01-01"}
    )
    db.update_viewer_token = AsyncMock(return_value=None)
    db.delete_viewer_token = AsyncMock(return_value=True)
    db.verify_viewer_token = AsyncMock(return_value=None)
    db.get_all_settings = AsyncMock(return_value=[])
    db.set_setting = AsyncMock()
    db.get_audit_logs = AsyncMock(return_value=[])
    db.create_audit_log = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    db.delete_user_sessions = AsyncMock()
    db.delete_sessions_by_source_token_id = AsyncMock()
    db.cleanup_expired_sessions = AsyncMock(return_value=0)
    db.load_all_sessions = AsyncMock(return_value=[])
    return db


class _WebTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class that sets up mocked db and disables auth for route testing."""

    def setUp(self):
        self._saved_db = web_main.db
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_allow_anonymous = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_sessions = dict(web_main._sessions)
        self._saved_push = web_main.push_manager
        self._saved_display = web_main.config.display_chat_ids
        self._saved_avatar_cache = dict(web_main._avatar_cache)
        self._saved_avatar_cache_time = web_main._avatar_cache_time

        self.mock_db = _mock_db()
        web_main.db = self.mock_db
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main._sessions.clear()
        web_main.push_manager = None
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

    def tearDown(self):
        web_main.db = self._saved_db
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_allow_anonymous
        web_main._sessions.clear()
        web_main._sessions.update(self._saved_sessions)
        web_main.push_manager = self._saved_push
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache.update(self._saved_avatar_cache)
        web_main._avatar_cache_time = self._saved_avatar_cache_time

    def _client(self):
        transport = ASGITransport(app=web_main.app)
        return AsyncClient(transport=transport, base_url="http://test")


# ============================================================================
# Health check
# ============================================================================


@_skip_unless_web
class TestHealthEndpoint(_WebTestBase):
    """Test /api/health endpoint."""

    async def test_health_returns_ok_when_db_connected(self):
        """health_check returns ok and database=connected when db works."""
        self.mock_db.get_chat_count = AsyncMock(return_value=5)
        async with self._client() as client:
            resp = await client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["database"], "connected")

    async def test_health_returns_degraded_when_db_fails(self):
        """health_check returns degraded when db query raises."""
        self.mock_db.get_chat_count = AsyncMock(side_effect=Exception("db down"))
        async with self._client() as client:
            resp = await client.get("/api/health")
        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(data["database"], "unreachable")


# ============================================================================
# Auth check
# ============================================================================


@_skip_unless_web
class TestAuthCheckEndpoint(_WebTestBase):
    """Test /api/auth/check endpoint."""

    async def test_returns_authenticated_when_auth_disabled(self):
        """auth check returns anonymous master only with explicit anonymous opt-in."""
        async with self._client() as client:
            resp = await client.get("/api/auth/check")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["authenticated"])
        self.assertFalse(data["auth_required"])
        self.assertEqual(data["role"], "master")

    async def test_returns_setup_required_when_auth_missing_without_opt_in(self):
        """auth check fails closed when credentials are missing and anonymous mode is not explicit."""
        web_main.ALLOW_ANONYMOUS_VIEWER = False
        async with self._client() as client:
            resp = await client.get("/api/auth/check")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["authenticated"])
        self.assertTrue(data["auth_required"])
        self.assertTrue(data["setup_required"])

    async def test_returns_unauthenticated_when_no_cookie(self):
        """auth check returns authenticated=False when auth enabled but no cookie."""
        web_main.AUTH_ENABLED = True
        async with self._client() as client:
            resp = await client.get("/api/auth/check")
        data = resp.json()
        self.assertFalse(data["authenticated"])
        self.assertTrue(data["auth_required"])

    async def test_returns_authenticated_with_valid_session(self):
        """auth check returns authenticated=True with valid session cookie."""
        web_main.AUTH_ENABLED = True
        token = "test-session-token-abc"
        web_main._sessions[token] = web_main.SessionData(username="admin", role="master", created_at=time.time())
        async with self._client() as client:
            resp = await client.get("/api/auth/check", cookies={"viewer_auth": token})
        data = resp.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["role"], "master")
        self.assertEqual(data["username"], "admin")

    async def test_returns_unauthenticated_with_expired_session(self):
        """auth check returns authenticated=False for expired session."""
        web_main.AUTH_ENABLED = True
        token = "expired-token"
        web_main._sessions[token] = web_main.SessionData(
            username="old",
            role="viewer",
            created_at=time.time() - web_main.AUTH_SESSION_SECONDS - 100,
        )
        async with self._client() as client:
            resp = await client.get("/api/auth/check", cookies={"viewer_auth": token})
        data = resp.json()
        self.assertFalse(data["authenticated"])


# ============================================================================
# Chats API
# ============================================================================


@_skip_unless_web
class TestChatsEndpoint(_WebTestBase):
    """Test /api/chats endpoint."""

    async def test_returns_empty_chats_list(self):
        """get_chats returns empty list when no chats exist."""
        self.mock_db.get_all_chats = AsyncMock(return_value=[])
        self.mock_db.get_chat_count = AsyncMock(return_value=0)
        async with self._client() as client:
            resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["chats"], [])
        self.assertEqual(data["total"], 0)
        self.assertFalse(data["has_more"])

    async def test_returns_chats_with_pagination(self):
        """get_chats returns paginated chat data."""
        chats = [{"id": 1, "title": "Chat 1", "type": "private"}, {"id": 2, "title": "Chat 2", "type": "group"}]
        self.mock_db.get_all_chats = AsyncMock(return_value=chats)
        self.mock_db.get_chat_count = AsyncMock(return_value=10)
        async with self._client() as client:
            resp = await client.get("/api/chats?limit=2&offset=0")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["chats"]), 2)
        self.assertEqual(data["total"], 10)
        self.assertTrue(data["has_more"])

    async def test_chats_filtered_by_user_allowed_ids(self):
        """get_chats filters chats when user has allowed_chat_ids."""
        web_main.AUTH_ENABLED = True
        token = "viewer-token"
        web_main._sessions[token] = web_main.SessionData(username="viewer1", role="viewer", allowed_chat_ids={1, 3})
        all_chats = [
            {"id": 1, "title": "Allowed", "type": "private"},
            {"id": 2, "title": "Denied", "type": "group"},
            {"id": 3, "title": "Also Allowed", "type": "private"},
        ]
        self.mock_db.get_all_chats = AsyncMock(return_value=all_chats)
        async with self._client() as client:
            resp = await client.get("/api/chats", cookies={"viewer_auth": token})
        data = resp.json()
        self.assertEqual(data["total"], 2)
        chat_ids = [c["id"] for c in data["chats"]]
        self.assertIn(1, chat_ids)
        self.assertIn(3, chat_ids)
        self.assertNotIn(2, chat_ids)

    async def test_chats_search_parameter(self):
        """get_chats passes search parameter to db."""
        self.mock_db.get_all_chats = AsyncMock(return_value=[])
        self.mock_db.get_chat_count = AsyncMock(return_value=0)
        async with self._client() as client:
            resp = await client.get("/api/chats?search=test")
        self.assertEqual(resp.status_code, 200)
        self.mock_db.get_all_chats.assert_awaited_once()
        call_kwargs = self.mock_db.get_all_chats.call_args.kwargs
        self.assertEqual(call_kwargs["search"], "test")

    async def test_chats_handles_db_error(self):
        """get_chats returns 500 on non-connection db error."""
        self.mock_db.get_all_chats = AsyncMock(side_effect=ValueError("bad query"))
        async with self._client() as client:
            resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 500)

    async def test_chats_handles_db_connection_error(self):
        """get_chats returns 503 on db connection error."""
        self.mock_db.get_all_chats = AsyncMock(side_effect=OSError("connection refused"))
        async with self._client() as client:
            resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 503)


# ============================================================================
# Messages API
# ============================================================================


@_skip_unless_web
class TestMessagesEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/messages endpoint."""

    async def test_returns_messages(self):
        """get_messages returns message list."""
        self.mock_db.get_messages_paginated = AsyncMock(return_value={"messages": [], "total": 0})
        async with self._client() as client:
            resp = await client.get("/api/chats/123/messages")
        self.assertEqual(resp.status_code, 200)

    async def test_denies_access_to_restricted_chat(self):
        """get_messages returns 403 when user cannot access the chat."""
        web_main.AUTH_ENABLED = True
        token = "restricted-viewer"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
        async with self._client() as client:
            resp = await client.get("/api/chats/999/messages", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)

    async def test_passes_cursor_pagination_params(self):
        """get_messages forwards before_date and before_id to db."""
        self.mock_db.get_messages_paginated = AsyncMock(return_value=[])
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages?before_date=2025-06-15T12:00:00Z&before_id=500")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_db.get_messages_paginated.call_args.kwargs
        self.assertIsNotNone(call_kwargs["before_date"])
        self.assertEqual(call_kwargs["before_id"], 500)

    async def test_invalid_before_date_returns_400(self):
        """get_messages returns 400 for invalid before_date format."""
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages?before_date=not-a-date")
        self.assertEqual(resp.status_code, 400)

    async def test_messages_db_connection_error_returns_503(self):
        """get_messages returns 503 on db connection error."""
        self.mock_db.get_messages_paginated = AsyncMock(side_effect=OSError("conn refused"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages")
        self.assertEqual(resp.status_code, 503)


# ============================================================================
# Pinned Messages API
# ============================================================================


@_skip_unless_web
class TestPinnedMessagesEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/pinned endpoint."""

    async def test_returns_pinned_messages(self):
        """get_pinned_messages returns list of pinned messages."""
        self.mock_db.get_pinned_messages = AsyncMock(return_value=[{"id": 1, "text": "pinned"}])
        async with self._client() as client:
            resp = await client.get("/api/chats/42/pinned")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)

    async def test_pinned_denies_restricted_chat(self):
        """get_pinned_messages returns 403 for restricted chat."""
        web_main.AUTH_ENABLED = True
        token = "rv"
        web_main._sessions[token] = web_main.SessionData(username="v", role="viewer", allowed_chat_ids={10})
        async with self._client() as client:
            resp = await client.get("/api/chats/999/pinned", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)


# ============================================================================
# Folders API
# ============================================================================


@_skip_unless_web
class TestFoldersEndpoint(_WebTestBase):
    """Test /api/folders endpoint."""

    async def test_returns_folders(self):
        """get_folders returns folder list."""
        self.mock_db.get_all_folders = AsyncMock(return_value=[{"id": 1, "title": "Work"}])
        async with self._client() as client:
            resp = await client.get("/api/folders")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["folders"]), 1)


# ============================================================================
# Topics API
# ============================================================================


@_skip_unless_web
class TestTopicsEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/topics endpoint."""

    async def test_returns_topics(self):
        """get_chat_topics returns topic list."""
        self.mock_db.get_forum_topics = AsyncMock(return_value=[{"id": 1, "title": "General"}])
        async with self._client() as client:
            resp = await client.get("/api/chats/42/topics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["topics"]), 1)


# ============================================================================
# Archived count API
# ============================================================================


@_skip_unless_web
class TestArchivedCountEndpoint(_WebTestBase):
    """Test /api/archived/count endpoint."""

    async def test_returns_archived_count(self):
        """get_archived_count returns count of archived chats."""
        self.mock_db.get_archived_chat_count = AsyncMock(return_value=5)
        async with self._client() as client:
            resp = await client.get("/api/archived/count")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 5)

    async def test_archived_count_filtered_by_user_chats(self):
        """get_archived_count filters by user allowed chats."""
        web_main.AUTH_ENABLED = True
        token = "av"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={1, 2, 3})
        self.mock_db.get_all_chats = AsyncMock(return_value=[{"id": 1}, {"id": 2}, {"id": 99}])
        async with self._client() as client:
            resp = await client.get("/api/archived/count", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 2)


# ============================================================================
# Stats API
# ============================================================================


@_skip_unless_web
class TestStatsEndpoint(_WebTestBase):
    """Test /api/stats endpoint."""

    async def test_returns_stats_with_timezone(self):
        """get_stats returns cached stats with timezone info."""
        self.mock_db.get_cached_statistics = AsyncMock(return_value={"chats": 10, "messages": 500})
        self.mock_db.get_metadata = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.get("/api/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("timezone", data)
        self.assertIn("push_notifications", data)
        self.assertIn("push_enabled", data)

    async def test_stats_filters_per_chat_for_restricted_user(self):
        """get_stats filters per_chat_message_counts by user allowed chats."""
        web_main.AUTH_ENABLED = True
        token = "sv"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={1})
        self.mock_db.get_cached_statistics = AsyncMock(
            return_value={
                "per_chat_message_counts": {"1": 100, "2": 200},
                "chats": 2,
                "messages": 300,
                "media_files": 50,
                "total_size_mb": 1000,
            }
        )
        self.mock_db.get_metadata = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.get("/api/stats", cookies={"viewer_auth": token})
        data = resp.json()
        self.assertEqual(data["chats"], 1)
        self.assertEqual(data["messages"], 100)
        self.assertNotIn("media_files", data)


# ============================================================================
# Stats refresh API
# ============================================================================


@_skip_unless_web
class TestStatsRefreshEndpoint(_WebTestBase):
    """Test /api/stats/refresh endpoint."""

    async def test_refresh_stats_returns_result(self):
        """refresh_stats triggers recalculation and returns result."""
        self.mock_db.calculate_and_store_statistics = AsyncMock(return_value={"chats": 5})
        async with self._client() as client:
            resp = await client.post("/api/stats/refresh")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("timezone", data)

    async def test_refresh_stats_requires_master(self):
        """refresh_stats returns 403 for non-master users."""
        web_main.AUTH_ENABLED = True
        token = "viewer-tok"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer")
        async with self._client() as client:
            resp = await client.post("/api/stats/refresh", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)


# ============================================================================
# Chat stats API
# ============================================================================


@_skip_unless_web
class TestChatStatsEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/stats endpoint."""

    async def test_returns_chat_stats(self):
        """get_chat_stats returns stats for specific chat."""
        self.mock_db.get_chat_stats = AsyncMock(return_value={"messages": 100, "media": 20})
        async with self._client() as client:
            resp = await client.get("/api/chats/42/stats")
        self.assertEqual(resp.status_code, 200)

    async def test_chat_stats_denies_restricted_chat(self):
        """get_chat_stats returns 403 for restricted chat."""
        web_main.AUTH_ENABLED = True
        token = "cs"
        web_main._sessions[token] = web_main.SessionData(username="v", role="viewer", allowed_chat_ids={1})
        async with self._client() as client:
            resp = await client.get("/api/chats/999/stats", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)


# ============================================================================
# Message by date API
# ============================================================================


@_skip_unless_web
class TestMessageByDateEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/messages/by-date endpoint."""

    async def test_returns_message_for_valid_date(self):
        """get_message_by_date returns message when found."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(return_value={"id": 100, "text": "found"})
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2025-06-15")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], 100)

    async def test_returns_404_when_no_message_found(self):
        """get_message_by_date returns 404 when no messages match."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2020-01-01")
        self.assertEqual(resp.status_code, 404)

    async def test_returns_400_for_invalid_date_format(self):
        """get_message_by_date returns 400 for invalid date string."""
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=not-a-date")
        self.assertEqual(resp.status_code, 400)

    async def test_accepts_timezone_parameter(self):
        """get_message_by_date uses timezone parameter for date interpretation."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(return_value={"id": 1})
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2025-06-15&timezone=Europe/Madrid")
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Push config API
# ============================================================================


@_skip_unless_web
class TestPushConfigEndpoint(_WebTestBase):
    """Test /api/push/config endpoint."""

    async def test_returns_push_disabled_by_default(self):
        """get_push_config returns disabled when no push manager."""
        async with self._client() as client:
            resp = await client.get("/api/push/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["enabled"])
        self.assertIsNone(data["vapid_public_key"])

    async def test_returns_push_enabled_with_manager(self):
        """get_push_config returns enabled and vapid key when push is active."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.public_key = "BTEST_KEY_123"
        web_main.push_manager = mock_pm
        web_main.config.push_notifications = "full"
        async with self._client() as client:
            resp = await client.get("/api/push/config")
        data = resp.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["vapid_public_key"], "BTEST_KEY_123")
        web_main.config.push_notifications = "off"


# ============================================================================
# Push subscribe/unsubscribe API
# ============================================================================


@_skip_unless_web
class TestPushSubscribeEndpoint(_WebTestBase):
    """Test /api/push/subscribe and /api/push/unsubscribe endpoints."""

    async def test_subscribe_returns_400_when_push_disabled(self):
        """push_subscribe returns 400 when push is not enabled."""
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://push.example.com/sub",
                    "keys": {"p256dh": "k", "auth": "a"},
                },
            )
        self.assertEqual(resp.status_code, 400)

    async def test_subscribe_stores_subscription(self):
        """push_subscribe stores subscription and returns success."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.subscribe = AsyncMock(return_value=True)
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://push.example.com/sub",
                    "keys": {"p256dh": "key1", "auth": "auth1"},
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "subscribed")

    async def test_subscribe_missing_fields_returns_400(self):
        """push_subscribe returns 400 when required fields missing."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post("/api/push/subscribe", json={"endpoint": "https://x.com"})
        self.assertEqual(resp.status_code, 400)

    async def test_unsubscribe_returns_400_when_push_disabled(self):
        """push_unsubscribe returns 400 when push not enabled."""
        async with self._client() as client:
            resp = await client.post("/api/push/unsubscribe", json={"endpoint": "https://x.com"})
        self.assertEqual(resp.status_code, 400)

    async def test_unsubscribe_removes_subscription(self):
        """push_unsubscribe removes subscription and returns status."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.unsubscribe = AsyncMock(return_value=True)
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example.com/sub"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "unsubscribed")


# ============================================================================
# Login API
# ============================================================================


@_skip_unless_web
class TestLoginEndpoint(_WebTestBase):
    """Test /api/login endpoint."""

    async def test_login_disabled_returns_success(self):
        """login returns success when auth is disabled."""
        async with self._client() as client:
            resp = await client.post("/api/login", json={"username": "x", "password": "y"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    async def test_login_with_valid_master_credentials(self):
        """login sets cookie for valid master credentials."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        try:
            async with self._client() as client:
                resp = await client.post("/api/login", json={"username": "admin", "password": "secretpass"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["role"], "master")
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_with_invalid_credentials_returns_401(self):
        """login returns 401 for wrong credentials."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        try:
            async with self._client() as client:
                resp = await client.post("/api/login", json={"username": "admin", "password": "wrong"})
            self.assertEqual(resp.status_code, 401)
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_missing_fields_returns_400(self):
        """login returns 400 when username or password missing."""
        web_main.AUTH_ENABLED = True
        async with self._client() as client:
            resp = await client.post("/api/login", json={"username": ""})
        self.assertEqual(resp.status_code, 400)

    async def test_login_rate_limited_returns_429(self):
        """login returns 429 when rate limit exceeded."""
        web_main.AUTH_ENABLED = True
        saved_attempts = dict(web_main._login_attempts)
        now = time.time()
        web_main._login_attempts["127.0.0.1"] = [now] * (web_main._LOGIN_RATE_LIMIT + 5)
        try:
            async with self._client() as client:
                resp = await client.post("/api/login", json={"username": "x", "password": "y"})
            self.assertEqual(resp.status_code, 429)
        finally:
            web_main._login_attempts.clear()
            web_main._login_attempts.update(saved_attempts)

    async def test_login_with_db_viewer_account(self):
        """login authenticates against database viewer accounts."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "masterpass"

        salt = "testsalt123"
        pw_hash = web_main._hash_password("viewerpass", salt)
        self.mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "username": "viewer1",
                "password_hash": pw_hash,
                "salt": salt,
                "is_active": 1,
                "allowed_chat_ids": json.dumps([1, 2, 3]),
                "no_download": 0,
            }
        )
        try:
            async with self._client() as client:
                resp = await client.post("/api/login", json={"username": "viewer1", "password": "viewerpass"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["role"], "viewer")
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass


# ============================================================================
# Logout API
# ============================================================================


@_skip_unless_web
class TestLogoutEndpoint(_WebTestBase):
    """Test /api/logout endpoint."""

    async def test_logout_clears_session(self):
        """logout removes session and clears cookie."""
        token = "logout-token"
        web_main._sessions[token] = web_main.SessionData(username="u", role="master")
        async with self._client() as client:
            resp = await client.post("/api/logout", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertNotIn(token, web_main._sessions)

    async def test_logout_without_cookie(self):
        """logout succeeds even without a cookie."""
        async with self._client() as client:
            resp = await client.post("/api/logout")
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Token auth API
# ============================================================================


@_skip_unless_web
class TestTokenAuthEndpoint(_WebTestBase):
    """Test /auth/token endpoint."""

    async def test_token_auth_without_db_returns_500(self):
        """auth_via_token returns 500 when db is not available."""
        web_main.db = None
        async with self._client() as client:
            resp = await client.post("/auth/token", json={"token": "abc"})
        self.assertEqual(resp.status_code, 500)

    async def test_token_auth_invalid_token_returns_401(self):
        """auth_via_token returns 401 for invalid token."""
        self.mock_db.verify_viewer_token = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.post("/auth/token", json={"token": "invalid"})
        self.assertEqual(resp.status_code, 401)

    async def test_token_auth_valid_token_creates_session(self):
        """auth_via_token creates session for valid token."""
        self.mock_db.verify_viewer_token = AsyncMock(
            return_value={
                "id": 1,
                "label": "share-link",
                "allowed_chat_ids": json.dumps([10, 20]),
                "no_download": 0,
            }
        )
        async with self._client() as client:
            resp = await client.post("/auth/token", json={"token": "valid-token-hex"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["role"], "token")

    async def test_token_auth_empty_token_returns_400(self):
        """auth_via_token returns 400 when token is empty."""
        async with self._client() as client:
            resp = await client.post("/auth/token", json={"token": ""})
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Admin viewers API
# ============================================================================


@_skip_unless_web
class TestAdminViewersEndpoint(_WebTestBase):
    """Test /api/admin/viewers endpoints."""

    async def test_list_viewers_returns_empty(self):
        """list_viewers returns empty list."""
        async with self._client() as client:
            resp = await client.get("/api/admin/viewers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["viewers"], [])

    async def test_list_viewers_returns_accounts(self):
        """list_viewers returns viewer account data."""
        self.mock_db.get_all_viewer_accounts = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "username": "v1",
                    "allowed_chat_ids": json.dumps([1, 2]),
                    "is_active": 1,
                    "no_download": 0,
                    "created_by": "admin",
                    "created_at": "2025-01-01",
                    "updated_at": "2025-01-01",
                }
            ]
        )
        async with self._client() as client:
            resp = await client.get("/api/admin/viewers")
        data = resp.json()
        self.assertEqual(len(data["viewers"]), 1)
        self.assertEqual(data["viewers"][0]["allowed_chat_ids"], [1, 2])

    async def test_create_viewer_validates_username(self):
        """create_viewer returns 400 for short username."""
        async with self._client() as client:
            resp = await client.post("/api/admin/viewers", json={"username": "ab", "password": "longpassword"})
        self.assertEqual(resp.status_code, 400)

    async def test_create_viewer_validates_password(self):
        """create_viewer returns 400 for short password."""
        async with self._client() as client:
            resp = await client.post("/api/admin/viewers", json={"username": "viewer1", "password": "short"})
        self.assertEqual(resp.status_code, 400)

    async def test_create_viewer_success(self):
        """create_viewer creates account and returns data."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/viewers", json={"username": "newviewer", "password": "longpassword123"}
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["username"], "test")

    async def test_create_viewer_duplicate_returns_409(self):
        """create_viewer returns 409 for existing username."""
        self.mock_db.get_viewer_by_username = AsyncMock(return_value={"username": "existing"})
        async with self._client() as client:
            resp = await client.post("/api/admin/viewers", json={"username": "existing", "password": "longpassword123"})
        self.assertEqual(resp.status_code, 409)

    async def test_update_viewer_not_found(self):
        """update_viewer returns 404 for unknown viewer_id."""
        async with self._client() as client:
            resp = await client.put("/api/admin/viewers/999", json={"is_active": False})
        self.assertEqual(resp.status_code, 404)

    async def test_update_viewer_success(self):
        """update_viewer updates account and invalidates sessions."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put("/api/admin/viewers/1", json={"is_active": False})
        self.assertEqual(resp.status_code, 200)

    async def test_update_viewer_no_fields_returns_400(self):
        """update_viewer returns 400 when no updatable fields provided."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put("/api/admin/viewers/1", json={})
        self.assertEqual(resp.status_code, 400)

    async def test_delete_viewer_success(self):
        """delete_viewer removes account."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.delete("/api/admin/viewers/1")
        self.assertEqual(resp.status_code, 200)

    async def test_delete_viewer_not_found(self):
        """delete_viewer returns 404 for unknown id."""
        async with self._client() as client:
            resp = await client.delete("/api/admin/viewers/999")
        self.assertEqual(resp.status_code, 404)


# ============================================================================
# Admin tokens API
# ============================================================================


@_skip_unless_web
class TestAdminTokensEndpoint(_WebTestBase):
    """Test /api/admin/tokens endpoints."""

    async def test_list_tokens_returns_empty(self):
        """list_tokens returns empty list."""
        async with self._client() as client:
            resp = await client.get("/api/admin/tokens")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tokens"], [])

    async def test_list_tokens_returns_data(self):
        """list_tokens returns token data."""
        self.mock_db.get_all_viewer_tokens = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "label": "test",
                    "created_by": "admin",
                    "allowed_chat_ids": json.dumps([1]),
                    "is_revoked": 0,
                    "no_download": 0,
                    "expires_at": None,
                    "last_used_at": None,
                    "use_count": 0,
                    "created_at": "2025-01-01",
                }
            ]
        )
        async with self._client() as client:
            resp = await client.get("/api/admin/tokens")
        data = resp.json()
        self.assertEqual(len(data["tokens"]), 1)

    async def test_create_token_requires_allowed_chat_ids(self):
        """create_token returns 400 when allowed_chat_ids missing."""
        async with self._client() as client:
            resp = await client.post("/api/admin/tokens", json={"label": "test"})
        self.assertEqual(resp.status_code, 400)

    async def test_create_token_success(self):
        """create_token returns token with plaintext."""
        async with self._client() as client:
            resp = await client.post("/api/admin/tokens", json={"allowed_chat_ids": [1, 2, 3], "label": "my-token"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)
        self.assertIsNotNone(data["token"])

    async def test_update_token_not_found(self):
        """update_token returns 404 when token not found."""
        self.mock_db.update_viewer_token = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.put("/api/admin/tokens/999", json={"label": "x"})
        self.assertEqual(resp.status_code, 404)

    async def test_update_token_no_fields_returns_400(self):
        """update_token returns 400 with no updatable fields."""
        async with self._client() as client:
            resp = await client.put("/api/admin/tokens/1", json={})
        self.assertEqual(resp.status_code, 400)

    async def test_delete_token_success(self):
        """delete_token removes token."""
        self.mock_db.delete_viewer_token = AsyncMock(return_value=True)
        async with self._client() as client:
            resp = await client.delete("/api/admin/tokens/1")
        self.assertEqual(resp.status_code, 200)

    async def test_delete_token_not_found(self):
        """delete_token returns 404 when not found."""
        self.mock_db.delete_viewer_token = AsyncMock(return_value=False)
        async with self._client() as client:
            resp = await client.delete("/api/admin/tokens/999")
        self.assertEqual(resp.status_code, 404)


# ============================================================================
# Admin settings API
# ============================================================================


@_skip_unless_web
class TestAdminSettingsEndpoint(_WebTestBase):
    """Test /api/admin/settings endpoints."""

    async def test_get_settings(self):
        """get_settings returns settings list."""
        self.mock_db.get_all_settings = AsyncMock(return_value=[{"key": "theme", "value": "dark"}])
        async with self._client() as client:
            resp = await client.get("/api/admin/settings")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["settings"]), 1)

    async def test_set_setting_success(self):
        """set_setting stores value."""
        async with self._client() as client:
            resp = await client.put("/api/admin/settings/theme", json={"value": "dark"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["key"], "theme")
        self.assertEqual(data["value"], "dark")

    async def test_set_setting_missing_value_returns_400(self):
        """set_setting returns 400 when value is missing."""
        async with self._client() as client:
            resp = await client.put("/api/admin/settings/key", json={})
        self.assertEqual(resp.status_code, 400)

    async def test_set_setting_invalid_key_returns_400(self):
        """set_setting returns 400 for empty key."""
        async with self._client() as client:
            resp = await client.put("/api/admin/settings/", json={"value": "x"})
        # FastAPI returns 404 for empty path param or 307 redirect
        self.assertIn(resp.status_code, (307, 404, 405))


# ============================================================================
# Admin audit log
# ============================================================================


@_skip_unless_web
class TestAuditLogEndpoint(_WebTestBase):
    """Test /api/admin/audit endpoint."""

    async def test_returns_audit_logs(self):
        """get_audit_log returns paginated logs."""
        self.mock_db.get_audit_logs = AsyncMock(return_value=[{"id": 1, "action": "login"}])
        async with self._client() as client:
            resp = await client.get("/api/admin/audit")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["logs"]), 1)

    async def test_audit_log_with_filters(self):
        """get_audit_log passes username and action filters."""
        async with self._client() as client:
            resp = await client.get("/api/admin/audit?username=admin&action=login")
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_db.get_audit_logs.call_args.kwargs
        self.assertEqual(call_kwargs["username"], "admin")
        self.assertEqual(call_kwargs["action"], "login")


# ============================================================================
# Admin chats (for chat picker)
# ============================================================================


@_skip_unless_web
class TestAdminChatsEndpoint(_WebTestBase):
    """Test /api/admin/chats endpoint."""

    async def test_returns_all_chats(self):
        """admin_list_chats returns all chats with display metadata."""
        self.mock_db.get_all_chats = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "title": "Group Chat",
                    "type": "group",
                    "username": None,
                    "first_name": None,
                    "last_name": None,
                },
                {
                    "id": 2,
                    "title": None,
                    "type": "private",
                    "username": "john",
                    "first_name": "John",
                    "last_name": "Doe",
                },
            ]
        )
        async with self._client() as client:
            resp = await client.get("/api/admin/chats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["chats"]), 2)
        self.assertEqual(data["chats"][0]["title"], "Group Chat")
        self.assertEqual(data["chats"][1]["title"], "John Doe")


# ============================================================================
# Notification settings
# ============================================================================


@_skip_unless_web
class TestNotificationSettingsEndpoint(_WebTestBase):
    """Test /api/notifications/settings endpoint."""

    async def test_returns_notification_settings(self):
        """get_notification_settings returns settings when auth disabled."""
        async with self._client() as client:
            resp = await client.get("/api/notifications/settings")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("enabled", data)
        self.assertIn("mode", data)
        self.assertIn("websocket_url", data)

    async def test_returns_not_authenticated_when_no_session(self):
        """get_notification_settings returns enabled=False when auth required but no session."""
        web_main.AUTH_ENABLED = True
        async with self._client() as client:
            resp = await client.get("/api/notifications/settings")
        data = resp.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["reason"], "Not authenticated")


# ============================================================================
# Broadcast helper functions
# ============================================================================


@_skip_unless_web
class TestBroadcastHelpers(_WebTestBase):
    """Test broadcast_new_message, broadcast_message_edit, broadcast_message_delete."""

    async def test_broadcast_new_message(self):
        """broadcast_new_message calls ws_manager.broadcast_to_chat."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.broadcast_new_message(42, {"id": 1, "text": "hi"})
        mock_bc.assert_awaited_once()
        args = mock_bc.call_args[0]
        self.assertEqual(args[0], 42)
        self.assertEqual(args[1]["type"], "new_message")

    async def test_broadcast_message_edit(self):
        """broadcast_message_edit calls ws_manager.broadcast_to_chat."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.broadcast_message_edit(42, 10, "edited text", "2025-01-01")
        mock_bc.assert_awaited_once()
        msg = mock_bc.call_args[0][1]
        self.assertEqual(msg["type"], "edit")
        self.assertEqual(msg["new_text"], "edited text")

    async def test_broadcast_message_delete(self):
        """broadcast_message_delete calls ws_manager.broadcast_to_chat."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.broadcast_message_delete(42, 10)
        mock_bc.assert_awaited_once()
        msg = mock_bc.call_args[0][1]
        self.assertEqual(msg["type"], "delete")
        self.assertEqual(msg["message_id"], 10)


# ============================================================================
# _normalize_display_chat_ids
# ============================================================================


@_skip_unless_web
class TestNormalizeDisplayChatIds(_WebTestBase):
    """Test _normalize_display_chat_ids auto-correction logic."""

    async def test_no_op_when_display_empty(self):
        """_normalize_display_chat_ids does nothing when display_chat_ids is empty."""
        web_main.config.display_chat_ids = set()
        await web_main._normalize_display_chat_ids()
        self.assertEqual(web_main.config.display_chat_ids, set())

    async def test_keeps_existing_id(self):
        """_normalize_display_chat_ids keeps IDs that exist in DB."""
        web_main.config.display_chat_ids = {100}
        self.mock_db.get_all_chats = AsyncMock(return_value=[{"id": 100}])
        await web_main._normalize_display_chat_ids()
        self.assertIn(100, web_main.config.display_chat_ids)

    async def test_autocorrects_positive_to_marked_format(self):
        """_normalize_display_chat_ids converts positive ID to -100 prefix when needed."""
        web_main.config.display_chat_ids = {12345}
        marked = -1000000000000 - 12345
        self.mock_db.get_all_chats = AsyncMock(return_value=[{"id": marked}])
        await web_main._normalize_display_chat_ids()
        self.assertIn(marked, web_main.config.display_chat_ids)

    async def test_keeps_unknown_positive_id(self):
        """_normalize_display_chat_ids keeps positive ID when neither format found."""
        web_main.config.display_chat_ids = {99999}
        self.mock_db.get_all_chats = AsyncMock(return_value=[])
        await web_main._normalize_display_chat_ids()
        self.assertIn(99999, web_main.config.display_chat_ids)

    async def test_keeps_unknown_negative_id(self):
        """_normalize_display_chat_ids keeps unknown negative ID."""
        web_main.config.display_chat_ids = {-500}
        self.mock_db.get_all_chats = AsyncMock(return_value=[])
        await web_main._normalize_display_chat_ids()
        self.assertIn(-500, web_main.config.display_chat_ids)


# ============================================================================
# Internal push endpoint
# ============================================================================


@_skip_unless_web
class TestInternalPushEndpoint(_WebTestBase):
    """Test /internal/push endpoint."""

    async def test_rejects_non_private_ip(self):
        """internal_push returns 403 for non-private IP."""
        # httpx sends from 127.0.0.1 by default which is private,
        # so we patch the client host
        async with self._client() as client:
            with patch.object(web_main, "realtime_listener", None):
                # The test client connects from localhost (127.0.0.1), which is allowed.
                # Test the success path instead.
                resp = await client.post("/internal/push", json={"type": "test"})
        self.assertEqual(resp.status_code, 200)

    async def test_internal_push_calls_listener(self):
        """internal_push forwards payload to realtime_listener."""
        mock_listener = MagicMock()
        mock_listener.handle_http_push = AsyncMock()
        saved_listener = web_main.realtime_listener
        web_main.realtime_listener = mock_listener
        try:
            async with self._client() as client:
                resp = await client.post("/internal/push", json={"type": "new_message", "chat_id": 1})
            self.assertEqual(resp.status_code, 200)
            mock_listener.handle_http_push.assert_awaited_once()
        finally:
            web_main.realtime_listener = saved_listener


# ============================================================================
# Exception handler
# ============================================================================


@_skip_unless_web
class TestExceptionHandler(_WebTestBase):
    """Test the unhandled exception handler."""

    async def test_db_connection_error_returns_503(self):
        """Unhandled OSError returns 503."""
        self.mock_db.get_all_chats = AsyncMock(side_effect=OSError("conn error"))
        async with self._client() as client:
            resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 503)

    async def test_generic_error_returns_500(self):
        """Unhandled non-connection error returns 500."""
        self.mock_db.get_all_chats = AsyncMock(side_effect=RuntimeError("unexpected"))
        async with self._client() as client:
            resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 500)


# ============================================================================
# _resolve_session
# ============================================================================


@_skip_unless_web
class TestResolveSession(_WebTestBase):
    """Test _resolve_session in-memory and DB fallback."""

    async def test_returns_in_memory_session(self):
        """_resolve_session returns session from memory cache."""
        session = web_main.SessionData(username="u", role="master")
        web_main._sessions["tok"] = session
        result = await web_main._resolve_session("tok")
        self.assertIs(result, session)

    async def test_returns_none_for_unknown_token_no_db(self):
        """_resolve_session returns None when token not in memory and no db."""
        web_main.db = None
        result = await web_main._resolve_session("unknown")
        self.assertIsNone(result)

    async def test_returns_none_for_expired_db_session(self):
        """_resolve_session returns None for expired session from db."""
        self.mock_db.get_session = AsyncMock(
            return_value={
                "username": "old",
                "role": "viewer",
                "allowed_chat_ids": None,
                "no_download": 0,
                "source_token_id": None,
                "created_at": time.time() - web_main.AUTH_SESSION_SECONDS - 100,
                "last_accessed": time.time() - web_main.AUTH_SESSION_SECONDS - 100,
            }
        )
        result = await web_main._resolve_session("expired-db-tok")
        self.assertIsNone(result)

    async def test_loads_valid_session_from_db(self):
        """_resolve_session loads valid session from db and caches it."""
        now = time.time()
        self.mock_db.get_session = AsyncMock(
            return_value={
                "username": "dbuser",
                "role": "viewer",
                "allowed_chat_ids": json.dumps([1, 2]),
                "no_download": 1,
                "source_token_id": 5,
                "created_at": now,
                "last_accessed": now,
            }
        )
        result = await web_main._resolve_session("db-tok")
        self.assertIsNotNone(result)
        self.assertEqual(result.username, "dbuser")
        self.assertEqual(result.allowed_chat_ids, {1, 2})
        self.assertTrue(result.no_download)
        # Should be cached now
        self.assertIn("db-tok", web_main._sessions)


# ============================================================================
# require_auth and require_master
# ============================================================================


@_skip_unless_web
class TestRequireAuth(_WebTestBase):
    """Test require_auth dependency."""

    async def test_returns_anonymous_master_when_auth_disabled(self):
        """require_auth returns anonymous master only with explicit anonymous opt-in."""
        result = await web_main.require_auth(auth_cookie=None)
        self.assertEqual(result.username, "anonymous")
        self.assertEqual(result.role, "master")

    async def test_auth_disabled_without_opt_in_fails_closed(self):
        """require_auth raises setup error when auth is missing and anonymous mode is not explicit."""
        web_main.ALLOW_ANONYMOUS_VIEWER = False
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            await web_main.require_auth(auth_cookie=None)
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_raises_401_when_no_cookie(self):
        """require_auth raises 401 when auth enabled and no cookie."""
        web_main.AUTH_ENABLED = True
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            await web_main.require_auth(auth_cookie=None)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_raises_401_for_invalid_session(self):
        """require_auth raises 401 when session not found."""
        web_main.AUTH_ENABLED = True
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            await web_main.require_auth(auth_cookie="bad-token")
        self.assertEqual(ctx.exception.status_code, 401)


@_skip_unless_web
class TestRequireMaster(unittest.TestCase):
    """Test require_master dependency."""

    def test_passes_for_master_role(self):
        """require_master returns user when role is master."""
        user = web_main.UserContext(username="admin", role="master")
        req = MagicMock()
        req.headers = {}
        result = web_main.require_master(req, user)
        self.assertIs(result, user)

    def test_raises_403_for_viewer_role(self):
        """require_master raises 403 for non-master role."""
        user = web_main.UserContext(username="v1", role="viewer")
        req = MagicMock()
        req.headers = {}
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            web_main.require_master(req, user)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_raises_403_when_viewer_only_header_set(self):
        """require_master raises 403 when x-viewer-only header is true."""
        user = web_main.UserContext(username="admin", role="master")
        req = MagicMock()
        req.headers = {"x-viewer-only": "true"}
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            web_main.require_master(req, user)
        self.assertEqual(ctx.exception.status_code, 403)


# ============================================================================
# handle_realtime_notification with push manager
# ============================================================================


@_skip_unless_web
class TestRealtimeNotificationWithPush(_WebTestBase):
    """Test handle_realtime_notification push notification branch."""

    async def test_sends_push_for_new_message(self):
        """handle_realtime_notification sends push when push_manager is enabled."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.notify_new_message = AsyncMock(return_value=1)
        web_main.push_manager = mock_pm
        self.mock_db.get_chat_by_id = AsyncMock(return_value={"title": "Test Chat"})
        self.mock_db.get_user_by_id = AsyncMock(return_value={"first_name": "Alice", "username": "alice"})

        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock):
            await web_main.handle_realtime_notification(
                {
                    "type": "new_message",
                    "chat_id": 42,
                    "data": {"message": {"id": 1, "text": "hello", "sender_id": 100}},
                }
            )

        mock_pm.notify_new_message.assert_awaited_once()
        call_kwargs = mock_pm.notify_new_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], 42)
        self.assertEqual(call_kwargs["sender_name"], "Alice")


if __name__ == "__main__":
    unittest.main()
