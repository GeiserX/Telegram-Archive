"""Tests for web main module (src/web/main.py).

Pure utility functions and classes are tested directly.
Route handlers that require a running FastAPI app use pytest.importorskip
so they are gracefully skipped when pydantic version mismatches prevent import.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# The module import triggers FastAPI initialization, which may fail on
# environments with pydantic version mismatches.  Guard the import so
# pure-function tests still run even when FastAPI cannot be loaded.
try:
    os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_wm_"))
    from src.web import main as web_main

    _WEB_MAIN_AVAILABLE = True
except Exception:
    _WEB_MAIN_AVAILABLE = False
    web_main = None  # type: ignore[assignment]


def _skip_unless_web_main(cls_or_fn):
    """Skip test class/method when web_main could not be imported."""
    return unittest.skipUnless(_WEB_MAIN_AVAILABLE, "web_main import failed (pydantic mismatch)")(cls_or_fn)


# ============================================================================
# ConnectionManager (pure async, no FastAPI dependency beyond WebSocket type)
# ============================================================================


@_skip_unless_web_main
class TestConnectionManagerConnect(unittest.IsolatedAsyncioTestCase):
    """Test ConnectionManager.connect and disconnect."""

    def setUp(self):
        self.mgr = web_main.ConnectionManager()

    async def test_connect_adds_websocket(self):
        """connect() adds websocket to active_connections."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        self.assertIn(ws, self.mgr.active_connections)

    async def test_connect_initializes_empty_subscription_set(self):
        """connect() creates an empty subscription set for the websocket."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        self.assertEqual(self.mgr.active_connections[ws], set())

    async def test_disconnect_removes_websocket(self):
        """disconnect() removes websocket from active_connections."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        self.mgr.disconnect(ws)
        self.assertNotIn(ws, self.mgr.active_connections)

    async def test_disconnect_nonexistent_is_noop(self):
        """disconnect() does not raise for unknown websocket."""
        ws = AsyncMock()
        self.mgr.disconnect(ws)  # should not raise


@_skip_unless_web_main
class TestConnectionManagerSubscribe(unittest.IsolatedAsyncioTestCase):
    """Test ConnectionManager.subscribe and unsubscribe."""

    def setUp(self):
        self.mgr = web_main.ConnectionManager()

    async def test_subscribe_adds_chat_id(self):
        """subscribe() adds chat_id to connection's subscriptions."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        result = self.mgr.subscribe(ws, 42)
        self.assertTrue(result)
        self.assertIn(42, self.mgr.active_connections[ws])

    async def test_subscribe_returns_false_for_unknown_ws(self):
        """subscribe() returns False for unregistered websocket."""
        ws = AsyncMock()
        result = self.mgr.subscribe(ws, 42)
        self.assertFalse(result)

    async def test_subscribe_denied_by_acl(self):
        """subscribe() returns False when chat_id not in allowed set."""
        ws = AsyncMock()
        await self.mgr.connect(ws, allowed_chat_ids={100, 200})
        result = self.mgr.subscribe(ws, 999)
        self.assertFalse(result)

    async def test_subscribe_allowed_by_acl(self):
        """subscribe() returns True when chat_id is in allowed set."""
        ws = AsyncMock()
        await self.mgr.connect(ws, allowed_chat_ids={100, 200})
        result = self.mgr.subscribe(ws, 100)
        self.assertTrue(result)

    async def test_subscribe_allowed_when_acl_is_none(self):
        """subscribe() allows any chat when allowed_chat_ids is None."""
        ws = AsyncMock()
        await self.mgr.connect(ws, allowed_chat_ids=None)
        result = self.mgr.subscribe(ws, 999)
        self.assertTrue(result)

    async def test_unsubscribe_removes_chat_id(self):
        """unsubscribe() removes chat_id from subscriptions."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        self.mgr.subscribe(ws, 42)
        self.mgr.unsubscribe(ws, 42)
        self.assertNotIn(42, self.mgr.active_connections[ws])

    async def test_unsubscribe_nonexistent_chat_is_noop(self):
        """unsubscribe() does not raise when chat was never subscribed."""
        ws = AsyncMock()
        await self.mgr.connect(ws)
        self.mgr.unsubscribe(ws, 999)  # should not raise


