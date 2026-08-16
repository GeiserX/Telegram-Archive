"""Tests for #261 (?download=1 forces attachment) and #258 (URL round-tripping).

Both defects made a media file unreachable or unsaveable from the viewer:
- serve_media accepted a ``download`` query param and ignored it, so the gallery's
  download button just opened/played the file inline.
- Server-built media URLs used to carry the filename and needed per-segment
  percent-encoding; since v8.0 the URL is ``/media/{chat_ref}/{message_id}_{type}``
  resolved through the media row, so the filename never enters a URL at all.
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

CHAT_REF = "dispositionRef1001AB0A"


def _reload_main(media_root=None):
    """Reload src.web.main under anonymous auth and return (client, module)."""
    import src.web.main as main_mod

    importlib.reload(main_mod)
    main_mod.db = AsyncMock()
    if media_root is not None:
        main_mod._media_root = main_mod.Path(media_root).resolve()
    return TestClient(main_mod.app, raise_server_exceptions=False), main_mod


def _wire_chat_and_media(main_mod, filename: str, chat_id: int = -1001, folder: str | None = None):
    """Point the mock db at one chat (ref CHAT_REF) holding one media row."""
    main_mod.db.get_chat_by_ref = AsyncMock(
        return_value={"id": chat_id, "account_id": 1, "ref": CHAT_REF, "type": "channel"}
    )
    main_mod.db.get_media_by_id = AsyncMock(
        return_value={
            "id": f"{chat_id}_5_photo",
            "file_path": f"{folder or chat_id}/{filename}",
            "file_name": filename,
        }
    )


class TestDownloadDisposition:
    """#261: ?download=1 must emit Content-Disposition: attachment."""

    def _serve(self, filename: str, query: str = ""):
        with tempfile.TemporaryDirectory() as tmpdir:
            chat_dir = os.path.join(tmpdir, "-1001")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, filename), "wb") as handle:
                handle.write(b"bytes")
            with patch.dict(os.environ, ANON_ENV):
                client, main_mod = _reload_main(media_root=tmpdir)
                _wire_chat_and_media(main_mod, filename)
                return client.get(f"/media/{CHAT_REF}/5_photo{query}")

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
                _wire_chat_and_media(main_mod, "photo.jpg")
                main_mod.AUTH_ENABLED = True
                token = "no-download-token"
                main_mod._sessions[token] = main_mod.SessionData(username="viewer1", role="viewer", no_download=True)
                resp = client.get(f"/media/{CHAT_REF}/5_photo?download=1", cookies={"viewer_auth": token})
                assert resp.status_code == 403


class TestMediaUrlEncoding:
    """#258's successor: media URLs are ref+key addressed, so the filename —
    the only attacker-influenced string — never enters a URL. What remains to
    pin is that key URLs are encoded defensively and that a hostile filename
    still round-trips URL → row → bytes."""

    def test_encode_helper_escapes_reserved_chars(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            _, main_mod = _reload_main()
        # Server-minted keys are untouched; a reserved character would be escaped.
        assert main_mod._encode_media_key("5_video_note") == "5_video_note"
        assert main_mod._encode_media_key("5_a#b?c") == "5_a%23b%3Fc"

    def test_gallery_urls_carry_the_key_not_the_filename(self) -> None:
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            _wire_chat_and_media(main_mod, "we#1 who? are.jpg")
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
            resp = client.get(f"/api/chats/{CHAT_REF}/media")

        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["id"] == "5_photo"
        assert item["media_url"] == f"/media/{CHAT_REF}/5_photo"
        assert item["thumb_url"] == f"/media/thumb/200/{CHAT_REF}/5_photo"
        assert "we%231" not in item["media_url"] and "#" not in item["media_url"]

    def test_plain_filenames_produce_the_same_key_urls(self) -> None:
        """The URL shape is filename-independent — the gallery round-trips item.id."""
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            _wire_chat_and_media(main_mod, "photo_123.jpg")
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
            resp = client.get(f"/api/chats/{CHAT_REF}/media")

        item = resp.json()["items"][0]
        assert item["media_url"] == f"/media/{CHAT_REF}/5_photo"
        assert item["thumb_url"] == f"/media/thumb/200/{CHAT_REF}/5_photo"

    def test_chat_avatar_url_is_ref_addressed(self) -> None:
        """The avatar URL names the chat's ref only — never the on-disk avatar file."""
        with patch.dict(os.environ, ANON_ENV):
            client, main_mod = _reload_main()
            main_mod.db.get_all_chats = AsyncMock(
                return_value=[{"id": -1001, "account_id": 1, "ref": CHAT_REF, "title": "Chat A", "type": "channel"}]
            )
            main_mod.db.get_chat_count = AsyncMock(return_value=1)
            main_mod.db.get_archived_chat_count = AsyncMock(return_value=0)
            with patch.object(main_mod, "_get_cached_avatar_path", return_value="avatars/chats/-1001_a#b.jpg"):
                resp = client.get("/api/chats")

        assert resp.status_code == 200
        assert resp.json()["chats"][0]["avatar_url"] == f"/media/avatar/{CHAT_REF}"

    def test_sender_avatar_url_is_ref_and_message_addressed(self) -> None:
        """Message payloads point at /media/avatar/{ref}/{message_id}: no user id,
        no filename (for a private chat the peer's user id IS the chat id)."""
        with patch.dict(os.environ, ANON_ENV):
            _, main_mod = _reload_main()
        chat = main_mod.ChatContext(account_id=1, chat_id=-1001, ref=CHAT_REF, type="channel")
        message = {"id": 7, "sender_id": 42}
        with patch.object(main_mod, "_get_cached_avatar_path", return_value="avatars/users/42_a?b.jpg"):
            main_mod._attach_message_payload_urls([message], chat)
        assert message["sender_avatar_url"] == f"/media/avatar/{CHAT_REF}/7"

    def test_thumb_url_resolves_folder_and_filename_from_the_row(self) -> None:
        """The key URL routes, and ensure_thumbnail receives the ROW's on-disk
        folder/filename — hostile characters and all — never URL segments."""
        ensure = AsyncMock(return_value=None)
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, ANON_ENV),
            patch("src.web.thumbnails.ensure_thumbnail", ensure),
        ):
            client, main_mod = _reload_main(media_root=tmpdir)
            _wire_chat_and_media(main_mod, "we#1 who? are.jpg")
            resp = client.get(f"/media/thumb/200/{CHAT_REF}/5_photo")

        assert resp.status_code == 404, "route matched but no thumbnail exists — 404 is the expected miss"
        assert ensure.await_count == 1
        _, size, folder, filename = ensure.await_args[0]
        assert (size, folder, filename) == (200, "-1001", "we#1 who? are.jpg")

    def test_hostile_filename_round_trips_through_serve_media(self) -> None:
        """A filename full of reserved characters is reachable through its key URL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chat_dir = os.path.join(tmpdir, "-1001")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, "we#1 who? are.jpg"), "wb") as handle:
                handle.write(b"jpegbytes")
            with patch.dict(os.environ, ANON_ENV):
                client, main_mod = _reload_main(media_root=tmpdir)
                _wire_chat_and_media(main_mod, "we#1 who? are.jpg")
                resp = client.get(f"/media/{CHAT_REF}/5_photo")

        assert resp.status_code == 200
        assert resp.content == b"jpegbytes"
