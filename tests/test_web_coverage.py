"""Extended coverage tests for src/web/main.py and src/web/push.py.

Targets the uncovered code paths: background tasks, lifespan, media serving,
export, admin edge cases, login flow branches, WebSocket, internal push auth,
push subscribe/unsubscribe/get_subscriptions with SQLAlchemy sessions.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_cov_"))
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

try:
    import src.web.push as push_mod
    from src.web.push import PushNotificationManager

    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False
    push_mod = None  # type: ignore[assignment]
    PushNotificationManager = None  # type: ignore[assignment, misc]


def _skip_unless_web(cls_or_fn):
    return unittest.skipUnless(_WEB_AVAILABLE and _HTTPX_AVAILABLE, "web_main or httpx import failed")(cls_or_fn)


def _skip_unless_push(cls_or_fn):
    return unittest.skipUnless(_PUSH_AVAILABLE, "push module import failed")(cls_or_fn)


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
        return_value={
            "id": 1,
            "label": "test",
            "no_download": 0,
            "expires_at": None,
            "created_at": "2025-01-01",
        }
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
    db.get_user_by_id = AsyncMock(return_value=None)
    db.get_messages_for_export = None  # Will be set per test
    return db


class _WebTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class that sets up mocked db and disables auth for route testing."""

    def setUp(self):
        if not _WEB_AVAILABLE:
            return
        self._saved_db = web_main.db
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_sessions = dict(web_main._sessions)
        self._saved_push = web_main.push_manager
        self._saved_display = web_main.config.display_chat_ids
        self._saved_avatar_cache = dict(web_main._avatar_cache)
        self._saved_avatar_cache_time = web_main._avatar_cache_time
        self._saved_media_root = web_main._media_root
        self._saved_realtime = web_main.realtime_listener

        self.mock_db = _mock_db()
        web_main.db = self.mock_db
        web_main.AUTH_ENABLED = False
        web_main._sessions.clear()
        web_main.push_manager = None
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

    def tearDown(self):
        if not _WEB_AVAILABLE:
            return
        web_main.db = self._saved_db
        web_main.AUTH_ENABLED = self._saved_auth
        web_main._sessions.clear()
        web_main._sessions.update(self._saved_sessions)
        web_main.push_manager = self._saved_push
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache.update(self._saved_avatar_cache)
        web_main._avatar_cache_time = self._saved_avatar_cache_time
        web_main._media_root = self._saved_media_root
        web_main.realtime_listener = self._saved_realtime

    def _client(self):
        transport = ASGITransport(app=web_main.app)
        return AsyncClient(transport=transport, base_url="http://test")


# ============================================================================
# session_cleanup_task
# ============================================================================


@_skip_unless_web
class TestSessionCleanupTask(_WebTestBase):
    """Test session_cleanup_task background loop."""

    async def test_cleans_expired_sessions(self):
        """session_cleanup_task evicts expired sessions from memory and DB."""
        expired_time = time.time() - web_main.AUTH_SESSION_SECONDS - 100
        web_main._sessions["expired1"] = web_main.SessionData(username="old", role="viewer", created_at=expired_time)
        web_main._sessions["valid1"] = web_main.SessionData(username="current", role="viewer", created_at=time.time())

        with patch.object(web_main, "_SESSION_CLEANUP_INTERVAL", 0):
            task = asyncio.create_task(web_main.session_cleanup_task())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertNotIn("expired1", web_main._sessions)
        self.assertIn("valid1", web_main._sessions)

    async def test_cleans_stale_rate_limit_entries(self):
        """session_cleanup_task evicts stale rate limit entries."""
        saved_attempts = dict(web_main._login_attempts)
        old_time = time.time() - web_main._LOGIN_RATE_WINDOW - 100
        web_main._login_attempts["1.2.3.4"] = [old_time]

        with patch.object(web_main, "_SESSION_CLEANUP_INTERVAL", 0):
            task = asyncio.create_task(web_main.session_cleanup_task())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertNotIn("1.2.3.4", web_main._login_attempts)
        web_main._login_attempts.clear()
        web_main._login_attempts.update(saved_attempts)

    async def test_handles_db_cleanup_failure(self):
        """session_cleanup_task continues when DB cleanup raises."""
        self.mock_db.cleanup_expired_sessions = AsyncMock(side_effect=Exception("db error"))
        expired_time = time.time() - web_main.AUTH_SESSION_SECONDS - 100
        web_main._sessions["exp"] = web_main.SessionData(username="u", role="viewer", created_at=expired_time)

        with patch.object(web_main, "_SESSION_CLEANUP_INTERVAL", 0):
            task = asyncio.create_task(web_main.session_cleanup_task())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Session still removed from memory even if DB fails
        self.assertNotIn("exp", web_main._sessions)


# ============================================================================
# stats_calculation_scheduler
# ============================================================================


@_skip_unless_web
class TestStatsCalculationScheduler(_WebTestBase):
    """Test stats_calculation_scheduler background loop."""

    async def test_cancellation_exits_cleanly(self):
        """stats_calculation_scheduler exits cleanly on CancelledError."""
        task = asyncio.create_task(web_main.stats_calculation_scheduler())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # If we get here without hanging, it exited cleanly


# ============================================================================
# Service worker endpoint
# ============================================================================


