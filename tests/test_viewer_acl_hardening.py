"""Regression tests for the viewer's access-control and media-serving hardening.

Each class pins one defect found by the security audit of src/web/main.py:

- /ws/updates admitted credential-less sockets, and gave authenticated viewers a
  socket with NO chat ACL, whenever AUTH_PROXY_HEADER and VIEWER_USERNAME/
  VIEWER_PASSWORD were configured together.
- The thumbnail route authorized the raw request string while the file lookup
  used the joined path, so a percent-encoded ".." read another chat's media and
  bypassed no_download. (Phase 4 media URLs are single-segment ref + key, so the
  URL variant now dies at routing; the row's file_path is the surface left.)
- broadcast_to_chat iterated the live connection dict across awaits.
- Archived .html/.svg documents were served inline as same-origin documents.
- Access-controlled media carried Cache-Control: public.
- The global exception handlers logged the request path (a chat id and the
  sender's file name), and exc_info on the 500 branch printed the exception's
  own text — a subprocess error's ffmpeg argv carries that same media path.
- The media gallery handed no_download viewers thumb_urls that always 403.
- Login and share-token creation ran a 600k-round PBKDF2 on the event loop.
- Avatars were resolved with one directory scan per id.
"""

import asyncio
import importlib
import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_acl_"))
    from src.web import main as web_main

    _WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on installs without fastapi
    _WEB_AVAILABLE = False
    web_main = None  # type: ignore[assignment]

try:
    from fastapi.testclient import TestClient
    from httpx import ASGITransport, AsyncClient
    from starlette.websockets import WebSocketDisconnect

    _CLIENTS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on installs without fastapi
    _CLIENTS_AVAILABLE = False


def _skip_unless_web(cls):
    return unittest.skipUnless(_WEB_AVAILABLE and _CLIENTS_AVAILABLE, "web_main/test client import failed")(cls)


def _mock_db():
    db = AsyncMock()
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    db.create_audit_log = AsyncMock()
    db.get_viewer_by_username = AsyncMock(return_value=None)
    # Phase 4: chat-scoped routes resolve their {chat_ref} through these two
    # reads. None = "no such chat/media", the fail-closed default.
    db.get_chat_by_ref = AsyncMock(return_value=None)
    db.get_media_by_id = AsyncMock(return_value=None)
    return db


def _chat_row(chat_id: int, ref: str, chat_type: str = "channel") -> dict:
    """A chats row dict of the shape get_chat_by_ref returns."""
    return {"id": chat_id, "account_id": 1, "ref": ref, "type": chat_type}


# ============================================================================
# /ws/updates authentication (proxy header + password auth configured together)
# ============================================================================

# The shape tests/test_proxy_auth.py calls proxy_with_basic_env: the documented
# combination where the WebSocket used to skip the cookie check entirely.
PROXY_WITH_BASIC_ENV = {
    "VIEWER_USERNAME": "admin",
    "VIEWER_PASSWORD": "testpass123",
    "AUTH_PROXY_HEADER": "X-Forwarded-User",
    "AUTH_PROXY_ADMIN_USERS": "sso-admin@corp.com",
    "AUTH_PROXY_DEFAULT_ACCESS": "none",
    "SECURE_COOKIES": "false",
}

NEUTRAL_ENV = {
    "VIEWER_USERNAME": "",
    "VIEWER_PASSWORD": "",
    "AUTH_PROXY_HEADER": "",
    "ALLOW_ANONYMOUS_VIEWER": "false",
}


