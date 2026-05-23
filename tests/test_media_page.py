"""Integration tests for media gallery endpoints (v7.10.0)."""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

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
            {"id": -1001, "title": "Chat A", "type": "channel"},
        ]
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
    # Media-specific mocks
    db.get_media_paginated = AsyncMock(
        return_value={
            "items": [
                {
                    "id": "file_abc123",
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
        assert data["items"][0]["id"] == "file_abc123"

    def test_passes_types_filter(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?types=photo,video")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=["photo", "video"],
            limit=50,
            before_id=None,
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
        )

    def test_passes_before_id(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media?before_id=abc")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=50,
            before_id="abc",
        )

    def test_empty_types_means_all(self, anon_env):
        client, _, mock_db = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        mock_db.get_media_paginated.assert_called_once_with(
            -1001,
            media_types=None,
            limit=50,
            before_id=None,
        )

    def test_items_include_thumb_url(self, anon_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats/-1001/media")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["thumb_url"] == "/media/thumb/200/-1001/photo_123.jpg"

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
                "allowed_chat_ids": json.dumps([-1001]),
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
        resp = client.get("/api/chats/-1001/media", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert "media_url" not in data["items"][0]
        assert "file_path" not in data["items"][0]


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
                "allowed_chat_ids": json.dumps([-1002]),
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/-1001/media/counts", cookies={"viewer_auth": cookie})
        assert resp.status_code == 403


class TestMediaPathValidation:
    """Tests for path traversal protection and URL generation."""

    def test_traversal_path_gets_null_thumb_url(self, anon_env):
        mock_db = _make_mock_db()
        mock_db.get_media_paginated = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "file_evil",
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

    def test_absolute_path_gets_null_thumb_url(self, anon_env):
        mock_db = _make_mock_db()
        mock_db.get_media_paginated = AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "file_abs",
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
        assert data["items"][0]["media_url"] == "/media/-1001/photo_123.jpg"


class TestMediaACL:
    """Tests for media access control list enforcement."""

    def test_forbidden_chat_returns_403(self, auth_env):
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
                "allowed_chat_ids": json.dumps([-1002]),
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats/-1001/media", cookies={"viewer_auth": cookie})
        assert resp.status_code == 403
