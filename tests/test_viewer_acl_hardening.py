"""Regression tests for the viewer's access-control and media-serving hardening.

Each class pins one defect found by the security audit of src/web/main.py:

- /ws/updates admitted credential-less sockets, and gave authenticated viewers a
  socket with NO chat ACL, whenever AUTH_PROXY_HEADER and VIEWER_USERNAME/
  VIEWER_PASSWORD were configured together.
- The thumbnail route authorized the raw request string while the file lookup
  used the joined path, so a percent-encoded ".." read another chat's media and
  bypassed no_download.
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
    return db


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
        token = "acl-ws-session"
        web_main._sessions[token] = web_main.SessionData(
            username="v1", role="viewer", allowed_chat_ids={1, 2}, created_at=time.time()
        )
        self.client.cookies.set("viewer_auth", token)

        with self.client.websocket_connect("/ws/updates") as socket:
            socket.send_json({"action": "subscribe", "chat_id": 1})
            self.assertEqual({"type": "subscribed", "chat_id": 1}, socket.receive_json())
            socket.send_json({"action": "subscribe", "chat_id": 999})
            self.assertEqual({"type": "subscribe_denied", "chat_id": 999}, socket.receive_json())

    def test_proxy_header_still_authenticates(self):
        """Control: the proxy path itself is untouched and still connects."""
        with self.client.websocket_connect("/ws/updates", headers={"X-Forwarded-User": "sso-admin@corp.com"}) as socket:
            socket.send_json({"action": "ping"})
            self.assertEqual({"type": "pong"}, socket.receive_json())


# ============================================================================
# Thumbnail route: percent-encoded ".." (the ACL string must be the read string)
# ============================================================================


@_skip_unless_web
class TestThumbnailPathTraversal(unittest.IsolatedAsyncioTestCase):
    """`%2e%2e` arrives decoded, so the folder the ACL reads must be the folder served."""

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

    async def test_encoded_dot_dot_cannot_reach_a_forbidden_chat(self):
        """The exploit: authorized on -1001, served from -1002."""
        cookies = self._session("tv-restricted", allowed_chat_ids={-1001})
        generated = AsyncMock(return_value=(Path(self.tmp.name) / "thumb.webp", "-1002"))
        with patch("src.web.thumbnails.ensure_thumbnail", generated):
            async with self._client() as client:
                resp = await client.get("/media/thumb/200/-1001/%2e%2e/-1002/secret.jpg", cookies=cookies)
        self.assertEqual(403, resp.status_code)
        # No file was even looked at: the request never reached generation.
        generated.assert_not_awaited()

    async def test_encoded_slash_spelling_is_refused_too(self):
        """`..%2f` decodes to the same traversal and must fail the same way."""
        cookies = self._session("tv-restricted-2", allowed_chat_ids={-1001})
        async with self._client() as client:
            resp = await client.get("/media/thumb/200/-1001/..%2f-1002/secret.jpg", cookies=cookies)
        self.assertEqual(403, resp.status_code)

    async def test_encoded_dot_dot_cannot_bypass_no_download(self):
        """`avatars/..` used to skip the no_download rule on its way to real media."""
        cookies = self._session("tv-nodl", allowed_chat_ids={-1001}, no_download=True)
        generated = AsyncMock(return_value=(Path(self.tmp.name) / "thumb.webp", "-1001"))
        with patch("src.web.thumbnails.ensure_thumbnail", generated):
            async with self._client() as client:
                resp = await client.get("/media/thumb/200/avatars/%2e%2e/-1001/private.jpg", cookies=cookies)
        self.assertEqual(403, resp.status_code)
        generated.assert_not_awaited()

    async def test_clean_path_in_an_allowed_chat_still_serves(self):
        """Control: the guard denies only requests that were already meant to be denied."""
        cookies = self._session("tv-allowed", allowed_chat_ids={-1001})
        thumb = Path(self.tmp.name) / "thumb.webp"
        thumb.write_bytes(b"\x00" * 8)
        with patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(return_value=(thumb, "-1001"))):
            async with self._client() as client:
                resp = await client.get("/media/thumb/200/-1001/allowed.jpg", cookies=cookies)
        self.assertEqual(200, resp.status_code)

    async def test_serve_media_still_refuses_traversal(self):
        """The sibling route shares the same guard now; it must not have regressed."""
        cookies = self._session("tv-media", allowed_chat_ids={-1001})
        async with self._client() as client:
            resp = await client.get("/media/-1001/%2e%2e/-1002/secret.jpg", cookies=cookies)
        self.assertEqual(403, resp.status_code)


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
        for socket in (first, leaving, staying):
            await manager.connect(socket)
            manager.subscribe(socket, 42)

        await manager.broadcast_to_chat(42, {"type": "new_message", "chat_id": 42})

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
        for socket in (first, leaving, staying):
            await manager.connect(socket)

        await manager.broadcast_to_all({"type": "ping"})

        self.assertEqual(1, len(staying.received))


# ============================================================================
# Media content type, disposition and cache directives
# ============================================================================


@_skip_unless_web
class TestMediaServingHeaders(unittest.IsolatedAsyncioTestCase):
    """Archived bytes are attacker-named: they must never become a live document."""

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
        self._saved_auth = web_main.AUTH_ENABLED
        self._saved_anon = web_main.ALLOW_ANONYMOUS_VIEWER
        self._saved_db = web_main.db
        web_main._media_root = self.root
        web_main.AUTH_ENABLED = False
        web_main.ALLOW_ANONYMOUS_VIEWER = True
        web_main.db = _mock_db()

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main.db = self._saved_db
        self.tmp.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def _get(self, name, query=""):
        (self.chat_dir / name).write_bytes(b"<script>archive.exfiltrate()</script>")
        async with self._client() as client:
            return await client.get(f"/media/-1001/{name}{query}")

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
        async with self._client() as client:
            resp = await client.get("/media/avatars/chats/-1001_7.jpg")
        self.assertEqual(200, resp.status_code)
        self.assertEqual("private, max-age=86400", resp.headers["cache-control"])

    async def test_thumbnail_cache_control_is_private(self):
        thumb = self.root / "thumb.webp"
        thumb.write_bytes(b"\x00" * 8)
        saved_cache_dir = web_main._thumb_cache_dir
        web_main._thumb_cache_dir = self.root / "thumbs"
        try:
            with patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(return_value=(thumb, "-1001"))):
                async with self._client() as client:
                    resp = await client.get("/media/thumb/200/-1001/77_photo.jpg")
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
    """A media URL is /media/<chat id>/<file id>_<the sender's file name>."""

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

    def tearDown(self):
        web_main._media_root = self._saved_root
        web_main.AUTH_ENABLED = self._saved_auth
        web_main.ALLOW_ANONYMOUS_VIEWER = self._saved_anon
        web_main._thumb_cache_dir = self._saved_cache
        web_main.db = self._saved_db
        self.tmp.cleanup()

    def _drive_failing_request(self, failure):
        client = TestClient(web_main.app, raise_server_exceptions=False)
        url = f"/media/thumb/200/{self.CHAT_FOLDER}/{self.FILE_NAME}"
        with (
            patch("src.web.thumbnails.ensure_thumbnail", AsyncMock(side_effect=failure)),
            self.assertLogs("src.web.main", level=logging.ERROR) as captured,
        ):
            response = client.get(url)
        # captured.output is the FORMATTED record — unlike getMessage(), it
        # appends the exc_info traceback, which is exactly where the leak hid.
        return response, "\n".join(captured.output)

    def test_database_error_branch_logs_no_identifiers(self):
        response, logged = self._drive_failing_request(OSError(20, "Not a directory", "/cache/thumbs"))
        self.assertEqual(503, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn("Maria", logged)
        self.assertNotIn("Errno", logged)
        self.assertIn("/media/thumb/{size}/{folder:path}/{filename}", logged)

    def test_unhandled_error_branch_logs_no_identifiers(self):
        response, logged = self._drive_failing_request(RuntimeError("thumbnail worker exploded"))
        self.assertEqual(500, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
        self.assertNotIn("Maria", logged)
        self.assertIn("/media/thumb/{size}/{folder:path}/{filename}", logged)
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
        self.assertNotIn("Maria", logged)
        self.assertNotIn("ffmpeg", logged)
        # Debuggability survives redaction: the type names the failure and the
        # frame list (file/line/function — never a runtime value) locates it.
        self.assertIn("TimeoutExpired", logged)
        self.assertIn('File "', logged)

    def test_oserror_with_a_media_path_stays_clean_on_the_503_branch(self):
        """An OSError carrying the media path classifies as a connection error;
        its 503 branch must keep refusing the exception text (which stringifies
        with the offending filename) and must never grow an exc_info."""
        attack = FileNotFoundError(2, "No such file or directory", f"/data/media/{self.CHAT_FOLDER}/{self.FILE_NAME}")
        response, logged = self._drive_failing_request(attack)
        self.assertEqual(503, response.status_code)
        self.assertNotIn(self.CHAT_FOLDER, logged)
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
        url = f"/media/thumb/200/{self.CHAT_FOLDER}/{self.FILE_NAME}"

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
        response, logged = self._drive(OSError(20, "Not a directory", "/cache/thumbs"))
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
        self.mock_db.get_media_paginated = AsyncMock(
            side_effect=lambda *a, **k: {"items": [{"id": 1, "file_path": "-1001/photo_123.jpg"}]}
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
            allowed_chat_ids={-1001},
            no_download=no_download,
            created_at=time.time(),
        )
        async with AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test") as client:
            resp = await client.get("/api/chats/-1001/media", cookies={"viewer_auth": token})
        self.assertEqual(200, resp.status_code)
        return resp.json()["items"][0]

    async def test_no_download_gallery_omits_the_thumbnail_url(self):
        item = await self._gallery("gal-nodl", no_download=True)
        self.assertIsNone(item["thumb_url"])
        self.assertNotIn("file_path", item)

    async def test_ordinary_viewer_still_gets_the_thumbnail_url(self):
        item = await self._gallery("gal-ok", no_download=False)
        self.assertEqual("/media/thumb/200/-1001/photo_123.jpg", item["thumb_url"])


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
                    json={"label": "backup", "allowed_chat_ids": [-1001]},
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
        sender_ids = list(range(1000, 1030))
        for sender_id in sender_ids:
            self._touch(f"{sender_id}_1.jpg")

        real_scandir = os.scandir
        scans = []

        def counting_scandir(path="."):
            scans.append(str(path))
            return real_scandir(path)

        with patch.object(web_main.os, "scandir", counting_scandir):
            urls = [web_main._sender_avatar_url(sender_id) for sender_id in sender_ids]

        self.assertEqual([f"/media/avatars/users/{sender_id}_1.jpg" for sender_id in sender_ids], urls)
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