@_skip_unless_web
class TestWebSocketAuthWithProxyAndPassword(unittest.TestCase):
    """A socket must belong to a principal, and carry that principal's chat ACL."""

    def setUp(self):
        with patch.dict(os.environ, PROXY_WITH_BASIC_ENV):
            importlib.reload(web_main)
        web_main.db = _mock_db()
        web_main._sessions.clear()
        self.client = TestClient(web_main.app, raise_server_exceptions=False)

    def tearDown(self):
        web_main._sessions.clear()
        # Leave the module in a neutral state so later test files are not
        # affected by this file's reload.
        with patch.dict(os.environ, NEUTRAL_ENV):
            importlib.reload(web_main)

    def test_config_under_test_is_the_reported_one(self):
        """Guard the guard: both auth mechanisms really are enabled here."""
        self.assertTrue(web_main.AUTH_ENABLED)
        self.assertTrue(web_main._PROXY_AUTH_ENABLED)

    def test_socket_without_any_credential_is_refused(self):
        """No cookie and no proxy header: the handshake must be closed, not accepted."""
        with self.assertRaises(WebSocketDisconnect) as caught, self.client.websocket_connect("/ws/updates"):
            pass
        self.assertEqual(4001, caught.exception.code)

    def test_authenticated_viewer_socket_keeps_its_chat_acl(self):
        """A restricted viewer must not be able to subscribe outside its allowed set."""
        allowed_ref = "wsAllowedRefwsAllowedR"
        forbidden_ref = "wsForbiddenRwsForbidde"
        # BOTH refs resolve to real chats: the denial below is entitlement,
        # not unknownness — and the two are indistinguishable on the wire.
        rows = {allowed_ref: _chat_row(1, allowed_ref), forbidden_ref: _chat_row(2, forbidden_ref)}
        web_main.db.get_chat_by_ref = AsyncMock(side_effect=lambda ref, **kwargs: rows.get(ref))
        token = "acl-ws-session"
        web_main._sessions[token] = web_main.SessionData(
            username="v1", role="viewer", allowed_chat_refs={allowed_ref}, created_at=time.time()
        )
        self.client.cookies.set("viewer_auth", token)

        with self.client.websocket_connect("/ws/updates") as socket:
            socket.send_json({"action": "subscribe", "chat_ref": allowed_ref})
            self.assertEqual({"type": "subscribed", "chat_ref": allowed_ref}, socket.receive_json())
            socket.send_json({"action": "subscribe", "chat_ref": forbidden_ref})
            self.assertEqual({"type": "subscribe_denied", "chat_ref": forbidden_ref}, socket.receive_json())

    def test_proxy_header_still_authenticates(self):
        """Control: the proxy path itself is untouched and still connects."""
        with self.client.websocket_connect("/ws/updates", headers={"X-Forwarded-User": "sso-admin@corp.com"}) as socket:
            socket.send_json({"action": "ping"})
            self.assertEqual({"type": "pong"}, socket.receive_json())


# ============================================================================
# Media traversal (the ref-addressed routes must keep the containment absolute)
# ============================================================================