@_skip_unless_web
class TestServiceWorkerEndpoint(_WebTestBase):
    """Test /sw.js endpoint."""

    async def test_sw_returns_404_when_file_missing(self):
        """serve_service_worker returns 404 when sw.js does not exist."""
        with patch.object(web_main, "static_dir", web_main.Path("/nonexistent")):
            async with self._client() as client:
                resp = await client.get("/sw.js")
            self.assertEqual(resp.status_code, 404)

    async def test_sw_returns_file_when_exists(self):
        """serve_service_worker returns the file with correct headers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sw_path = os.path.join(tmpdir, "sw.js")
            with open(sw_path, "w") as f:
                f.write("// service worker")
            with patch.object(web_main, "static_dir", web_main.Path(tmpdir)):
                async with self._client() as client:
                    resp = await client.get("/sw.js")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("Service-Worker-Allowed", resp.headers)


# ============================================================================
# Media serving (serve_media and serve_thumbnail)
# ============================================================================


@_skip_unless_web
class TestServeMedia(_WebTestBase):
    """Test /media/{path} endpoint."""

    async def test_media_404_when_no_media_root(self):
        """serve_media returns 404 when media directory not configured."""
        web_main._media_root = None
        async with self._client() as client:
            resp = await client.get("/media/test.jpg")
        self.assertEqual(resp.status_code, 404)

    async def test_media_rejects_absolute_path(self):
        """serve_media returns 403 for absolute path prefix in route."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            # The path traversal check tests ".." in split("/") or startswith("/")
            # We test the function directly since httpx normalizes URLs
            from fastapi import HTTPException

            with self.assertRaises(HTTPException) as ctx:
                await web_main.serve_media(
                    path="../etc/passwd",
                    download=0,
                    user=web_main.UserContext(username="anon", role="master"),
                )
            self.assertEqual(ctx.exception.status_code, 403)

    async def test_media_rejects_no_download_with_download_flag(self):
        """serve_media returns 403 when user has no_download and download=1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            web_main.AUTH_ENABLED = True
            token = "nd-token"
            web_main._sessions[token] = web_main.SessionData(username="viewer1", role="viewer", no_download=True)
            async with self._client() as client:
                resp = await client.get("/media/test.jpg?download=1", cookies={"viewer_auth": token})
            self.assertEqual(resp.status_code, 403)

    async def test_media_serves_existing_file(self):
        """serve_media returns FileResponse for existing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = web_main.Path(tmpdir).resolve()
            web_main._media_root = media_root
            # Create file inside a subfolder to ensure path has segments
            sub_dir = os.path.join(tmpdir, "files")
            os.makedirs(sub_dir)
            test_file = os.path.join(sub_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("content")
            async with self._client() as client:
                resp = await client.get("/media/files/test.txt")
            self.assertEqual(resp.status_code, 200)

    async def test_media_404_for_nonexistent_file(self):
        """serve_media returns 404 for files that don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            async with self._client() as client:
                resp = await client.get("/media/nonexistent.jpg")
            # Path doesn't resolve, so 404
            self.assertIn(resp.status_code, (403, 404))

    async def test_media_restricts_by_chat_id(self):
        """serve_media enforces chat-level access for restricted users."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            # Create media file under chat folder
            chat_dir = os.path.join(tmpdir, "123")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, "photo.jpg"), "w") as f:
                f.write("img")

            web_main.AUTH_ENABLED = True
            token = "restricted-media"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={999})
            async with self._client() as client:
                resp = await client.get("/media/123/photo.jpg", cookies={"viewer_auth": token})
            self.assertEqual(resp.status_code, 403)

    async def test_media_avatar_access_check(self):
        """serve_media enforces chat-level access for avatar paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            avatar_dir = os.path.join(tmpdir, "avatars", "chats")
            os.makedirs(avatar_dir)
            with open(os.path.join(avatar_dir, "456_789.jpg"), "w") as f:
                f.write("avatar")

            web_main.AUTH_ENABLED = True
            token = "av-restricted"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
            async with self._client() as client:
                resp = await client.get("/media/avatars/chats/456_789.jpg", cookies={"viewer_auth": token})
            self.assertEqual(resp.status_code, 403)

    async def test_media_avatar_invalid_chat_id_format(self):
        """serve_media returns 403 for avatar with non-numeric filename."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            avatar_dir = os.path.join(tmpdir, "avatars", "users")
            os.makedirs(avatar_dir)
            with open(os.path.join(avatar_dir, "badname.jpg"), "w") as f:
                f.write("avatar")

            web_main.AUTH_ENABLED = True
            token = "av-bad"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
            async with self._client() as client:
                resp = await client.get("/media/avatars/users/badname.jpg", cookies={"viewer_auth": token})
            self.assertEqual(resp.status_code, 403)


# ============================================================================
# Thumbnail endpoint
# ============================================================================


@_skip_unless_web
class TestServeThumbnail(_WebTestBase):
    """Test /media/thumb/{size}/{folder}/{filename} endpoint."""

    async def test_thumbnail_404_when_no_media_root(self):
        """serve_thumbnail returns 404 when media directory not configured."""
        web_main._media_root = None
        async with self._client() as client:
            resp = await client.get("/media/thumb/200/123/photo.jpg")
        self.assertEqual(resp.status_code, 404)

    async def test_thumbnail_restricts_by_chat_id(self):
        """serve_thumbnail enforces chat-level access for restricted users."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            web_main.AUTH_ENABLED = True
            token = "th-restrict"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={999})
            async with self._client() as client:
                resp = await client.get("/media/thumb/200/123/photo.jpg", cookies={"viewer_auth": token})
            self.assertEqual(resp.status_code, 403)

    async def test_thumbnail_avatar_access_check(self):
        """serve_thumbnail enforces access for avatar thumbnails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            web_main.AUTH_ENABLED = True
            token = "th-av"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
            async with self._client() as client:
                resp = await client.get(
                    "/media/thumb/100/avatars/chats/456_789.jpg",
                    cookies={"viewer_auth": token},
                )
            self.assertEqual(resp.status_code, 403)

    async def test_thumbnail_avatar_invalid_format(self):
        """serve_thumbnail returns 403 for avatar with invalid chat ID format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            web_main.AUTH_ENABLED = True
            token = "th-bad"
            web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
            async with self._client() as client:
                resp = await client.get(
                    "/media/thumb/100/avatars/users/badname.jpg",
                    cookies={"viewer_auth": token},
                )
            self.assertEqual(resp.status_code, 403)

    async def test_thumbnail_returns_404_when_not_generated(self):
        """serve_thumbnail returns 404 when thumbnail generation returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            with patch("src.web.thumbnails.ensure_thumbnail", new_callable=AsyncMock, return_value=None):
                async with self._client() as client:
                    resp = await client.get("/media/thumb/200/123/photo.jpg")
            self.assertEqual(resp.status_code, 404)

    async def test_thumbnail_serves_generated_file(self):
        """serve_thumbnail returns FileResponse when thumbnail is available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            web_main._media_root = web_main.Path(tmpdir)
            thumb_file = os.path.join(tmpdir, "thumb.webp")
            with open(thumb_file, "wb") as f:
                f.write(b"\x00" * 10)
            with patch(
                "src.web.thumbnails.ensure_thumbnail",
                new_callable=AsyncMock,
                return_value=web_main.Path(thumb_file),
            ):
                async with self._client() as client:
                    resp = await client.get("/media/thumb/200/123/photo.jpg")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("image/webp", resp.headers.get("content-type", ""))


# ============================================================================
# Root endpoint
# ============================================================================


@_skip_unless_web
class TestRootEndpoint(_WebTestBase):
    """Test / root endpoint."""

    async def test_root_serves_index_html(self):
        """read_root returns index.html template."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index_path = os.path.join(tmpdir, "index.html")
            with open(index_path, "w") as f:
                f.write("<html>test</html>")
            with patch.object(web_main, "templates_dir", web_main.Path(tmpdir)):
                async with self._client() as client:
                    resp = await client.get("/")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/html", resp.headers.get("content-type", ""))


