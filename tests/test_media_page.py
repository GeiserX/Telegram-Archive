"""Integration tests for media gallery endpoints (v7.10.0)."""

import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

# Self-contained bootstrap (same pattern as test_media_download_disposition.py):
# _get_client reloads src.web.main, whose Config creates BACKUP_PATH — defaulting
# to the read-only "/data" and erroring this module when it runs on its own.
os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_auth_module():
    """Reset auth module state between tests."""
    import src.web.main as main_mod

    main_mod._sessions.clear()
    main_mod._login_attempts.clear()
    yield
    main_mod._sessions.clear()
    main_mod._login_attempts.clear()


def _make_mock_db():
    db = AsyncMock()
    db.get_all_chats = AsyncMock(
        return_value=[
            {"id": -1001, "account_id": 1, "ref": "mediaChatRef01001ABC", "title": "Chat A", "type": "channel"},
        ]
    )
    # Echo any requested ref back as chat -1001, so both the anonymous tests
    # (which use "-1001" as the URL segment) and the restricted-viewer tests
    # (which grant a realistic ref) resolve to the same chat.
    db.get_chat_by_ref = AsyncMock(
        side_effect=lambda ref, **kwargs: {"id": -1001, "account_id": 1, "ref": ref, "type": "channel"}
    )
    db.get_chat_count = AsyncMock(return_value=1)
    db.get_cached_statistics = AsyncMock(return_value={"total_chats": 1})
    db.get_metadata = AsyncMock(return_value=None)
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.get_viewer_account = AsyncMock(return_value=None)
    db.get_all_viewer_accounts = AsyncMock(return_value=[])
    db.create_audit_log = AsyncMock()
    db.get_all_folders = AsyncMock(return_value=[])
    db.get_archived_chat_count = AsyncMock(return_value=0)
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    # Media-specific mocks (storage key id: "{chat_id}_{message_id}_{type}")
    db.get_media_paginated = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "-1001_100_photo",
                    "message_id": 100,
                    "chat_id": -1001,
                    "type": "photo",
                    "file_path": "-1001/photo_123.jpg",
                    "file_name": "photo_123.jpg",
                    "file_size": 245000,
                    "mime_type": "image/jpeg",
                    "width": 1920,
                    "height": 1080,
                    "duration": None,
                    "message_date": "2026-01-15T10:30:00",
                    "sender_name": "TestUser",
                },
            ],
            "has_more": False,
        }
    )
    db.get_media_counts = AsyncMock(
        return_value={
            "photo": 10,
            "video": 5,
            "animation": 2,
            "voice": 3,
            "document": 8,
        }
    )
    return db


@pytest.fixture
def auth_env():
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "admin",
            "VIEWER_PASSWORD": "testpass123",
            "AUTH_SESSION_DAYS": "1",
            "SECURE_COOKIES": "false",
        },
    ):
        yield


@pytest.fixture
def anon_env():
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "",
            "VIEWER_PASSWORD": "",
            "ALLOW_ANONYMOUS_VIEWER": "true",
        },
    ):
        yield


def _get_client(mock_db=None):
    """Create a fresh TestClient by reloading the module with current env."""
    import importlib

    import src.web.main as main_mod

    importlib.reload(main_mod)

    if mock_db is None:
        mock_db = _make_mock_db()
    main_mod.db = mock_db

    return TestClient(main_mod.app, raise_server_exceptions=False), main_mod, mock_db


def _login(client, username="admin", password="testpass123"):
    """Helper to login and get authenticated client."""
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return client