@_skip_unless_web
class TestThumbnailPathTraversal(unittest.IsolatedAsyncioTestCase):
    """Traversal died at the URL; it must stay dead in the DB row too.

    Phase 4 media URLs are ``/media/{chat_ref}/{media_key}`` — single segments,
    so an encoded ``..`` can no longer splice extra path components into the
    request: those URLs stop matching any route at all. What CAN still carry a
    traversal is the media row's ``file_path`` (database content), because the
    bytes are now selected through the row. Both surfaces must refuse.
    """

    ALLOWED_REF = "thumbAllowedRefthumbAl"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_root = web_main._media_root
        self._saved_cache = web_main._thumb_cache_dir
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_db = web_main.db
        web_main._media_root = Path(self.tmp.name)
        web_main._thumb_cache_dir = Path(self.tmp.name) / "thumbs"
        web_main.AUTH_ENABLED = True
        web_main.db = _mock_db()
        web_main.db.get_chat_by_ref = AsyncMock(
            side_effect=lambda ref, **kwargs: _chat_row(-1001, self.ALLOWED_REF) if ref == self.ALLOWED_REF else None
        )
        web_main._sessions.clear()

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main._thumb_cache_dir = self._saved_cache
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.db = self._saved_db
        web_main._sessions.clear()
        self.tmp.cleanup()

    def _session(self, token, **kwargs):
        web_main._sessions[token] = web_main.SessionData(username="v1", role="viewer", created_at=time.time(), **kwargs)
        return {"viewer_auth": token}

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_encoded_dot_dot_urls_no_longer_route_at_all(self):
        """Every multi-segment traversal spelling dies before any code runs."""
        cookies = self._session("tv-restricted", allowed_chat_refs={self.ALLOWED_REF})
        generated = AsyncMock(return_value=(Path(self.tmp.name) / "thumb.webp", "-1002"))
        with patch("src.web.thumbnails.ensure_thumbnail", generated):
            async with self._client() as client:
                for url in (
                    f"/media/thumb/200/{self.ALLOWED_REF}/%2e%2e/-1002/secret.jpg",
                    f"/media/thumb/200/{self.ALLOWED_REF}/..%2f-1002/secret.jpg",
                    "/media/thumb/200/avatars/%2e%2e/-1001/private.jpg",
                    f"/media/{self.ALLOWED_REF}/%2e%2e/-1002/secret.jpg",
                ):
                    resp = await client.get(url, cookies=cookies)
                    self.assertEqual(404, resp.status_code, url)
        # No file was even looked at: the requests never reached generation.
        generated.assert_not_awaited()

    async def test_a_media_row_carrying_a_traversal_path_serves_nothing(self):
        """file_path is data; a ``..`` planted there must not select bytes."""
        cookies = self._session("tv-row", allowed_chat_refs={self.ALLOWED_REF})
        web_main.db.get_media_by_id = AsyncMock(
            return_value={"id": "-1001_9_photo", "file_path": "-1001/../-1002/secret.jpg", "file_name": "secret.jpg"}
        )
        generated = AsyncMock(return_value=(Path(self.tmp.name) / "thumb.webp", "-1001"))
        with patch("src.web.thumbnails.ensure_thumbnail", generated):
            async with self._client() as client:
                thumb = await client.get(f"/media/thumb/200/{self.ALLOWED_REF}/9_photo", cookies=cookies)
                media = await client.get(f"/media/{self.ALLOWED_REF}/9_photo", cookies=cookies)
        self.assertEqual(404, thumb.status_code)
        self.assertEqual(404, media.status_code)
        generated.assert_not_awaited()

    async def test_an_absolute_file_path_outside_the_root_serves_nothing(self):
        """Absolute stored paths are honoured ONLY under the media root."""
        cookies = self._session("tv-abs", allowed_chat_refs={self.ALLOWED_REF})
        outside = Path(self.tmp.name).parent / "outside-secret.jpg"
        web_main.db.get_media_by_id = AsyncMock(
            return_value={"id": "-1001_9_photo", "file_path": str(outside), "file_name": "outside-secret.jpg"}
        )
        async with self._client() as client:
            resp = await client.get(f"/media/{self.ALLOWED_REF}/9_photo", cookies=cookies)
        self.assertEqual(404, resp.status_code)

    async def test_traversal_predicates_themselves_still_hold(self):
        """The helpers the routes lean on keep refusing ``..`` and absolutes."""
        with self.assertRaises(web_main.HTTPException):
            web_main._checked_media_path("../secret.txt")
        with self.assertRaises(web_main.HTTPException):
            web_main._checked_media_path("/etc/passwd")
        self.assertIsNone(web_main._media_relative_path("-1001/../-1002/x.jpg"))
        self.assertIsNone(web_main._media_relative_path("/not/under/root.jpg"))

    async def test_no_download_cannot_reach_thumbnails(self):
        """The no_download rule fires before any row or file work."""
        cookies = self._session("tv-nodl", allowed_chat_refs={self.ALLOWED_REF}, no_download=True)
        generated = AsyncMock(return_value=(Path(self.tmp.name) / "thumb.webp", "-1001"))
        with patch("src.web.thumbnails.ensure_thumbnail", generated):
            async with self._client() as client:
                resp = await client.get(f"/media/thumb/200/{self.ALLOWED_REF}/9_photo", cookies=cookies)
        self.assertEqual(403, resp.status_code)
        generated.assert_not_awaited()

    async def test_clean_path_in_an_allowed_chat_still_serves(self):
        """Control: the guards deny only requests that were already meant to be denied."""
        cookies = self._session("tv-allowed", allowed_chat_refs={self.ALLOWED_REF})
        thumb = Path(self.tmp.name) / "thumb.webp"
        thumb.write_bytes(b"\x00" * 8)
        web_main.db.get_media_by_id = AsyncMock(
            return_value={"id": "-1001_9_photo", "file_path": "-1001/9_photo.jpg", "file_name": "9_photo.jpg"}
        )
        with patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(return_value=(thumb, "-1001"))):
            async with self._client() as client:
                resp = await client.get(f"/media/thumb/200/{self.ALLOWED_REF}/9_photo", cookies=cookies)
        self.assertEqual(200, resp.status_code)
        # The row was looked up under the resolved chat's account + storage key.
        web_main.db.get_media_by_id.assert_awaited_once_with("-1001_9_photo", account_id=1)


# ============================================================================
# Broadcast must survive a disconnect landing mid-send
# ============================================================================


class _FakeSocket:
    """Minimal stand-in for a Starlette WebSocket that can suspend inside send_json."""

    def __init__(self, on_send=None):
        self.received = []
        self._on_send = on_send

    async def accept(self):
        return None

    async def send_json(self, message):
        if self._on_send is not None:
            hook, self._on_send = self._on_send, None
            await hook()
        self.received.append(message)