# ============================================================================
# Login edge cases
# ============================================================================


@_skip_unless_web
class TestLoginEdgeCases(_WebTestBase):
    """Test login endpoint edge cases."""

    async def test_login_invalid_json_returns_400(self):
        """login returns 400 for invalid JSON body."""
        web_main.AUTH_ENABLED = True
        async with self._client() as client:
            resp = await client.post(
                "/api/login",
                content="not json",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_login_viewer_only_header_blocks_master(self):
        """login returns 401 when x-viewer-only header blocks master credentials."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": "admin", "password": "secretpass"},
                    headers={"x-viewer-only": "true"},
                )
            self.assertEqual(resp.status_code, 401)
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_db_unreachable_returns_503(self):
        """login returns 503 when DB is unreachable and credentials don't match master."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        self.mock_db.get_viewer_by_username = AsyncMock(side_effect=Exception("db down"))
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": "viewer1", "password": "wrongpass"},
                )
            self.assertEqual(resp.status_code, 503)
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_failed_audit_log_write_continues(self):
        """login continues normally even when audit log write fails for failed login."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        self.mock_db.create_audit_log = AsyncMock(side_effect=Exception("audit error"))
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": "wrong", "password": "wrong"},
                )
            self.assertEqual(resp.status_code, 401)
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_master_audit_log_failure_continues(self):
        """login succeeds for master even when audit log write fails."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "secretpass"
        self.mock_db.create_audit_log = AsyncMock(side_effect=Exception("audit fail"))
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": "admin", "password": "secretpass"},
                )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass

    async def test_login_viewer_audit_log_failure_continues(self):
        """login succeeds for viewer even when audit log fails."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        saved_pass = web_main.VIEWER_PASSWORD
        web_main.VIEWER_USERNAME = "admin"
        web_main.VIEWER_PASSWORD = "masterpass"

        salt = "testsalt"
        pw_hash = web_main._hash_password("viewerpass", salt)
        self.mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "username": "v1",
                "password_hash": pw_hash,
                "salt": salt,
                "is_active": 1,
                "allowed_chat_ids": None,
                "no_download": 0,
            }
        )
        self.mock_db.create_audit_log = AsyncMock(side_effect=Exception("audit fail"))
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": "v1", "password": "viewerpass"},
                )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["role"], "viewer")
        finally:
            web_main.VIEWER_USERNAME = saved_user
            web_main.VIEWER_PASSWORD = saved_pass


# ============================================================================
# Logout edge cases
# ============================================================================


@_skip_unless_web
class TestLogoutEdgeCases(_WebTestBase):
    """Test logout endpoint edge cases."""

    async def test_logout_with_db_session_not_in_memory(self):
        """logout looks up session from DB when not in memory cache."""
        token = "db-only-token"
        self.mock_db.get_session = AsyncMock(
            return_value={
                "username": "dbuser",
                "role": "viewer",
            }
        )
        async with self._client() as client:
            resp = await client.post("/api/logout", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 200)
        self.mock_db.delete_session.assert_awaited()
        self.mock_db.create_audit_log.assert_awaited()

    async def test_logout_handles_db_delete_error(self):
        """logout succeeds even when DB delete raises (caught by bare except)."""
        token = "fail-token"
        web_main._sessions[token] = web_main.SessionData(username="u", role="master")
        # delete_session failure is caught by the bare `except Exception: pass` block
        self.mock_db.delete_session = AsyncMock(side_effect=Exception("db error"))
        # create_audit_log is outside the try/except so we keep it working
        self.mock_db.create_audit_log = AsyncMock()
        async with self._client() as client:
            resp = await client.post("/api/logout", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Token auth edge cases
# ============================================================================


@_skip_unless_web
class TestTokenAuthEdgeCases(_WebTestBase):
    """Test /auth/token endpoint edge cases."""

    async def test_token_auth_invalid_json_returns_400(self):
        """auth_via_token returns 400 for invalid JSON."""
        async with self._client() as client:
            resp = await client.post(
                "/auth/token",
                content="bad json",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_token_auth_rate_limited(self):
        """auth_via_token returns 429 when rate limited."""
        saved_attempts = dict(web_main._login_attempts)
        now = time.time()
        web_main._login_attempts["127.0.0.1"] = [now] * (web_main._LOGIN_RATE_LIMIT + 5)
        try:
            async with self._client() as client:
                resp = await client.post("/auth/token", json={"token": "test"})
            self.assertEqual(resp.status_code, 429)
        finally:
            web_main._login_attempts.clear()
            web_main._login_attempts.update(saved_attempts)

    async def test_token_auth_with_no_download(self):
        """auth_via_token includes no_download flag in response."""
        self.mock_db.verify_viewer_token = AsyncMock(
            return_value={
                "id": 2,
                "label": "restricted",
                "allowed_chat_ids": json.dumps([10]),
                "no_download": 1,
            }
        )
        async with self._client() as client:
            resp = await client.post("/auth/token", json={"token": "valid"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["no_download"])


# ============================================================================
# Export endpoint
# ============================================================================


@_skip_unless_web
class TestExportEndpoint(_WebTestBase):
    """Test /api/chats/{chat_id}/export endpoint."""

    async def test_export_returns_403_for_no_download_user(self):
        """export_chat returns 403 when user has no_download restriction."""
        web_main.AUTH_ENABLED = True
        token = "nd-export"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", no_download=True)
        async with self._client() as client:
            resp = await client.get("/api/chats/1/export", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)

    async def test_export_returns_403_for_restricted_chat(self):
        """export_chat returns 403 when user cannot access the chat."""
        web_main.AUTH_ENABLED = True
        token = "restricted-export"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
        async with self._client() as client:
            resp = await client.get("/api/chats/999/export", cookies={"viewer_auth": token})
        self.assertEqual(resp.status_code, 403)

    async def test_export_returns_404_when_chat_not_found(self):
        """export_chat returns 404 when chat doesn't exist."""
        self.mock_db.get_chat_by_id = AsyncMock(return_value=None)
        async with self._client() as client:
            resp = await client.get("/api/chats/999/export")
        self.assertEqual(resp.status_code, 404)

    async def test_export_streams_json_successfully(self):
        """export_chat streams JSON for a valid chat."""
        self.mock_db.get_chat_by_id = AsyncMock(return_value={"title": "Test Chat", "username": None})

        async def fake_export(chat_id):
            yield {"id": 1, "text": "hello", "date": "2025-01-01"}
            yield {"id": 2, "text": "world", "date": "2025-01-02"}

        self.mock_db.get_messages_for_export = fake_export

        async with self._client() as client:
            resp = await client.get("/api/chats/42/export")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.text)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["text"], "hello")

    async def test_export_handles_db_error(self):
        """export_chat returns 500 on db error."""
        self.mock_db.get_chat_by_id = AsyncMock(side_effect=RuntimeError("db error"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/export")
        self.assertEqual(resp.status_code, 500)


# ============================================================================
# Admin viewer update edge cases
# ============================================================================


@_skip_unless_web
class TestAdminViewerUpdateEdgeCases(_WebTestBase):
    """Test /api/admin/viewers/{id} PUT edge cases."""

    async def test_update_viewer_password(self):
        """update_viewer updates password correctly."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/viewers/1",
                json={"password": "newlongpassword123"},
            )
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_db.update_viewer_account.call_args.kwargs
        self.assertIn("password_hash", call_kwargs)
        self.assertIn("salt", call_kwargs)

    async def test_update_viewer_short_password_returns_400(self):
        """update_viewer returns 400 for password shorter than 8 chars."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put("/api/admin/viewers/1", json={"password": "short"})
        self.assertEqual(resp.status_code, 400)

    async def test_update_viewer_allowed_chat_ids_null(self):
        """update_viewer sets allowed_chat_ids to None."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/viewers/1",
                json={"allowed_chat_ids": None},
            )
        self.assertEqual(resp.status_code, 200)

    async def test_update_viewer_no_download(self):
        """update_viewer updates no_download flag."""
        self.mock_db.get_viewer_account = AsyncMock(return_value={"id": 1, "username": "v1"})
        async with self._client() as client:
            resp = await client.put("/api/admin/viewers/1", json={"no_download": True})
        self.assertEqual(resp.status_code, 200)

    async def test_update_viewer_invalid_json_returns_400(self):
        """update_viewer returns 400 for invalid JSON."""
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/viewers/1",
                content="bad json",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_create_viewer_master_conflict_returns_409(self):
        """create_viewer returns 409 when username matches master account."""
        web_main.AUTH_ENABLED = True
        saved_user = web_main.VIEWER_USERNAME
        web_main.VIEWER_USERNAME = "admin"
        token = "master-tok-conflict"
        web_main._sessions[token] = web_main.SessionData(username="admin", role="master")
        try:
            async with self._client() as client:
                resp = await client.post(
                    "/api/admin/viewers",
                    json={"username": "admin", "password": "longpassword123"},
                    cookies={"viewer_auth": token},
                )
            self.assertEqual(resp.status_code, 409)
        finally:
            web_main.VIEWER_USERNAME = saved_user

    async def test_create_viewer_invalid_json_returns_400(self):
        """create_viewer returns 400 for invalid JSON."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/viewers",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Admin token edge cases
# ============================================================================


@_skip_unless_web
class TestAdminTokenEdgeCases(_WebTestBase):
    """Test /api/admin/tokens edge cases."""

    async def test_create_token_with_expiry(self):
        """create_token accepts expires_at field."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/tokens",
                json={
                    "allowed_chat_ids": [1, 2],
                    "expires_at": "2030-01-01T00:00:00Z",
                },
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)

    async def test_create_token_invalid_expiry_returns_400(self):
        """create_token returns 400 for invalid expires_at."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/tokens",
                json={"allowed_chat_ids": [1], "expires_at": "not-a-date"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_create_token_invalid_json_returns_400(self):
        """create_token returns 400 for invalid JSON."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/tokens",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_update_token_success(self):
        """update_token updates and returns token data."""
        self.mock_db.update_viewer_token = AsyncMock(
            return_value={
                "id": 1,
                "label": "updated",
                "allowed_chat_ids": json.dumps([1, 2]),
                "is_revoked": 0,
                "no_download": 0,
                "expires_at": None,
            }
        )
        async with self._client() as client:
            resp = await client.put("/api/admin/tokens/1", json={"label": "updated"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["label"], "updated")

    async def test_update_token_revoke_invalidates_sessions(self):
        """update_token invalidates sessions when revoking."""
        web_main._sessions["tok-sess"] = web_main.SessionData(username="token:test", role="token", source_token_id=5)
        self.mock_db.update_viewer_token = AsyncMock(
            return_value={
                "id": 5,
                "label": "test",
                "allowed_chat_ids": None,
                "is_revoked": 1,
                "no_download": 0,
                "expires_at": None,
            }
        )
        async with self._client() as client:
            resp = await client.put("/api/admin/tokens/5", json={"is_revoked": True})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("tok-sess", web_main._sessions)

    async def test_update_token_invalid_json_returns_400(self):
        """update_token returns 400 for invalid JSON."""
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/tokens/1",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_update_token_allowed_chat_ids_null_returns_400(self):
        """update_token returns 400 when allowed_chat_ids is null."""
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/tokens/1",
                json={"allowed_chat_ids": None},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_create_token_with_no_download(self):
        """create_token accepts no_download flag."""
        async with self._client() as client:
            resp = await client.post(
                "/api/admin/tokens",
                json={"allowed_chat_ids": [1], "no_download": True, "label": "nd"},
            )
        self.assertEqual(resp.status_code, 200)

    async def test_update_token_no_download(self):
        """update_token updates no_download flag."""
        self.mock_db.update_viewer_token = AsyncMock(
            return_value={
                "id": 1,
                "label": "test",
                "allowed_chat_ids": json.dumps([1]),
                "is_revoked": 0,
                "no_download": 1,
                "expires_at": None,
            }
        )
        async with self._client() as client:
            resp = await client.put("/api/admin/tokens/1", json={"no_download": True})
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Admin settings edge cases
# ============================================================================


@_skip_unless_web
class TestAdminSettingsEdgeCases(_WebTestBase):
    """Test /api/admin/settings edge cases."""

    async def test_set_setting_invalid_json_returns_400(self):
        """set_setting returns 400 for invalid JSON body."""
        async with self._client() as client:
            resp = await client.put(
                "/api/admin/settings/key",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)


# ============================================================================
# Push subscribe/unsubscribe edge cases
# ============================================================================


@_skip_unless_web
class TestPushEndpointEdgeCases(_WebTestBase):
    """Test push endpoint edge cases."""

    async def test_subscribe_invalid_json(self):
        """push_subscribe returns 400 for invalid JSON."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_subscribe_failure_returns_500(self):
        """push_subscribe returns 500 when subscription storage fails."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.subscribe = AsyncMock(return_value=False)
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://push.example.com/sub",
                    "keys": {"p256dh": "k", "auth": "a"},
                },
            )
        self.assertEqual(resp.status_code, 500)

    async def test_subscribe_with_chat_id_restricted(self):
        """push_subscribe returns 403 when user can't access the chat."""
        web_main.AUTH_ENABLED = True
        token = "push-restrict"
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", allowed_chat_ids={100})
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://push.example.com/sub",
                    "keys": {"p256dh": "k", "auth": "a"},
                    "chat_id": 999,
                },
                cookies={"viewer_auth": token},
            )
        self.assertEqual(resp.status_code, 403)

    async def test_subscribe_db_connection_error(self):
        """push_subscribe returns 503 on DB connection error."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.subscribe = AsyncMock(side_effect=OSError("db down"))
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/subscribe",
                json={
                    "endpoint": "https://push.example.com/sub",
                    "keys": {"p256dh": "k", "auth": "a"},
                },
            )
        self.assertEqual(resp.status_code, 503)

    async def test_unsubscribe_missing_endpoint(self):
        """push_unsubscribe returns 400 when endpoint missing."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post("/api/push/unsubscribe", json={})
        self.assertEqual(resp.status_code, 400)

    async def test_unsubscribe_invalid_json(self):
        """push_unsubscribe returns 400 for invalid JSON."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/unsubscribe",
                content="bad",
                headers={"content-type": "application/json"},
            )
        self.assertEqual(resp.status_code, 400)

    async def test_unsubscribe_db_connection_error(self):
        """push_unsubscribe returns 503 on DB connection error."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.unsubscribe = AsyncMock(side_effect=OSError("db down"))
        web_main.push_manager = mock_pm
        async with self._client() as client:
            resp = await client.post(
                "/api/push/unsubscribe",
                json={"endpoint": "https://push.example.com/sub"},
            )
        self.assertEqual(resp.status_code, 503)


# ============================================================================
# Internal push endpoint edge cases
# ============================================================================


@_skip_unless_web
class TestInternalPushEdgeCases(_WebTestBase):
    """Test /internal/push edge cases."""

    async def test_internal_push_with_secret_valid(self):
        """internal_push accepts valid secret."""
        mock_listener = MagicMock()
        mock_listener.handle_http_push = AsyncMock()
        web_main.realtime_listener = mock_listener
        with patch.dict(os.environ, {"INTERNAL_PUSH_SECRET": "mysecret"}):
            async with self._client() as client:
                resp = await client.post(
                    "/internal/push",
                    json={"type": "test"},
                    headers={"Authorization": "Bearer mysecret"},
                )
        self.assertEqual(resp.status_code, 200)

    async def test_internal_push_with_secret_invalid(self):
        """internal_push rejects invalid secret."""
        with patch.dict(os.environ, {"INTERNAL_PUSH_SECRET": "mysecret"}):
            async with self._client() as client:
                resp = await client.post(
                    "/internal/push",
                    json={"type": "test"},
                    headers={"Authorization": "Bearer wrong"},
                )
        self.assertEqual(resp.status_code, 403)

    async def test_internal_push_handles_error(self):
        """internal_push returns error status when processing fails."""
        mock_listener = MagicMock()
        mock_listener.handle_http_push = AsyncMock(side_effect=Exception("fail"))
        web_main.realtime_listener = mock_listener
        async with self._client() as client:
            resp = await client.post("/internal/push", json={"type": "test"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "error")


# ============================================================================
# Notification settings edge cases
# ============================================================================


@_skip_unless_web
class TestNotificationSettingsEdgeCases(_WebTestBase):
    """Test /api/notifications/settings edge cases."""

    async def test_returns_enabled_with_valid_session(self):
        """notification_settings returns enabled when session valid."""
        web_main.AUTH_ENABLED = True
        token = "notif-tok"
        web_main._sessions[token] = web_main.SessionData(username="u", role="master", created_at=time.time())
        web_main.config.enable_notifications = True
        async with self._client() as client:
            resp = await client.get(
                "/api/notifications/settings",
                cookies={"viewer_auth": token},
            )
        data = resp.json()
        self.assertTrue(data["enabled"])
        web_main.config.enable_notifications = False

    async def test_returns_disabled_with_expired_session(self):
        """notification_settings returns disabled for expired session."""
        web_main.AUTH_ENABLED = True
        token = "exp-notif"
        web_main._sessions[token] = web_main.SessionData(
            username="u",
            role="master",
            created_at=time.time() - web_main.AUTH_SESSION_SECONDS - 100,
        )
        async with self._client() as client:
            resp = await client.get(
                "/api/notifications/settings",
                cookies={"viewer_auth": token},
            )
        data = resp.json()
        self.assertFalse(data["enabled"])


# ============================================================================
# _resolve_session edge cases
# ============================================================================


@_skip_unless_web
class TestResolveSessionEdgeCases(_WebTestBase):
    """Test _resolve_session edge cases."""

    async def test_returns_none_when_db_get_session_raises(self):
        """_resolve_session returns None when DB query raises."""
        self.mock_db.get_session = AsyncMock(side_effect=Exception("db error"))
        result = await web_main._resolve_session("unknown-tok")
        self.assertIsNone(result)

    async def test_returns_none_when_db_returns_none(self):
        """_resolve_session returns None when DB returns no row."""
        self.mock_db.get_session = AsyncMock(return_value=None)
        result = await web_main._resolve_session("missing-tok")
        self.assertIsNone(result)


# ============================================================================
# _create_session with DB
# ============================================================================


@_skip_unless_web
class TestCreateSessionWithDb(_WebTestBase):
    """Test _create_session with DB integration."""

    async def test_creates_session_with_db_persistence(self):
        """_create_session persists session to DB when db is available."""
        token = await web_main._create_session("admin", "master", allowed_chat_ids={1, 2})
        self.assertIn(token, web_main._sessions)
        self.mock_db.save_session.assert_awaited_once()

    async def test_handles_db_save_failure(self):
        """_create_session succeeds even when DB save fails."""
        self.mock_db.save_session = AsyncMock(side_effect=Exception("db error"))
        token = await web_main._create_session("admin", "master")
        self.assertIn(token, web_main._sessions)

    async def test_eviction_with_db(self):
        """_create_session evicts old sessions and calls db.delete_session."""
        for _ in range(web_main._MAX_SESSIONS_PER_USER):
            await web_main._create_session("evict_user", "viewer")

        await web_main._create_session("evict_user", "viewer")
        user_sessions = [s for s in web_main._sessions.values() if s.username == "evict_user"]
        self.assertEqual(len(user_sessions), web_main._MAX_SESSIONS_PER_USER)
        self.mock_db.delete_session.assert_awaited()


# ============================================================================
# _invalidate with DB
# ============================================================================


@_skip_unless_web
class TestInvalidateWithDb(_WebTestBase):
    """Test session invalidation with DB errors."""

    async def test_invalidate_user_sessions_handles_db_error(self):
        """_invalidate_user_sessions continues when DB delete fails."""
        self.mock_db.delete_user_sessions = AsyncMock(side_effect=Exception("db err"))
        await web_main._create_session("user1", "viewer")
        await web_main._invalidate_user_sessions("user1")
        # Memory sessions should still be cleared
        user_sessions = [s for s in web_main._sessions.values() if s.username == "user1"]
        self.assertEqual(len(user_sessions), 0)

    async def test_invalidate_token_sessions_handles_db_error(self):
        """_invalidate_token_sessions continues when DB delete fails."""
        self.mock_db.delete_sessions_by_source_token_id = AsyncMock(side_effect=Exception("db err"))
        await web_main._create_session("t1", "token", source_token_id=10)
        await web_main._invalidate_token_sessions(10)
        token_sessions = [s for s in web_main._sessions.values() if s.source_token_id == 10]
        self.assertEqual(len(token_sessions), 0)


# ============================================================================
# Chats endpoint: avatar error handling
# ============================================================================


@_skip_unless_web
class TestChatsAvatarErrors(_WebTestBase):
    """Test chats endpoint avatar error handling."""

    async def test_chats_handles_avatar_lookup_error(self):
        """get_chats handles avatar lookup errors gracefully."""
        chats = [{"id": 1, "title": "Chat", "type": "private"}]
        self.mock_db.get_all_chats = AsyncMock(return_value=chats)
        self.mock_db.get_chat_count = AsyncMock(return_value=1)
        with patch.object(web_main, "_get_cached_avatar_path", side_effect=Exception("fs error")):
            async with self._client() as client:
                resp = await client.get("/api/chats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["chats"][0]["avatar_url"])


# ============================================================================
# Folders/Topics/Pinned DB error handling
# ============================================================================


@_skip_unless_web
class TestEndpointDbErrors(_WebTestBase):
    """Test DB error handling in various endpoints."""

    async def test_folders_db_connection_error(self):
        """get_folders returns 503 on DB connection error."""
        self.mock_db.get_all_folders = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/folders")
        self.assertEqual(resp.status_code, 503)

    async def test_folders_generic_error(self):
        """get_folders returns 500 on generic error."""
        self.mock_db.get_all_folders = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/folders")
        self.assertEqual(resp.status_code, 500)

    async def test_topics_db_connection_error(self):
        """get_chat_topics returns 503 on DB connection error."""
        self.mock_db.get_forum_topics = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/topics")
        self.assertEqual(resp.status_code, 503)

    async def test_topics_generic_error(self):
        """get_chat_topics returns 500 on generic error."""
        self.mock_db.get_forum_topics = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/topics")
        self.assertEqual(resp.status_code, 500)

    async def test_pinned_db_connection_error(self):
        """get_pinned_messages returns 503 on DB connection error."""
        self.mock_db.get_pinned_messages = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/pinned")
        self.assertEqual(resp.status_code, 503)

    async def test_pinned_generic_error(self):
        """get_pinned_messages returns 500 on generic error."""
        self.mock_db.get_pinned_messages = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/pinned")
        self.assertEqual(resp.status_code, 500)

    async def test_archived_count_db_connection_error(self):
        """get_archived_count returns 503 on DB connection error."""
        self.mock_db.get_archived_chat_count = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/archived/count")
        self.assertEqual(resp.status_code, 503)

    async def test_archived_count_generic_error(self):
        """get_archived_count returns 500 on generic error."""
        self.mock_db.get_archived_chat_count = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/archived/count")
        self.assertEqual(resp.status_code, 500)

    async def test_stats_db_connection_error(self):
        """get_stats returns 503 on DB connection error."""
        self.mock_db.get_cached_statistics = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/stats")
        self.assertEqual(resp.status_code, 503)

    async def test_stats_generic_error(self):
        """get_stats returns 500 on generic error."""
        self.mock_db.get_cached_statistics = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/stats")
        self.assertEqual(resp.status_code, 500)

    async def test_refresh_stats_db_connection_error(self):
        """refresh_stats returns 503 on DB connection error."""
        self.mock_db.calculate_and_store_statistics = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.post("/api/stats/refresh")
        self.assertEqual(resp.status_code, 503)

    async def test_refresh_stats_generic_error(self):
        """refresh_stats returns 500 on generic error."""
        self.mock_db.calculate_and_store_statistics = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.post("/api/stats/refresh")
        self.assertEqual(resp.status_code, 500)

    async def test_chat_stats_db_connection_error(self):
        """get_chat_stats returns 503 on DB connection error."""
        self.mock_db.get_chat_stats = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/stats")
        self.assertEqual(resp.status_code, 503)

    async def test_chat_stats_generic_error(self):
        """get_chat_stats returns 500 on generic error."""
        self.mock_db.get_chat_stats = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/stats")
        self.assertEqual(resp.status_code, 500)

    async def test_messages_generic_error(self):
        """get_messages returns 500 on generic error."""
        self.mock_db.get_messages_paginated = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages")
        self.assertEqual(resp.status_code, 500)

    async def test_message_by_date_db_connection_error(self):
        """get_message_by_date returns 503 on DB connection error."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2025-01-01")
        self.assertEqual(resp.status_code, 503)

    async def test_message_by_date_generic_error(self):
        """get_message_by_date returns 500 on generic error."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(side_effect=RuntimeError("bug"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2025-01-01")
        self.assertEqual(resp.status_code, 500)

    async def test_message_by_date_invalid_timezone_fallback(self):
        """get_message_by_date falls back to UTC for invalid timezone."""
        self.mock_db.find_message_by_date_with_joins = AsyncMock(return_value={"id": 1})
        async with self._client() as client:
            resp = await client.get("/api/chats/1/messages/by-date?date=2025-01-01&timezone=Invalid/Zone")
        self.assertEqual(resp.status_code, 200)

    async def test_export_db_connection_error(self):
        """export_chat returns 503 on DB connection error."""
        self.mock_db.get_chat_by_id = AsyncMock(side_effect=OSError("conn"))
        async with self._client() as client:
            resp = await client.get("/api/chats/1/export")
        self.assertEqual(resp.status_code, 503)


# ============================================================================
# handle_realtime_notification: push with no sender
# ============================================================================


@_skip_unless_web
class TestRealtimeNotificationPushNoSender(_WebTestBase):
    """Test handle_realtime_notification push when sender not found."""

    async def test_push_with_no_sender_info(self):
        """Push notification works when sender lookup returns None."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.notify_new_message = AsyncMock(return_value=1)
        web_main.push_manager = mock_pm
        self.mock_db.get_chat_by_id = AsyncMock(return_value=None)
        self.mock_db.get_user_by_id = AsyncMock(return_value=None)

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
        self.assertEqual(call_kwargs["sender_name"], "")

    async def test_push_with_no_sender_id(self):
        """Push notification works when message has no sender_id."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.notify_new_message = AsyncMock(return_value=1)
        web_main.push_manager = mock_pm
        self.mock_db.get_chat_by_id = AsyncMock(return_value={"title": "Chat"})

        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock):
            await web_main.handle_realtime_notification(
                {
                    "type": "new_message",
                    "chat_id": 42,
                    "data": {"message": {"id": 1, "text": "hello"}},
                }
            )

        mock_pm.notify_new_message.assert_awaited_once()
        call_kwargs = mock_pm.notify_new_message.call_args.kwargs
        self.assertEqual(call_kwargs["sender_name"], "")

    async def test_push_with_no_db(self):
        """Push notification handles missing db gracefully."""
        mock_pm = MagicMock()
        mock_pm.is_enabled = True
        mock_pm.notify_new_message = AsyncMock(return_value=0)
        web_main.push_manager = mock_pm
        web_main.db = None

        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock):
            await web_main.handle_realtime_notification(
                {
                    "type": "new_message",
                    "chat_id": 42,
                    "data": {"message": {"id": 1, "text": "hello", "sender_id": 100}},
                }
            )

        mock_pm.notify_new_message.assert_awaited_once()