class TestMediaEndpointAuth:
    """Tests for media endpoint authentication requirements."""

    def test_requires_authentication(self, auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 401

    def test_works_when_authenticated(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/-1001/media", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200

    def test_works_anonymous_mode(self, anon_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200


class TestMediaPaginated:
    """Tests for paginated media list endpoint."""

    def test_returns_media_items(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "has_more" in data
        assert len(data["items"]) == 1
        # The storage key is rewritten to the chat-free URL key.
        assert data["items"][0]["id"] == "100_photo"

    def test_passes_types_filter(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?types=photo,video")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=["photo", "video"],
            limit=50,
            before_id=None,
            after_id=None,
            account_id=1,
        )

    def test_passes_limit(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?limit=20")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=20,
            before_id=None,
            after_id=None,
            account_id=1,
        )

    def test_passes_before_id(self, anon_env):
        """The chat-free cursor is reconstructed into the storage key server-side."""
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?before_id=90_photo")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=50,
            before_id="-1001_90_photo",
            after_id=None,
            account_id=1,
        )

    def test_passes_after_id(self, anon_env):
        """#266: the forward cursor is the CHAT-FREE key the endpoint returned as
        item.id; the chat id is re-attached from the resolved chat, so an
        old-format full-composite token reconstructs to a key that resolves to
        no row (an empty page, never someone else's page)."""
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?after_id=2428_voice")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=50,
            before_id=None,
            after_id="-1001_2428_voice",
            account_id=1,
        )

    def test_old_full_composite_cursor_reconstructs_to_no_row(self, anon_env):
        """A 7.x cursor ("-1001_2428_voice") gains a second chat prefix and can
        only ever miss — the #266 empty-page semantics."""
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?after_id=-1001_2428_voice")
        assert resp.status_code == 200
        called_after = mock_db.get_media_paginated.call_args.kwargs["after_id"]
        assert called_after == "-1001_-1001_2428_voice"

    def test_before_id_and_after_id_are_mutually_exclusive(self, anon_env):
        """#266: asking for both directions at once is rejected, not silently halved."""
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?before_id=a&after_id=b")
        assert resp.status_code == 400
        assert "mutually exclusive" in resp.json()["detail"]
        mock_db.get_media_paginated.assert_not_called()

    def test_empty_types_means_all(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=50,
            before_id=None,
            after_id=None,
            account_id=1,
        )

    def test_items_include_thumb_url(self, anon_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["thumb_url"] == "/media/thumb/200/-1001/100_photo"

    def test_no_download_strips_media_url(self, auth_env):
        import src.web.main as main_mod

        mock_db = _make_mock_db()
        salt = "abc123"
        pw_hash = main_mod._hash_password("vpass", salt)
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 1,
                "username": "restricted",
                "password_hash": pw_hash,
                "salt": salt,
                "allowed_chat_ids": "[]",  # rollback tombstone; the grant is the refs column
                "allowed_chat_refs": json.dumps(["mediaChatRef01001ABC"]),
                "is_active": 1,
                "no_download": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/mediaChatRef01001ABC/media", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert "media_url" not in data["items"][0]
        assert "file_path" not in data["items"][0]
        # serve_thumbnail refuses derived bytes for these accounts too.
        assert data["items"][0]["thumb_url"] is None


class TestMediaCounts:
    """Tests for media type counts endpoint."""

    def test_returns_counts(self, anon_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media/counts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["photo"] == 10
        assert data["video"] == 5
        assert data["animation"] == 2
        assert data["voice"] == 3
        assert data["document"] == 8

    def test_forbidden_for_restricted_user(self, auth_env):
        """A chat outside the viewer's ref grant answers the uniform 404."""
        import src.web.main as main_mod

        mock_db = _make_mock_db()
        salt = "abc123"
        pw_hash = main_mod._hash_password("vpass", salt)
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 1,
                "username": "restricted",
                "password_hash": pw_hash,
                "salt": salt,
                "allowed_chat_ids": "[]",
                "allowed_chat_refs": json.dumps(["someOtherChatRef1002"]),
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/mediaChatRef01001ABC/media/counts", cookies={"viewer_auth": cookie})
        assert resp.status_code == 404


class TestMediaPathValidation:
    """Tests for path traversal protection and URL generation."""

    def test_traversal_path_gets_null_thumb_url(self, anon_env):
        mock_db = _make_mock_db()
        mock_db.get_media_paginated = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "-1001_1_photo",
                        "message_id": 1,
                        "chat_id": -1001,
                        "type": "photo",
                        "file_path": "../../../etc/passwd",
                        "file_name": "passwd",
                        "file_size": 100,
                        "mime_type": "image/jpeg",
                        "width": 100,
                        "height": 100,
                        "duration": None,
                        "message_date": "2026-01-01T00:00:00",
                        "sender_name": "Attacker",
                    },
                ],
                "has_more": False,
            }
        )
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["thumb_url"] is None
        assert "file_path" not in data["items"][0]
        assert "media_url" not in data["items"][0]

    def test_absolute_path_gets_null_thumb_url(self, anon_env):
        mock_db = _make_mock_db()
        mock_db.get_media_paginated = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "-1001_2_photo",
                        "message_id": 2,
                        "chat_id": -1001,
                        "type": "photo",
                        "file_path": "/etc/shadow",
                        "file_name": "shadow",
                        "file_size": 100,
                        "mime_type": "text/plain",
                        "width": None,
                        "height": None,
                        "duration": None,
                        "message_date": "2026-01-01T00:00:00",
                        "sender_name": None,
                    },
                ],
                "has_more": False,
            }
        )
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["thumb_url"] is None
        assert "file_path" not in data["items"][0]

    def test_valid_path_includes_media_url(self, anon_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["media_url"] == "/media/-1001/100_photo"


class TestMediaACL:
    """Tests for media access control list enforcement."""

    def test_forbidden_chat_answers_the_uniform_404(self, auth_env):
        """A chat outside the ref grant is indistinguishable from a nonexistent one."""
        import src.web.main as main_mod

        mock_db = _make_mock_db()
        salt = "abc123"
        pw_hash = main_mod._hash_password("vpass", salt)
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 1,
                "username": "restricted",
                "password_hash": pw_hash,
                "salt": salt,
                "allowed_chat_ids": "[]",
                "allowed_chat_refs": json.dumps(["someOtherChatRef1002"]),
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/mediaChatRef01001ABC/media", cookies={"viewer_auth": cookie})
        assert resp.status_code == 404
        forbidden_body = resp.json()

        mock_db.get_chat_by_ref = AsyncMock(return_value=None)
        missing = client.get("/api/chats/mediaChatRef01001ABC/media", cookies={"viewer_auth": cookie})
        assert (missing.status_code, missing.json()) == (404, forbidden_body)