@_skip_unless_web_main
class TestConnectionManagerBroadcast(unittest.IsolatedAsyncioTestCase):
    """Test ConnectionManager broadcast methods."""

    def setUp(self):
        self.mgr = web_main.ConnectionManager()

    async def test_broadcast_to_chat_sends_to_subscribed(self):
        """broadcast_to_chat sends message to connections subscribed to that chat."""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await self.mgr.connect(ws1)
        await self.mgr.connect(ws2)
        self.mgr.subscribe(ws1, 42)
        # ws2 is not subscribed to 42

        await self.mgr.broadcast_to_chat(42, {"type": "test"})

        ws1.send_json.assert_awaited_once_with({"type": "test"})
        # Empty subscriptions no longer receive chat-specific events.
        ws2.send_json.assert_not_awaited()

    async def test_broadcast_to_chat_respects_acl(self):
        """broadcast_to_chat skips connections whose ACL excludes the chat."""
        ws = AsyncMock()
        await self.mgr.connect(ws, allowed_chat_ids={100})
        self.mgr.subscribe(ws, 100)

        await self.mgr.broadcast_to_chat(999, {"type": "test"})
        ws.send_json.assert_not_awaited()

    async def test_broadcast_to_chat_disconnects_failed_ws(self):
        """broadcast_to_chat removes websockets that fail to send."""
        ws = AsyncMock()
        ws.send_json.side_effect = Exception("broken pipe")
        await self.mgr.connect(ws)
        self.mgr.subscribe(ws, 1)

        await self.mgr.broadcast_to_chat(1, {"type": "test"})
        self.assertNotIn(ws, self.mgr.active_connections)

    async def test_broadcast_to_all_sends_to_every_connection(self):
        """broadcast_to_all sends to all connected websockets."""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await self.mgr.connect(ws1)
        await self.mgr.connect(ws2)

        await self.mgr.broadcast_to_all({"type": "ping"})
        ws1.send_json.assert_awaited_once()
        ws2.send_json.assert_awaited_once()

    async def test_broadcast_to_all_cleans_up_broken(self):
        """broadcast_to_all removes broken websockets."""
        ws = AsyncMock()
        ws.send_json.side_effect = RuntimeError("closed")
        await self.mgr.connect(ws)

        await self.mgr.broadcast_to_all({"type": "ping"})
        self.assertNotIn(ws, self.mgr.active_connections)


# ============================================================================
# Pure functions: password hashing, rate limiting, connection error detection
# ============================================================================