# ============================================================================
# Push module: subscribe/unsubscribe/get_subscriptions (SQLAlchemy paths)
# ============================================================================


def _make_push_manager(push_setting="full"):
    """Helper: create a PushNotificationManager with mock db/config."""
    db = MagicMock()
    cfg = MagicMock()
    cfg.push_notifications = push_setting
    cfg.vapid_private_key = ""
    cfg.vapid_public_key = ""
    cfg.vapid_contact = "mailto:test@example.com"
    return PushNotificationManager(db, cfg)


@_skip_unless_push
class TestPushSubscribe(unittest.IsolatedAsyncioTestCase):
    """Test PushNotificationManager.subscribe with mocked SQLAlchemy session."""

    async def test_subscribe_creates_new_subscription(self):
        """subscribe creates new subscription when endpoint not found."""
        mgr = _make_push_manager()

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.subscribe(
            endpoint="https://push.example.com/sub",
            p256dh="key1",
            auth="auth1",
            chat_id=42,
            user_agent="TestBrowser",
            username="testuser",
            allowed_chat_ids=[1, 2, 3],
        )

        self.assertTrue(result)
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    async def test_subscribe_updates_existing_subscription(self):
        """subscribe updates existing subscription when endpoint found."""
        mgr = _make_push_manager()

        mock_existing = MagicMock()
        mock_existing.endpoint = "https://push.example.com/sub"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_existing
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.subscribe(
            endpoint="https://push.example.com/sub",
            p256dh="newkey",
            auth="newauth",
            username="u1",
        )

        self.assertTrue(result)
        self.assertEqual(mock_existing.p256dh, "newkey")
        self.assertEqual(mock_existing.auth, "newauth")
        mock_session.commit.assert_awaited_once()

    async def test_subscribe_handles_exception(self):
        """subscribe returns False on exception."""
        mgr = _make_push_manager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("db error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.subscribe(
            endpoint="https://push.example.com/sub",
            p256dh="k",
            auth="a",
        )
        self.assertFalse(result)