@_skip_unless_web
class TestBroadcastSnapshot(unittest.IsolatedAsyncioTestCase):
    """One client leaving must not cancel the event for everyone else."""

    CHAT = {"id": 42, "account_id": 1, "ref": "broadcastRefbroadcastR"}

    def setUp(self):
        self._saved_display = web_main.config.display_chat_ids
        web_main.config.display_chat_ids = set()

    def tearDown(self):
        web_main.config.display_chat_ids = self._saved_display

    async def test_disconnect_during_send_still_reaches_the_remaining_clients(self):
        manager = web_main.ConnectionManager()
        leaving = _FakeSocket()
        staying = _FakeSocket()

        async def disconnect_mid_broadcast():
            # Exactly what websocket_endpoint's WebSocketDisconnect handler does,
            # from another task, while the broadcast is suspended on send_json.
            manager.disconnect(leaving)
            await asyncio.sleep(0)

        first = _FakeSocket(on_send=disconnect_mid_broadcast)
        user = web_main.UserContext(username="v1", role="viewer")
        for socket in (first, leaving, staying):
            await manager.connect(socket, user)
            manager.subscribe(socket, self.CHAT["ref"])

        await manager.broadcast_to_chat(self.CHAT, {"type": "new_message", "chat_ref": self.CHAT["ref"]})

        self.assertEqual(1, len(first.received))
        self.assertEqual(1, len(staying.received), "a client after the mutation point missed the broadcast")

    async def test_broadcast_to_all_survives_the_same_race(self):
        manager = web_main.ConnectionManager()
        leaving = _FakeSocket()
        staying = _FakeSocket()

        async def disconnect_mid_broadcast():
            manager.disconnect(leaving)
            await asyncio.sleep(0)

        first = _FakeSocket(on_send=disconnect_mid_broadcast)
        user = web_main.UserContext(username="v1", role="viewer")
        for socket in (first, leaving, staying):
            await manager.connect(socket, user)

        await manager.broadcast_to_all({"type": "ping"})

        self.assertEqual(1, len(staying.received))


# ============================================================================
# Media content type, disposition and cache directives
# ============================================================================


@_skip_unless_web
class TestMediaServingHeaders(unittest.IsolatedAsyncioTestCase):
    """Archived bytes are attacker-named: they must never become a live document."""

    REF = "mediaHdrRefmediaHdrRef"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # serve_media compares the resolved file against _media_root, and the
        # module resolves media_path at import; a temp dir behind a symlink
        # (macOS /var) would fail that check for reasons unrelated to this test.
        self.root = Path(self.tmp.name).resolve()
        self.chat_dir = self.root / "-1001"
        self.chat_dir.mkdir()
        (self.root / "avatars" / "chats").mkdir(parents=True)
        (self.root / "avatars" / "chats" / "-1001_7.jpg").write_bytes(b"\xff\xd8\xff")
        self._saved_root = web_main._media_root
        self._saved_media_path = web_main.config.media_path
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_anon = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_db = web_main.db
        web_main._media_root = self.root
        web_main.config.media_path = str(self.root)
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main.db = _mock_db()
        web_main.db.get_chat_by_ref = AsyncMock(
            side_effect=lambda ref, **kwargs: _chat_row(-1001, self.REF) if ref == self.REF else None
        )
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._avatar_dir_index.clear()

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main.config.media_path = self._saved_media_path
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main.db = self._saved_db
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._avatar_dir_index.clear()
        self.tmp.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def _get(self, name, query=""):
        # ``name`` doubles as the URL media key: ``77_report.html`` parses as
        # message 77, type "report.html", and the media row's file_path picks
        # the actual bytes — the phase-4 shape, where the row selects the file.
        (self.chat_dir / name).write_bytes(b"<script>archive.exfiltrate()</script>")
        web_main.db.get_media_by_id = AsyncMock(
            return_value={"id": f"-1001_{name}", "file_path": f"-1001/{name}", "file_name": name}
        )
        async with self._client() as client:
            return await client.get(f"/media/{self.REF}/{name}{query}")

    async def test_archived_html_is_a_download_not_a_document(self):
        resp = await self._get("77_report.html")
        self.assertEqual(200, resp.status_code)
        self.assertNotIn("text/html", resp.headers["content-type"])
        self.assertEqual("application/octet-stream", resp.headers["content-type"])
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))
        self.assertEqual("nosniff", resp.headers["x-content-type-options"])

    async def test_archived_svg_is_a_download_too(self):
        """An <img> cannot run an SVG's script, but navigating to the URL can."""
        resp = await self._get("77_logo.svg")
        self.assertEqual("application/octet-stream", resp.headers["content-type"])
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))

    async def test_archived_xhtml_is_a_download_too(self):
        resp = await self._get("77_page.xhtml")
        self.assertEqual("application/octet-stream", resp.headers["content-type"])
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))

    async def test_real_media_still_renders_inline(self):
        """Control: the types the viewer renders inline keep their type and stay inline."""
        for name, expected in (("77_photo.jpg", "image/jpeg"), ("77_clip.mp4", "video/mp4")):
            resp = await self._get(name)
            self.assertEqual(200, resp.status_code)
            self.assertTrue(resp.headers["content-type"].startswith(expected), name)
            self.assertNotIn("attachment", resp.headers.get("content-disposition", ""), name)

    async def test_media_is_never_stored_by_a_shared_cache(self):
        resp = await self._get("77_photo.jpg")
        self.assertIn("private", resp.headers["cache-control"])
        self.assertNotIn("public", resp.headers["cache-control"])

    async def test_avatar_cache_control_is_private(self):
        # The chat's avatar is addressed by ref alone; the id-addressed
        # /media/avatars/... shape no longer routes.
        async with self._client() as client:
            resp = await client.get(f"/media/avatar/{self.REF}")
            legacy = await client.get("/media/avatars/chats/-1001_7.jpg")
        self.assertEqual(200, resp.status_code)
        self.assertEqual("private, max-age=86400", resp.headers["cache-control"])
        self.assertEqual(404, legacy.status_code)

    async def test_thumbnail_cache_control_is_private(self):
        thumb = self.root / "thumb.webp"
        thumb.write_bytes(b"\x00" * 8)
        web_main.db.get_media_by_id = AsyncMock(
            return_value={"id": "-1001_77_photo", "file_path": "-1001/77_photo.jpg", "file_name": "77_photo.jpg"}
        )
        saved_cache_dir = web_main._thumb_cache_dir
        web_main._thumb_cache_dir = self.root / "thumbs"
        try:
            with patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(return_value=(thumb, "-1001"))):
                async with self._client() as client:
                    resp = await client.get(f"/media/thumb/200/{self.REF}/77_photo")
        finally:
            web_main._thumb_cache_dir = saved_cache_dir
        self.assertEqual(200, resp.status_code)
        self.assertIn("private", resp.headers["cache-control"])
        self.assertNotIn("public", resp.headers["cache-control"])