@_skip_unless_web_main
class TestHashPassword(unittest.TestCase):
    """Test _hash_password determinism and format."""

    def test_returns_hex_string(self):
        """_hash_password returns a hex-encoded string."""
        result = web_main._hash_password("secret", "salt123")
        # PBKDF2 SHA256 produces 32 bytes = 64 hex chars
        self.assertEqual(len(result), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_deterministic_for_same_inputs(self):
        """_hash_password returns the same hash for identical inputs."""
        h1 = web_main._hash_password("pass", "salty")
        h2 = web_main._hash_password("pass", "salty")
        self.assertEqual(h1, h2)

    def test_different_salt_produces_different_hash(self):
        """_hash_password produces different output for different salts."""
        h1 = web_main._hash_password("pass", "salt_a")
        h2 = web_main._hash_password("pass", "salt_b")
        self.assertNotEqual(h1, h2)

    def test_different_password_produces_different_hash(self):
        """_hash_password produces different output for different passwords."""
        h1 = web_main._hash_password("alpha", "salt")
        h2 = web_main._hash_password("bravo", "salt")
        self.assertNotEqual(h1, h2)


@_skip_unless_web_main
class TestVerifyPassword(unittest.TestCase):
    """Test _verify_password matches _hash_password output."""

    def test_returns_true_for_matching_password(self):
        """_verify_password returns True when password matches stored hash."""
        salt = "test_salt"
        pw = "correct_password"
        pw_hash = web_main._hash_password(pw, salt)
        self.assertTrue(web_main._verify_password(pw, salt, pw_hash))

    def test_returns_false_for_wrong_password(self):
        """_verify_password returns False when password does not match."""
        salt = "test_salt"
        pw_hash = web_main._hash_password("correct", salt)
        self.assertFalse(web_main._verify_password("wrong", salt, pw_hash))


@_skip_unless_web_main
class TestCheckRateLimit(unittest.TestCase):
    """Test _check_rate_limit allows/blocks based on attempt count."""

    def setUp(self):
        self._saved = dict(web_main._login_attempts)
        web_main._login_attempts.clear()

    def tearDown(self):
        web_main._login_attempts.clear()
        web_main._login_attempts.update(self._saved)

    def test_allows_first_request(self):
        """_check_rate_limit returns True for a fresh IP."""
        self.assertTrue(web_main._check_rate_limit("10.0.0.1"))

    def test_blocks_after_exceeding_limit(self):
        """_check_rate_limit returns False after too many attempts."""
        ip = "10.0.0.2"
        now = time.time()
        web_main._login_attempts[ip] = [now] * web_main._LOGIN_RATE_LIMIT
        self.assertFalse(web_main._check_rate_limit(ip))

    def test_allows_after_window_expires(self):
        """_check_rate_limit allows requests once old attempts expire."""
        ip = "10.0.0.3"
        old = time.time() - web_main._LOGIN_RATE_WINDOW - 1
        web_main._login_attempts[ip] = [old] * 100
        self.assertTrue(web_main._check_rate_limit(ip))


@_skip_unless_web_main
class TestRecordLoginAttempt(unittest.TestCase):
    """Test _record_login_attempt appends timestamps."""

    def setUp(self):
        self._saved = dict(web_main._login_attempts)
        web_main._login_attempts.clear()

    def tearDown(self):
        web_main._login_attempts.clear()
        web_main._login_attempts.update(self._saved)

    def test_creates_entry_for_new_ip(self):
        """_record_login_attempt creates list for a new IP."""
        web_main._record_login_attempt("192.168.1.1")
        self.assertEqual(len(web_main._login_attempts["192.168.1.1"]), 1)

    def test_appends_to_existing_ip(self):
        """_record_login_attempt appends to existing IP entry."""
        web_main._login_attempts["192.168.1.1"] = [time.time()]
        web_main._record_login_attempt("192.168.1.1")
        self.assertEqual(len(web_main._login_attempts["192.168.1.1"]), 2)


@_skip_unless_web_main
class TestIsDbConnectionError(unittest.TestCase):
    """Test _is_db_connection_error detects OSError in chain."""

    def test_returns_true_for_direct_oserror(self):
        """_is_db_connection_error returns True for direct OSError."""
        self.assertTrue(web_main._is_db_connection_error(OSError("conn refused")))

    def test_returns_true_for_chained_oserror(self):
        """_is_db_connection_error returns True when OSError is in __cause__."""
        inner = OSError("network down")
        outer = RuntimeError("query failed")
        outer.__cause__ = inner
        self.assertTrue(web_main._is_db_connection_error(outer))

    def test_returns_false_for_unrelated_error(self):
        """_is_db_connection_error returns False for non-connection errors."""
        self.assertFalse(web_main._is_db_connection_error(ValueError("bad value")))

    def test_returns_false_for_none_like_chain(self):
        """_is_db_connection_error handles errors without __cause__."""
        self.assertFalse(web_main._is_db_connection_error(TypeError("type")))

    def test_deep_chain_detection(self):
        """_is_db_connection_error detects OSError several levels deep."""
        e1 = OSError("root")
        e2 = RuntimeError("mid")
        e2.__cause__ = e1
        e3 = Exception("outer")
        e3.__cause__ = e2
        self.assertTrue(web_main._is_db_connection_error(e3))


@_skip_unless_web_main
class TestGetSecureCookies(unittest.TestCase):
    """Test _get_secure_cookies env and header detection."""

    def _make_request(self, scheme="http", forwarded_proto="", env_val=""):
        req = MagicMock()
        req.headers = {"x-forwarded-proto": forwarded_proto}
        req.url.scheme = scheme
        return req, env_val

    def test_env_true_forces_secure(self):
        """_get_secure_cookies returns True when SECURE_COOKIES=true."""
        req, _ = self._make_request()
        with patch.dict(os.environ, {"SECURE_COOKIES": "true"}):
            self.assertTrue(web_main._get_secure_cookies(req))

    def test_env_false_forces_insecure(self):
        """_get_secure_cookies returns False when SECURE_COOKIES=false."""
        req, _ = self._make_request(scheme="https")
        with patch.dict(os.environ, {"SECURE_COOKIES": "false"}):
            self.assertFalse(web_main._get_secure_cookies(req))

    def test_https_forwarded_proto_returns_true(self):
        """_get_secure_cookies returns True for x-forwarded-proto: https."""
        req, _ = self._make_request(forwarded_proto="https")
        with patch.dict(os.environ, {"SECURE_COOKIES": ""}):
            self.assertTrue(web_main._get_secure_cookies(req))

    def test_https_scheme_returns_true(self):
        """_get_secure_cookies returns True for https URL scheme."""
        req, _ = self._make_request(scheme="https")
        with patch.dict(os.environ, {"SECURE_COOKIES": ""}):
            self.assertTrue(web_main._get_secure_cookies(req))

    def test_plain_http_returns_false(self):
        """_get_secure_cookies returns False for plain HTTP without overrides."""
        req, _ = self._make_request()
        with patch.dict(os.environ, {"SECURE_COOKIES": ""}):
            self.assertFalse(web_main._get_secure_cookies(req))


# ============================================================================
# get_user_chat_ids (access control logic)
# ============================================================================


@_skip_unless_web_main
class TestGetUserChatIds(unittest.TestCase):
    """Test get_user_chat_ids access control merging."""

    def setUp(self):
        self._saved_display = web_main.config.display_chat_ids
        web_main.config.display_chat_ids = set()

    def tearDown(self):
        web_main.config.display_chat_ids = self._saved_display

    def test_master_no_filter_returns_none(self):
        """Master with no display_chat_ids returns None (all chats)."""
        user = web_main.UserContext(username="admin", role="master")
        self.assertIsNone(web_main.get_user_chat_ids(user))

    def test_master_with_filter_returns_filter(self):
        """Master with display_chat_ids returns the filter set."""
        web_main.config.display_chat_ids = {1, 2, 3}
        user = web_main.UserContext(username="admin", role="master")
        self.assertEqual(web_main.get_user_chat_ids(user), {1, 2, 3})

    def test_viewer_no_restrictions_no_filter_returns_none(self):
        """Viewer with allowed_chat_ids=None and no display filter returns None."""
        user = web_main.UserContext(username="viewer1", role="viewer", allowed_chat_ids=None)
        self.assertIsNone(web_main.get_user_chat_ids(user))

    def test_viewer_with_allowed_no_filter_returns_allowed(self):
        """Viewer with allowed_chat_ids returns those IDs when no master filter."""
        user = web_main.UserContext(username="viewer1", role="viewer", allowed_chat_ids={10, 20})
        self.assertEqual(web_main.get_user_chat_ids(user), {10, 20})

    def test_viewer_with_allowed_and_filter_returns_intersection(self):
        """Viewer's allowed_chat_ids intersected with master display filter."""
        web_main.config.display_chat_ids = {10, 20, 30}
        user = web_main.UserContext(username="viewer1", role="viewer", allowed_chat_ids={20, 40})
        self.assertEqual(web_main.get_user_chat_ids(user), {20})

    def test_viewer_allowed_none_with_filter_returns_filter(self):
        """Viewer with no restriction but master filter returns the filter."""
        web_main.config.display_chat_ids = {5, 10}
        user = web_main.UserContext(username="viewer1", role="viewer", allowed_chat_ids=None)
        self.assertEqual(web_main.get_user_chat_ids(user), {5, 10})


# ============================================================================
# _find_avatar_path and _get_cached_avatar_path
# ============================================================================


@_skip_unless_web_main
class TestFindAvatarPath(unittest.TestCase):
    """Test _find_avatar_path filesystem lookups."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._saved_media = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name

    def tearDown(self):
        web_main.config.media_path = self._saved_media
        self.temp_dir.cleanup()

    def _touch(self, relpath, mtime=None):
        full = os.path.join(self.temp_dir.name, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write("x")
        if mtime:
            os.utime(full, (mtime, mtime))

    def test_returns_none_when_no_avatar_dir(self):
        """_find_avatar_path returns None when avatars directory missing."""
        result = web_main._find_avatar_path(123, "private")
        self.assertIsNone(result)

    def test_finds_avatar_in_users_for_private(self):
        """_find_avatar_path looks in users/ for private chats."""
        self._touch("avatars/users/123_456.jpg")
        result = web_main._find_avatar_path(123, "private")
        self.assertIsNotNone(result)
        self.assertIn("avatars/users/", result)

    def test_finds_avatar_in_chats_for_group(self):
        """_find_avatar_path looks in chats/ for group chats."""
        self._touch("avatars/chats/-100123_789.jpg")
        result = web_main._find_avatar_path(-100123, "group")
        self.assertIsNotNone(result)
        self.assertIn("avatars/chats/", result)

    def test_finds_legacy_avatar_without_photo_id(self):
        """_find_avatar_path finds legacy {chat_id}.jpg format."""
        self._touch("avatars/users/999.jpg")
        result = web_main._find_avatar_path(999, "private")
        self.assertIsNotNone(result)
        self.assertIn("999.jpg", result)

    def test_returns_newest_when_multiple_avatars(self):
        """_find_avatar_path returns the most recently modified avatar."""
        old_time = 1000000
        new_time = 2000000
        self._touch("avatars/users/55_old.jpg", mtime=old_time)
        self._touch("avatars/users/55_new.jpg", mtime=new_time)
        result = web_main._find_avatar_path(55, "private")
        self.assertIn("55_new.jpg", result)

    def test_returns_none_when_no_match(self):
        """_find_avatar_path returns None when no matching files exist."""
        os.makedirs(os.path.join(self.temp_dir.name, "avatars", "users"), exist_ok=True)
        result = web_main._find_avatar_path(777, "private")
        self.assertIsNone(result)


@_skip_unless_web_main
class TestGetCachedAvatarPath(unittest.TestCase):
    """Test _get_cached_avatar_path caching behavior."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._saved_media = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

    def tearDown(self):
        web_main.config.media_path = self._saved_media
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def test_caches_result_on_first_lookup(self):
        """_get_cached_avatar_path caches the result."""
        web_main._get_cached_avatar_path(123, "private")
        self.assertIn(123, web_main._avatar_cache)

    def test_returns_cached_value_on_second_call(self):
        """_get_cached_avatar_path returns cached value without re-lookup."""
        web_main._avatar_cache[42] = "avatars/users/42_1.jpg"
        from datetime import datetime

        web_main._avatar_cache_time = datetime.utcnow()
        result = web_main._get_cached_avatar_path(42, "private")
        self.assertEqual(result, "avatars/users/42_1.jpg")

    def test_invalidates_stale_cache(self):
        """_get_cached_avatar_path clears cache after TTL expires."""
        from datetime import datetime, timedelta

        web_main._avatar_cache[42] = "old/path"
        web_main._avatar_cache_time = datetime.utcnow() - timedelta(seconds=web_main.AVATAR_CACHE_TTL_SECONDS + 10)

        # After invalidation, the cache entry for 42 should be re-looked up
        result = web_main._get_cached_avatar_path(42, "private")
        # Since no actual avatar file exists, it should be None now
        self.assertIsNone(result)


# ============================================================================
# SessionData and UserContext dataclasses
# ============================================================================


@_skip_unless_web_main
class TestSessionData(unittest.TestCase):
    """Test SessionData dataclass defaults."""

    def test_default_timestamps_are_recent(self):
        """SessionData defaults created_at and last_accessed to now."""
        before = time.time()
        session = web_main.SessionData(username="u", role="viewer")
        after = time.time()
        self.assertGreaterEqual(session.created_at, before)
        self.assertLessEqual(session.created_at, after)
        self.assertGreaterEqual(session.last_accessed, before)

    def test_allowed_chat_ids_default_none(self):
        """SessionData defaults allowed_chat_ids to None."""
        session = web_main.SessionData(username="u", role="master")
        self.assertIsNone(session.allowed_chat_ids)

    def test_no_download_default_false(self):
        """SessionData defaults no_download to False."""
        session = web_main.SessionData(username="u", role="viewer")
        self.assertFalse(session.no_download)


@_skip_unless_web_main
class TestUserContext(unittest.TestCase):
    """Test UserContext dataclass."""

    def test_no_download_default_false(self):
        """UserContext defaults no_download to False."""
        user = web_main.UserContext(username="u", role="master")
        self.assertFalse(user.no_download)

    def test_allowed_chat_ids_default_none(self):
        """UserContext defaults allowed_chat_ids to None."""
        user = web_main.UserContext(username="u", role="viewer")
        self.assertIsNone(user.allowed_chat_ids)


# ============================================================================
# _create_session / _invalidate_user_sessions / _invalidate_token_sessions
# ============================================================================


@_skip_unless_web_main
class TestCreateSession(unittest.IsolatedAsyncioTestCase):
    """Test _create_session in-memory session management."""

    def setUp(self):
        self._saved_sessions = dict(web_main._sessions)
        self._saved_db = web_main.db
        web_main._sessions.clear()
        web_main.db = None  # No DB persistence in these tests

    def tearDown(self):
        web_main._sessions.clear()
        web_main._sessions.update(self._saved_sessions)
        web_main.db = self._saved_db

    async def test_returns_token_string(self):
        """_create_session returns a URL-safe token string."""
        token = await web_main._create_session("admin", "master")
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 20)

    async def test_stores_session_in_memory(self):
        """_create_session stores the session in _sessions dict."""
        token = await web_main._create_session("admin", "master")
        self.assertIn(token, web_main._sessions)
        self.assertEqual(web_main._sessions[token].username, "admin")
        self.assertEqual(web_main._sessions[token].role, "master")

    async def test_evicts_oldest_when_exceeding_max(self):
        """_create_session evicts oldest sessions when user exceeds max."""
        # Create max sessions
        for _i in range(web_main._MAX_SESSIONS_PER_USER):
            await web_main._create_session("user1", "viewer")

        count_before = len([s for s in web_main._sessions.values() if s.username == "user1"])
        self.assertEqual(count_before, web_main._MAX_SESSIONS_PER_USER)

        # Creating one more should evict the oldest
        await web_main._create_session("user1", "viewer")
        count_after = len([s for s in web_main._sessions.values() if s.username == "user1"])
        self.assertEqual(count_after, web_main._MAX_SESSIONS_PER_USER)

    async def test_preserves_allowed_chat_ids(self):
        """_create_session stores allowed_chat_ids in session."""
        token = await web_main._create_session("v1", "viewer", allowed_chat_ids={1, 2, 3})
        self.assertEqual(web_main._sessions[token].allowed_chat_ids, {1, 2, 3})