@_skip_unless_push
class TestPushUnsubscribe(unittest.IsolatedAsyncioTestCase):
    """Test PushNotificationManager.unsubscribe with mocked SQLAlchemy session."""

    async def test_unsubscribe_success(self):
        """unsubscribe removes subscription successfully."""
        mgr = _make_push_manager()

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.unsubscribe(endpoint="https://push.example.com/sub", username="u1")
        self.assertTrue(result)
        mock_session.commit.assert_awaited_once()

    async def test_unsubscribe_without_username(self):
        """unsubscribe works without username filter."""
        mgr = _make_push_manager()

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.unsubscribe(endpoint="https://push.example.com/sub")
        self.assertTrue(result)

    async def test_unsubscribe_handles_exception(self):
        """unsubscribe returns False on exception."""
        mgr = _make_push_manager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("db error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.unsubscribe(endpoint="https://push.example.com/sub")
        self.assertFalse(result)


@_skip_unless_push
class TestPushGetSubscriptions(unittest.IsolatedAsyncioTestCase):
    """Test PushNotificationManager.get_subscriptions."""

    async def test_get_subscriptions_returns_matching(self):
        """get_subscriptions returns subscriptions for a chat."""
        mgr = _make_push_manager()

        mock_sub = MagicMock()
        mock_sub.endpoint = "https://push.example.com/sub1"
        mock_sub.p256dh = "key1"
        mock_sub.auth = "auth1"
        mock_sub.allowed_chat_ids = None  # Master user, no restriction

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_sub]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.get_subscriptions(chat_id=42)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["endpoint"], "https://push.example.com/sub1")

    async def test_get_subscriptions_filters_by_allowed_chats(self):
        """get_subscriptions filters out subs where user can't see the chat."""
        mgr = _make_push_manager()

        mock_sub1 = MagicMock()
        mock_sub1.endpoint = "https://push.example.com/allowed"
        mock_sub1.p256dh = "k1"
        mock_sub1.auth = "a1"
        mock_sub1.allowed_chat_ids = json.dumps([42, 43])

        mock_sub2 = MagicMock()
        mock_sub2.endpoint = "https://push.example.com/denied"
        mock_sub2.p256dh = "k2"
        mock_sub2.auth = "a2"
        mock_sub2.allowed_chat_ids = json.dumps([100, 200])

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_sub1, mock_sub2]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.get_subscriptions(chat_id=42)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["endpoint"], "https://push.example.com/allowed")

    async def test_get_subscriptions_without_chat_id(self):
        """get_subscriptions returns all subscriptions when no chat_id."""
        mgr = _make_push_manager()

        mock_sub = MagicMock()
        mock_sub.endpoint = "https://push.example.com/sub"
        mock_sub.p256dh = "k"
        mock_sub.auth = "a"
        mock_sub.allowed_chat_ids = None

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_sub]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.get_subscriptions(chat_id=None)
        self.assertEqual(len(result), 1)

    async def test_get_subscriptions_handles_exception(self):
        """get_subscriptions returns empty list on exception."""
        mgr = _make_push_manager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("db error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.get_subscriptions(chat_id=42)
        self.assertEqual(result, [])

    async def test_get_subscriptions_handles_corrupted_json(self):
        """get_subscriptions skips subs with corrupted allowed_chat_ids."""
        mgr = _make_push_manager()

        mock_sub = MagicMock()
        mock_sub.endpoint = "https://push.example.com/bad"
        mock_sub.p256dh = "k"
        mock_sub.auth = "a"
        mock_sub.allowed_chat_ids = "not valid json {"

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_sub]
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mgr.db.db_manager.async_session_factory.return_value = mock_ctx

        result = await mgr.get_subscriptions(chat_id=42)
        self.assertEqual(len(result), 0)