# ============================================================================
# The global exception handlers must not log the request path
# ============================================================================


@_skip_unless_web
class TestExceptionHandlerRedaction(unittest.TestCase):
    """A media key still ends with the sender's own file name — it must not be logged.

    The chat id has left the URL (the ref addresses the chat), but the media
    key carries the sender's filename and a hostile client can put ANY string in
    either segment, so the redaction rule is unchanged: log the route template,
    never the request values.
    """

    CHAT_REF = "redactChatRefredactCha"
    CHAT_FOLDER = "-1001234567890"
    FILE_NAME = "555_Maria Invoice.jpg"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_root = web_main._media_root
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_anon = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_cache = web_main._thumb_cache_dir
        self._saved_db = web_main.db
        web_main._media_root = Path(self.tmp.name)
        web_main._thumb_cache_dir = Path(self.tmp.name) / "thumbs"
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main.db = _mock_db()
        # The failure is planted INSIDE the handler (ensure_thumbnail), so the
        # resolver and the media row lookup must both succeed first.
        web_main.db.get_chat_by_ref = AsyncMock(return_value=_chat_row(-1001234567890, self.CHAT_REF))
        web_main.db.get_media_by_id = AsyncMock(
            return_value={
                "id": f"{self.CHAT_FOLDER}_{self.FILE_NAME}",
                "file_path": f"{self.CHAT_FOLDER}/{self.FILE_NAME}",
                "file_name": self.FILE_NAME,
            }
        )

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main._thumb_cache_dir = self._saved_cache
        web_main.db = self._saved_db
        self.tmp.cleanup()

    def _drive_failing_request(self, failure):
        client = TestClient(web_main.app, raise_server_exceptions=False)
        url = f"/media/thumb/200/{self.CHAT_REF}/{self.FILE_NAME}"
        with (
            patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(side_effect=failure)),
            self.assertLogs("src.web.main", level=logging.ERROR) as captured,
        ):
            response = client.get(url)
        # captured.output is the FORMATTED record — unlike getMessage(), it
        # appends the exc_info traceback, which is exactly where the leak hid.
        return response, "\n".join(captured.output)

    def test_database_error_branch_logs_no_identifiers(self):
        response, logged = self._drive_failing_request(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(503, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn(self.CHAT_REF, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("Errno", logged)
        self.assertIn("/media/thumb/{size}/{chat_ref}/{media_key}", logged)

    def test_unhandled_error_branch_logs_no_identifiers(self):
        response, logged = self._drive_failing_request(RuntimeError("thumbnail worker exploded"))
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn(self.CHAT_REF, logged)
        self.assertNotIn("Maria", logged)
        self.assertIn("/media/thumb/{size}/{chat_ref}/{media_key}", logged)
        # The class and message of a non-path exception stay: that is the diagnostic.
        self.assertIn("RuntimeError", logged)

    def test_unhandled_error_traceback_cannot_carry_the_ffmpeg_argv(self):
        """The attack: a subprocess error stringifies with the full ffmpeg argv.

        describe_exception already refuses that message, but exc_info=True on the
        500 branch re-printed it as the traceback's last line — chat id, sender
        file name and all. The formatted log record must carry neither.
        """
        attack = subprocess.TimeoutExpired(
            ["ffmpeg", "-y", "-i", f"/data/media/{self.CHAT_FOLDER}/{self.FILE_NAME}", "-frames:v", "1", "t.jpg"],
            10,
        )
        response, logged = self._drive_failing_request(attack)
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn(self.CHAT_REF, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("ffmpeg", logged)
        # Debuggability survives redaction: the type names the failure and the
        # frame list (file/line/function — never a runtime value) locates it.
        self.assertIn("TimeoutExpired", logged)
        self.assertIn('File "', logged)

    def test_oserror_with_a_media_path_stays_clean_on_the_500_branch(self):
        """A filesystem OSError carrying the media path is no longer classified
        as a database outage; its 500 branch must keep refusing the exception
        text (which stringifies with the offending filename)."""
        attack = FileNotFoundError(2, "No such file or directory", f"/data/media/{self.CHAT_FOLDER}/{self.FILE_NAME}")
        response, logged = self._drive_failing_request(attack)
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn(self.CHAT_REF, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("Errno", logged)
        self.assertIn("FileNotFoundError", logged)


# ============================================================================
# The unhandled exception must never reach the ASGI server (uvicorn)
# ============================================================================


@_skip_unless_web
class TestUnhandledExceptionNeverReachesTheServer(unittest.TestCase):
    """Redacting the app logger was necessary but not sufficient.

    After the app's handler runs, Starlette's ServerErrorMiddleware re-raises the
    exception (errors.py: ``raise exc``) and uvicorn's run_asgi then logs
    "Exception in ASGI application" with exc_info UNCONDITIONALLY. That traceback
    ends with the exception's own str(), and a thumbnail failure raises
    subprocess.TimeoutExpired whose argv is the ffmpeg command — a media path
    carrying the chat id and the sender's file name. RedactingErrorMiddleware
    must catch the exception, answer 500/503 WITHOUT re-raising (so it never
    propagates out of the ASGI app), and leak nothing.
    """

    CHAT_REF = "reraiseChatRefreraiseC"
    CHAT_FOLDER = "-1001234567890"
    FILE_NAME = "555_Maria Invoice.jpg"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_root = web_main._media_root
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_anon = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_cache = web_main._thumb_cache_dir
        self._saved_db = web_main.db
        web_main._media_root = Path(self.tmp.name)
        web_main._thumb_cache_dir = Path(self.tmp.name) / "thumbs"
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main.db = _mock_db()
        # Resolve the ref and the media row so the request reaches the planted
        # ensure_thumbnail failure — the row's file_path is the PII-bearing path.
        web_main.db.get_chat_by_ref = AsyncMock(return_value=_chat_row(-1001234567890, self.CHAT_REF))
        web_main.db.get_media_by_id = AsyncMock(
            return_value={
                "id": f"{self.CHAT_FOLDER}_{self.FILE_NAME}",
                "file_path": f"{self.CHAT_FOLDER}/{self.FILE_NAME}",
                "file_name": self.FILE_NAME,
            }
        )

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main._thumb_cache_dir = self._saved_cache
        web_main.db = self._saved_db
        self.tmp.cleanup()

    def _drive(self, failure):
        # raise_server_exceptions=True is the in-process stand-in for uvicorn's
        # run_asgi: if the exception escapes the ASGI app, TestClient re-raises it
        # here — exactly the condition under which uvicorn would log the traceback
        # with exc_info. So "this call returned a response" == "uvicorn never saw
        # it". Before the middleware, this same call raised the exception.
        client = TestClient(web_main.app, raise_server_exceptions=True)
        url = f"/media/thumb/200/{self.CHAT_REF}/{self.FILE_NAME}"

        # Capture BOTH 'src.web.main' and 'uvicorn.error' by attaching a handler
        # to the ROOT logger (both propagate there), and FORMAT each record so an
        # exc_info traceback — if any code path ever attached one — would show up
        # in the captured text, which is exactly where the leak would hide.
        captured = []

        class _Capture(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        handler = _Capture()
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(side_effect=failure)):
                response = client.get(url)
        finally:
            root.removeHandler(handler)
        return response, "\n".join(captured)

    def test_ffmpeg_timeout_is_answered_not_reraised(self):
        """The exact assigned exploit: a TimeoutExpired whose argv is the media path."""
        attack = subprocess.TimeoutExpired(
            ["ffmpeg", "-y", "-i", f"/data/media/{self.CHAT_FOLDER}/{self.FILE_NAME}", "-frames:v", "1", "t.jpg"],
            10,
        )
        # If the middleware re-raised, client.get would raise TimeoutExpired and
        # this test would ERROR before reaching a single assertion.
        response, logged = self._drive(attack)
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("ffmpeg", logged)
        # Debuggability survives: the type names the failure, the frames locate it.
        self.assertIn("TimeoutExpired", logged)
        self.assertIn('File "', logged)

    def test_db_connection_error_still_answers_503_without_reraise(self):
        """The 503-for-DB branch keeps its status and its redaction under the middleware."""
        response, logged = self._drive(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(503, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("Errno", logged)

    def test_generic_error_answers_500_without_reraise(self):
        """Control: an ordinary exception is answered 500 and keeps its diagnostic type."""
        response, logged = self._drive(RuntimeError("thumbnail worker exploded"))
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn("Maria", logged)
        self.assertIn("RuntimeError", logged)


# ============================================================================
# Media gallery: no_download sessions
# ============================================================================


@_skip_unless_web
class TestNoDownloadGalleryThumbnails(unittest.IsolatedAsyncioTestCase):
    """A URL the route puts in its own response must be fetchable by its recipient."""

    REF = "galleryRefgalleryRefga"

    def setUp(self):
        self._saved_db = web_main.db
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_anon = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_display = web_main.config.display_chat_ids
        web_main.AUTH_ENABLED = True
        web_main.ALLOW_ANONYMOUS_VIEWER = False
        web_main.config.display_chat_ids = set()
        web_main._sessions.clear()
        self.mock_db = _mock_db()
        self.mock_db.get_chat_by_ref = AsyncMock(return_value=_chat_row(-1001, self.REF))
        self.mock_db.get_media_paginated = AsyncMock(
            side_effect=lambda *a, **k: {"items": [{"id": "-1001_123_photo", "file_path": "-1001/photo_123.jpg"}]}
        )
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.db = self._saved_db
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main.config.display_chat_ids = self._saved_display
        web_main._sessions.clear()

    async def _gallery(self, token, no_download):
        web_main._sessions[token] = web_main.SessionData(
            username="v1",
            role="viewer",
            allowed_chat_refs={self.REF},
            no_download=no_download,
            created_at=time.time(),
        )
        async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
            resp = await client.get(f"/api/chats/{self.REF}/media", cookies={"viewer_auth": token})
        self.assertEqual(200, resp.status_code)
        return resp.json()["items"][0]

    async def test_no_download_gallery_omits_the_thumbnail_url(self):
        item = await self._gallery("gal-nodl", no_download=True)
        self.assertIsNone(item["thumb_url"])
        self.assertNotIn("file_path", item)
        # No original-bytes URL either: no_download strips both.
        self.assertNotIn("media_url", item)

    async def test_ordinary_viewer_still_gets_the_thumbnail_url(self):
        item = await self._gallery("gal-ok", no_download=False)
        # The gallery id is the chat-free cursor key; both URLs ride the ref.
        self.assertEqual("123_photo", item["id"])
        self.assertEqual(f"/media/thumb/200/{self.REF}/123_photo", item["thumb_url"])
        self.assertEqual(f"/media/{self.REF}/123_photo", item["media_url"])


# ============================================================================
# Password hashing must not run on the event loop
# ============================================================================


@_skip_unless_web
class TestLoginHashingOffTheEventLoop(unittest.IsolatedAsyncioTestCase):
    """600k rounds of PBKDF2 inline would stall every other request for its duration."""

    def setUp(self):
        self._saved_db = web_main.db
        self._saved_auth = web_main.AUTH_ENABLED
        web_main.AUTH_ENABLED = True
        web_main._sessions.clear()
        web_main._login_attempts.clear()
        self.mock_db = _mock_db()
        self.mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "username": "v1",
                "is_active": 1,
                "salt": "salt-value",
                "password_hash": "hash-value",
                "allowed_chat_ids": None,
                "no_download": 0,
            }
        )
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.db = self._saved_db
        web_main.AUTH_ENABLED = self._saved_auth
        web_main._sessions.clear()
        web_main._login_attempts.clear()

    async def test_verify_password_runs_in_a_worker_thread(self):
        hashed_on = []

        def recording_verify(password, salt, password_hash):
            hashed_on.append(threading.get_ident())
            return True

        loop_thread = threading.get_ident()
        with patch.object(web_main, "_verify_password", recording_verify):
            async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
                resp = await client.post("/api/login", json={"username": "v1", "password": "test@value/here"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual(1, len(hashed_on))
        self.assertNotEqual(loop_thread, hashed_on[0], "PBKDF2 ran on the event loop thread")


@_skip_unless_web
class TestTokenHashingOffTheEventLoop(unittest.IsolatedAsyncioTestCase):
    """create_token kept a fourth inline PBKDF2 after the login/viewer sites moved."""

    def setUp(self):
        self._saved_db = web_main.db
        self._saved_auth = web_main.AUTH_ENABLED
        web_main.AUTH_ENABLED = True
        web_main._sessions.clear()
        self.mock_db = _mock_db()
        self.mock_db.create_viewer_token = AsyncMock(
            side_effect=lambda **kwargs: {
                "id": 7,
                "label": kwargs.get("label"),
                "no_download": kwargs.get("no_download", 0),
                "expires_at": None,
                "created_at": "2026-01-01T00:00:00",
            }
        )
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.db = self._saved_db
        web_main.AUTH_ENABLED = self._saved_auth
        web_main._sessions.clear()

    async def test_token_hash_runs_in_a_worker_thread(self):
        hashed_on = []

        def recording_hash(plaintext_token, salt):
            hashed_on.append(threading.get_ident())
            return "feedface" * 8

        web_main._sessions["master-tok"] = web_main.SessionData(username="admin", role="master")
        loop_thread = threading.get_ident()
        with patch.object(web_main, "_hash_token", recording_hash):
            async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/admin/tokens",
                    json={"label": "backup", "allowed_chat_refs": ["tokenGrantRefTokenGran"]},
                    cookies={"viewer_auth": "master-tok"},
                )

        self.assertEqual(200, resp.status_code)
        self.assertEqual(1, len(hashed_on), "create_token no longer routes through _hash_token")
        self.assertNotEqual(loop_thread, hashed_on[0], "token PBKDF2 ran on the event loop thread")


# ============================================================================
# Avatar resolution costs one directory read, not one per id
# ============================================================================


@_skip_unless_web
class TestAvatarLookupScans(unittest.TestCase):
    """A page of N senders used to trigger N full scans of the avatars folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users_dir = Path(self.tmp.name) / "avatars" / "users"
        self.users_dir.mkdir(parents=True)
        self._saved_media_path = web_main.config.media_path
        web_main.config.media_path = self.tmp.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._avatar_dir_index.clear()

    def tearDown(self):
        web_main.config.media_path = self._saved_media_path
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._avatar_dir_index.clear()
        self.tmp.cleanup()

    def _touch(self, name, mtime=None):
        path = self.users_dir / name
        path.write_bytes(b"x")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_a_page_of_senders_reads_the_folder_once(self):
        # _sender_avatar_url is gone (sender avatars are addressed by chat ref +
        # message id now); the per-sender lookup the page renderer performs is
        # _get_cached_avatar_path, and it must still cost one directory read.
        sender_ids = list(range(1000, 1030))
        for sender_id in sender_ids:
            self._touch(f"{sender_id}_1.jpg")

        real_scandir = os.scandir
        scans = []

        def counting_scandir(path="."):
            scans.append(str(path))
            return real_scandir(path)

        with patch.object(web_main.os, "scandir", counting_scandir):
            paths = [web_main._get_cached_avatar_path(sender_id, "private") for sender_id in sender_ids]

        self.assertEqual([f"avatars/users/{sender_id}_1.jpg" for sender_id in sender_ids], paths)
        self.assertEqual(1, len([s for s in scans if str(self.users_dir) in s]))

    def test_a_new_avatar_is_picked_up_without_waiting(self):
        """The listing is keyed on the folder's own mtime, so it cannot go stale."""
        self.assertIsNone(web_main._find_avatar_path(2001, "private"))
        self._touch("2001_5.jpg")
        self.assertEqual("avatars/users/2001_5.jpg", web_main._find_avatar_path(2001, "private"))

    def test_newest_avatar_still_wins_and_a_deleted_one_is_dropped(self):
        self._touch("2002_old.jpg", mtime=1_000_000)
        newest = self._touch("2002_new.jpg", mtime=2_000_000)
        self.assertEqual("avatars/users/2002_new.jpg", web_main._find_avatar_path(2002, "private"))

        newest.unlink()
        self.assertEqual("avatars/users/2002_old.jpg", web_main._find_avatar_path(2002, "private"))


if __name__ == "__main__":
    unittest.main()