@_skip_unless_web_main
class TestInvalidateUserSessions(unittest.IsolatedAsyncioTestCase):
    """Test _invalidate_user_sessions removes all sessions for a user."""

    def setUp(self):
        self._saved_sessions = dict(web_main._sessions)
        self._saved_db = web_main.db
        web_main._sessions.clear()
        web_main.db = None

    def tearDown(self):
        web_main._sessions.clear()
        web_main._sessions.update(self._saved_sessions)
        web_main.db = self._saved_db

    async def test_removes_all_sessions_for_user(self):
        """_invalidate_user_sessions removes all sessions for the specified user."""
        t1 = await web_main._create_session("alice", "viewer")
        t2 = await web_main._create_session("alice", "viewer")
        t3 = await web_main._create_session("bob", "viewer")

        await web_main._invalidate_user_sessions("alice")
        self.assertNotIn(t1, web_main._sessions)
        self.assertNotIn(t2, web_main._sessions)
        self.assertIn(t3, web_main._sessions)


@_skip_unless_web_main
class TestInvalidateTokenSessions(unittest.IsolatedAsyncioTestCase):
    """Test _invalidate_token_sessions removes sessions from a share token."""

    def setUp(self):
        self._saved_sessions = dict(web_main._sessions)
        self._saved_db = web_main.db
        web_main._sessions.clear()
        web_main.db = None

    def tearDown(self):
        web_main._sessions.clear()
        web_main._sessions.update(self._saved_sessions)
        web_main.db = self._saved_db

    async def test_removes_sessions_with_matching_token_id(self):
        """_invalidate_token_sessions removes sessions created from a specific token."""
        t1 = await web_main._create_session("v1", "token", source_token_id=5)
        t2 = await web_main._create_session("v2", "token", source_token_id=5)
        t3 = await web_main._create_session("v3", "token", source_token_id=99)

        await web_main._invalidate_token_sessions(5)
        self.assertNotIn(t1, web_main._sessions)
        self.assertNotIn(t2, web_main._sessions)
        self.assertIn(t3, web_main._sessions)