# ============================================================================
# Push send_notification: WebPushException edge cases
# ============================================================================


@_skip_unless_push
class TestSendNotificationEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Test send_notification edge cases."""

    async def test_handles_webpush_404(self):
        """send_notification removes 404 subscriptions."""
        mgr = _make_push_manager()
        mgr._vapid = MagicMock()
        mgr._vapid.sign.return_value = {"Authorization": "vapid t=token"}
        subs = [{"endpoint": "https://push.example.com/gone", "keys": {"p256dh": "k", "auth": "a"}}]
        mgr.get_subscriptions = AsyncMock(return_value=subs)
        mgr.unsubscribe = AsyncMock()

        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("src.web.push.webpush", side_effect=WebPushException("Not Found", response=mock_response)):
            result = await mgr.send_notification("Test", "404")

        self.assertEqual(result, 0)
        mgr.unsubscribe.assert_awaited_once()

    async def test_handles_generic_webpush_error(self):
        """send_notification logs but continues on non-status-code errors."""
        mgr = _make_push_manager()
        mgr._vapid = MagicMock()
        mgr._vapid.sign.return_value = {"Authorization": "vapid t=token"}
        subs = [{"endpoint": "https://push.example.com/err", "keys": {"p256dh": "k", "auth": "a"}}]
        mgr.get_subscriptions = AsyncMock(return_value=subs)

        from pywebpush import WebPushException

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("src.web.push.webpush", side_effect=WebPushException("Server Error", response=mock_response)):
            result = await mgr.send_notification("Test", "500")

        self.assertEqual(result, 0)

    async def test_handles_non_webpush_exception(self):
        """send_notification handles non-WebPushException errors."""
        mgr = _make_push_manager()
        mgr._vapid = MagicMock()
        mgr._vapid.sign.return_value = {"Authorization": "vapid t=token"}
        subs = [{"endpoint": "https://push.example.com/bad", "keys": {"p256dh": "k", "auth": "a"}}]
        mgr.get_subscriptions = AsyncMock(return_value=subs)

        with patch("src.web.push.webpush", side_effect=RuntimeError("network error")):
            result = await mgr.send_notification("Test", "Error")

        self.assertEqual(result, 0)


# ============================================================================
# Push initialize: DER/raw key format
# ============================================================================


@_skip_unless_push
class TestPushInitializeDerKey(unittest.IsolatedAsyncioTestCase):
    """Test initialize() with non-PEM key format."""

    async def test_uses_from_string_for_non_pem_key(self):
        """initialize() uses Vapid.from_string for non-PEM key."""
        mgr = _make_push_manager()
        mgr.db.get_metadata = AsyncMock(
            side_effect=lambda k: {
                "vapid_private_key": "raw_key_data_no_pem",
                "vapid_public_key": "BPUBLIC",
            }.get(k)
        )

        with patch("src.web.push.Vapid") as mock_vapid_cls:
            mock_vapid_cls.from_string.return_value = MagicMock()
            result = await mgr.initialize()

        self.assertTrue(result)
        mock_vapid_cls.from_string.assert_called_once_with("raw_key_data_no_pem")


if __name__ == "__main__":
    unittest.main()
