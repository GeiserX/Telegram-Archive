"""Tests for #261 (?download=1 forces attachment) and #258 (URL percent-encoding).

Both defects made a media file unreachable or unsaveable from the viewer:
- serve_media accepted a ``download`` query param and ignored it, so the gallery's
  download button just opened/played the file inline.
- Server-built media URLs were plain f-strings, so a Telegram filename containing
  "#" or "?" produced a URL that truncated into a fragment/query.
"""

import importlib
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

# Self-contained bootstrap (same pattern as tests/test_database_viewer.py): importing
# src.web.main builds a Config, which creates BACKUP_PATH — defaulting to the
# read-only "/data" and failing this whole module when it runs on its own.
os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

ANON_ENV = {
    "VIEWER_USERNAME": "",
    "VIEWER_PASSWORD": "",
    "ALLOW_ANONYMOUS_VIEWER": "true",
}


def _reload_main(media_root=None):
    """Reload src.web.main under anonymous auth and return (client, module)."""
    import src.web.main as main_mod

    importlib.reload(main_mod)
    main_mod.db = AsyncMock()
    if media_root is not None:
        main_mod._media_root = main_mod.Path(media_root).resolve()
    return TestClient(main_mod.app, raise_server_exceptions=False), main_mod


class TestDownloadDisposition:
    """#261: ?download=1 must emit Content-Disposition: attachment."""

    def _serve(self, filename: str, query: str = ""):
        with tempfile.TemporaryDirectory() as tmpdir:
            chat_dir = os.path.join(tmpdir, "-1001")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, filename), "wb") as handle:
                handle.write(b"bytes")
            with patch.dict(os.environ, ANON_ENV):
                client, _ = _reload_main(media_root=tmpdir)
                from urllib.parse import quote

                return client.get(f"/media/-1001/{quote(filename, safe='')}{query}")

    def test_download_flag_sets_attachment_disposition(self) -> None:
        resp = self._serve("clip.mp4", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith("attachment")
        assert "clip.mp4" in resp.headers["content-disposition"]

    def test_inline_by_default(self) -> None:
        """<img>/<video> use the same URL without the flag — must stay inline."""
        resp = self._serve("photo.jpg")
        assert resp.status_code == 200
        assert "attachment" not in resp.headers.get("content-disposition", "")

    def test_download_zero_stays_inline(self) -> None:
        resp = self._serve("photo.jpg", "?download=0")
        assert resp.status_code == 200
        assert "attachment" not in resp.headers.get("content-disposition", "")

    def test_download_keeps_media_type(self) -> None:
        """Forcing a download must not mislabel the bytes."""
        resp = self._serve("photo.jpg", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/jpeg")

    def test_quote_injecting_filename_is_escaped(self) -> None:
        """The filename is attacker-influenced (it is the Telegram document name)."""
        resp = self._serve('a"b;q=1.txt', "?download=1")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        # Starlette falls back to RFC 5987 whenever quoting changed the string, so
        # the raw quote never reaches the header value and cannot close it early.
        assert disposition.startswith("attachment; filename*=utf-8''")
        assert '"' not in disposition

    def test_download_name_is_the_display_name_not_the_storage_name(self) -> None:
        """On disk every file carries a uniqueness prefix; the save dialog must not."""
        resp = self._serve("123456789_holiday.jpg", "?download=1")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert disposition == 'attachment; filename="holiday.jpg"'
        assert "123456789" not in disposition

    def test_imported_media_storage_prefix_is_stripped_too(self) -> None:
        resp = self._serve("import_-1001_5_report.pdf", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="report.pdf"'

    def test_a_digit_first_component_is_stripped_like_the_gallery_label(self) -> None:
        """A leading digit run is indistinguishable from a storage prefix — both go.

        The saved name must equal the label the gallery shows for the same file,
        and the viewer's getMediaDisplayName strips ``^[0-9]+_`` unconditionally.
        """
        resp = self._serve("2026_report.pdf", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="report.pdf"'

    def test_name_without_any_prefix_is_saved_verbatim(self) -> None:
        """Only a leading prefix is removed — nothing else is rewritten."""
        resp = self._serve("holiday.jpg", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="holiday.jpg"'

    def test_only_the_first_prefix_is_stripped(self) -> None:
        resp = self._serve("77_2026_report.pdf", "?download=1")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"] == 'attachment; filename="2026_report.pdf"'

    def test_escaping_still_applies_after_the_prefix_is_stripped(self) -> None:
        """Stripping must not smuggle a dangerous byte past Starlette's quoting."""
        resp = self._serve('123_a"b;q=1.txt', "?download=1")
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment; filename*=utf-8''")
        assert '"' not in disposition
        assert "123_" not in disposition

    def test_crlf_filename_cannot_inject_a_header(self) -> None:
        """CRLF is escaped by Starlette's own quoting before it reaches the header.

        Driven at the FileResponse level: an ASGI client normalises %0D%0A out of
        the request path, so the router can never carry such a name.
        """
        from starlette.responses import FileResponse

        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "ok.txt")
            with open(target, "wb") as handle:
                handle.write(b"bytes")
            response = FileResponse(target, filename='a"b\r\nX-Injected: 1.txt')

        disposition = response.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert "x-injected" not in {key.lower() for key in response.headers}
        assert disposition.startswith("attachment; filename*=utf-8''")

    def test_no_download_account_still_blocked(self) -> None:
        """The 403 guard must fire before any disposition handling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chat_dir = os.path.join(tmpdir, "-1001")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, "photo.jpg"), "wb") as handle:
                handle.write(b"bytes")
            with patch.dict(
                os.environ,
                {"VIEWER_USERNAME": "admin", "VIEWER_PASSWORD": "test@value/here", "SECURE_COOKIES": "false"},
            ):
                client, main_mod = _reload_main(media_root=tmpdir)
                main_mod.AUTH_ENABLED = True
                token = "no-download-token"
                main_mod._sessions[token] = main_mod.SessionData(username="viewer1", role="viewer", no_download=True)
                resp = client.get("/media/-1001/photo.jpg?download=1", cookies={"viewer_auth": token})
                assert resp.status_code == 403


class TestMediaUrlEncoding:
    """#258: server-built media URLs must percent-encode each path segment."""

    def test_encode_helper_escapes_reserved_chars_per_segment(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            _, main_mod = _reload_main()
        encoded = main_mod._encode_media_path("-1001/we#1 who are? .jpg")
        assert encoded == "-1001/we%231%20who%20are%3F%20.jpg"
        assert encoded.count("/") == 1, "path separators must survive"

    def test_gallery_urls_encode_hash_and_question_mark(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            main_mod.db.get_media_paginated = AsyncMock(
                return_value={
                    "items": [
                        {
                            "id": "-1001_5_photo",
                            "message_id": 5,
                            "chat_id": -1001,
                            "type": "photo",
                            "file_path": "-1001/we#1 who? are.jpg",
                            "file_name": "we#1 who? are.jpg",
                        }
                    ],
                    "has_more": False,
                }
            )
            resp = client.get("/api/chats/-1001/media")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["media_url"] == "/media/-1001/we%231%20who%3F%20are.jpg"
        assert item["thumb_url"] == "/media/thumb/200/-1001/we%231%20who%3F%20are.jpg"

    def test_plain_filenames_are_unchanged(self) -> None:
        """Existing URLs must not shift — the gallery and tests round-trip them."""
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            main_mod.db.get_media_paginated = AsyncMock(
                return_value={
                    "items": [
                        {
                            "id": "-1001_5_photo",
                            "message_id": 5,
                            "chat_id": -1001,
                            "type": "photo",
                            "file_path": "-1001/photo_123.jpg",
                            "file_name": "photo_123.jpg",
                        }
                    ],
                    "has_more": False,
                }
            )
            resp = client.get("/api/chats/-1001/media")

        item = resp.json()["items"][0]
        assert item["media_url"] == "/media/-1001/photo_123.jpg"
        assert item["thumb_url"] == "/media/thumb/200/-1001/photo_123.jpg"

    def test_chat_avatar_url_is_encoded(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            main_mod.db.get_all_chats = AsyncMock(return_value=[{"id": -1001, "title": "Chat A", "type": "channel"}])
            main_mod.db.get_chat_count = AsyncMock(return_value=1)
            main_mod.db.get_archived_chat_count = AsyncMock(return_value=0)
            with patch.object(main_mod, "_get_cached_avatar_path", return_value="avatars/chats/-1001_a#b.jpg"):
                resp = client.get("/api/chats")

        assert resp.status_code == 200
        assert resp.json()["chats"][0]["avatar_url"] == "/media/avatars/chats/-1001_a%23b.jpg"

    def test_sender_avatar_url_is_encoded(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            _, main_mod = _reload_main()
        with patch.object(main_mod, "_get_cached_avatar_path", return_value="avatars/users/42_a?b.jpg"):
            assert main_mod._sender_avatar_url(42) == "/media/avatars/users/42_a%3Fb.jpg"

    def test_encoded_thumb_url_routes_with_decoded_segments(self) -> None:
        """The encoded thumb URL must still match /media/thumb/{size}/{folder:path}/{filename}."""
        ensure = AsyncMock(return_value=None)
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, ANON_ENV),
            patch("src.web.thumbnails.ensure_thumbnail", ensure),
        ):
            client, main_mod = _reload_main(media_root=tmpdir)
            encoded = main_mod._encode_media_path("-1001/we#1 who? are.jpg")
            resp = client.get(f"/media/thumb/200/{encoded}")

        assert resp.status_code == 404, "route matched but no thumbnail exists — 404 is the expected miss"
        assert ensure.await_count == 1
        _, size, folder, filename = ensure.await_args[0]
        assert (size, folder, filename) == (200, "-1001", "we#1 who? are.jpg")

    def test_encoded_url_round_trips_through_serve_media(self) -> None:
        """The encoded URL must resolve back to the same file on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chat_dir = os.path.join(tmpdir, "-1001")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, "we#1 who? are.jpg"), "wb") as handle:
                handle.write(b"jpegbytes")
            with patch.dict(os.environ, ANON_ENV):
                client, main_mod = _reload_main(media_root=tmpdir)
                encoded = main_mod._encode_media_path("-1001/we#1 who? are.jpg")
                resp = client.get(f"/media/{encoded}")

        assert resp.status_code == 200
        assert resp.content == b"jpegbytes"