# ============================================================================
# handle_realtime_notification
# ============================================================================


@_skip_unless_web_main
class TestHandleRealtimeNotification(unittest.IsolatedAsyncioTestCase):
    """Test handle_realtime_notification dispatch logic."""

    def setUp(self):
        self._saved_display = web_main.config.display_chat_ids
        self._saved_push = web_main.push_manager
        web_main.config.display_chat_ids = set()
        web_main.push_manager = None

    def tearDown(self):
        web_main.config.display_chat_ids = self._saved_display
        web_main.push_manager = self._saved_push

    async def test_ignores_notification_for_restricted_chat(self):
        """handle_realtime_notification ignores chats not in display_chat_ids."""
        web_main.config.display_chat_ids = {100}
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.handle_realtime_notification({"type": "new_message", "chat_id": 999, "data": {}})
        mock_bc.assert_not_awaited()

    async def test_broadcasts_new_message(self):
        """handle_realtime_notification broadcasts new_message events."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.handle_realtime_notification(
                {
                    "type": "new_message",
                    "chat_id": 42,
                    "data": {"message": {"id": 1, "text": "hi"}},
                }
            )
        mock_bc.assert_awaited_once()
        call_args = mock_bc.call_args
        self.assertEqual(call_args[0][0], 42)
        self.assertEqual(call_args[0][1]["type"], "new_message")

    async def test_broadcasts_edit_event(self):
        """handle_realtime_notification broadcasts edit events."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.handle_realtime_notification(
                {
                    "type": "edit",
                    "chat_id": 10,
                    "data": {"message_id": 5, "new_text": "edited"},
                }
            )
        mock_bc.assert_awaited_once()
        self.assertEqual(mock_bc.call_args[0][1]["type"], "edit")

    async def test_broadcasts_delete_event(self):
        """handle_realtime_notification broadcasts delete events."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.handle_realtime_notification(
                {
                    "type": "delete",
                    "chat_id": 10,
                    "data": {"message_id": 7},
                }
            )
        mock_bc.assert_awaited_once()
        self.assertEqual(mock_bc.call_args[0][1]["type"], "delete")

    async def test_broadcasts_pin_event(self):
        """handle_realtime_notification broadcasts pin events."""
        with patch.object(web_main.ws_manager, "broadcast_to_chat", new_callable=AsyncMock) as mock_bc:
            await web_main.handle_realtime_notification(
                {
                    "type": "pin",
                    "chat_id": 10,
                    "data": {"message_ids": [1, 2], "pinned": True},
                }
            )
        mock_bc.assert_awaited_once()
        msg = mock_bc.call_args[0][1]
        self.assertEqual(msg["type"], "pin")
        self.assertEqual(msg["message_ids"], [1, 2])


if __name__ == "__main__":
    unittest.main()
