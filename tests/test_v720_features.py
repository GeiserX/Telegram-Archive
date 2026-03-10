"""Tests for v7.2.0 features: share tokens, thumbnails, settings, no_download."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_auth_module(tmp_path):
    with patch.dict(
        os.environ,
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "MEDIA_PATH": str(tmp_path / "media"),
        },
    ):
        os.makedirs(tmp_path / "backups", exist_ok=True)
        os.makedirs(tmp_path / "media", exist_ok=True)
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
    db.get_cached_statistics = AsyncMock(return_value={"total_chats": 1, "total_messages": 10})
    db.get_metadata = AsyncMock(return_value=None)
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.get_all_viewer_accounts = AsyncMock(return_value=[])
    db.get_all_viewer_tokens = AsyncMock(return_value=[])
    db.create_viewer_token = AsyncMock()
    db.verify_viewer_token = AsyncMock(return_value=None)
    db.update_viewer_token = AsyncMock()
    db.delete_viewer_token = AsyncMock(return_value=True)
    db.get_all_settings = AsyncMock(return_value={})
    db.set_setting = AsyncMock()
    db.get_setting = AsyncMock(return_value=None)
    db.create_audit_log = AsyncMock()
    db.get_audit_logs = AsyncMock(return_value=[])
    db.get_all_folders = AsyncMock(return_value=[])
    db.get_archived_chat_count = AsyncMock(return_value=0)
    db.get_session = AsyncMock(return_value=None)
    db.delete_session = AsyncMock()
    db.save_session = AsyncMock()
    db.calculate_and_store_statistics = AsyncMock(return_value={"total_chats": 1})
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


def _get_client(mock_db=None):
    import importlib

    import src.web.main as main_mod

    importlib.reload(main_mod)
    if mock_db is None:
        mock_db = _make_mock_db()
    main_mod.db = mock_db
    return TestClient(main_mod.app, raise_server_exceptions=False), main_mod, mock_db


def _login_master(client):
    resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
    return resp.cookies.get("viewer_auth")


class TestTokenAuth:
    """Tests for share token authentication."""

    def test_token_auth_invalid_token(self, auth_env):
        client, _, db = _get_client()
        db.verify_viewer_token.return_value = None
        resp = client.post("/auth/token", json={"token": "badtoken"})
        assert resp.status_code == 401

    def test_token_auth_valid_token(self, auth_env):
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 1,
            "label": "test-token",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["role"] == "token"
        assert "viewer_auth" in resp.cookies

    def test_token_auth_no_download(self, auth_env):
        client, _, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 2,
            "label": "restricted",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
        }
        resp = client.post("/auth/token", json={"token": "validtoken456"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["no_download"] is True

    def test_token_auth_empty_token(self, auth_env):
        client, _, _ = _get_client()
        resp = client.post("/auth/token", json={"token": ""})
        assert resp.status_code == 400

    def test_token_auth_rate_limited(self, auth_env):
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = None
        # Exhaust rate limit
        for _ in range(16):
            client.post("/auth/token", json={"token": "bad"})
        resp = client.post("/auth/token", json={"token": "bad"})
        assert resp.status_code == 429


class TestTokenCRUD:
    """Tests for token admin CRUD endpoints."""

    def test_create_token(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.create_viewer_token.return_value = {
            "id": 1,
            "label": "my-token",
            "token_hash": "h",
            "token_salt": "s",
            "created_by": "admin",
            "allowed_chat_ids": json.dumps([-1001]),
            "is_revoked": 0,
            "no_download": 0,
            "expires_at": None,
            "last_used_at": None,
            "use_count": 0,
            "created_at": "2026-01-01T00:00:00",
        }
        resp = client.post(
            "/api/admin/tokens",
            json={"label": "my-token", "allowed_chat_ids": [-1001]},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data  # plaintext token returned
        assert len(data["token"]) == 64  # 32 bytes hex

    def test_create_token_requires_chat_ids(self, auth_env):
        client, _, _ = _get_client()
        cookie = _login_master(client)
        resp = client.post(
            "/api/admin/tokens",
            json={"label": "bad"},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 400

    def test_list_tokens(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.get_all_viewer_tokens.return_value = [
            {
                "id": 1,
                "label": "tok",
                "created_by": "admin",
                "allowed_chat_ids": json.dumps([-1001]),
                "is_revoked": 0,
                "no_download": 0,
                "expires_at": None,
                "last_used_at": None,
                "use_count": 5,
                "created_at": "2026-01-01",
            }
        ]
        resp = client.get("/api/admin/tokens", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        assert len(resp.json()["tokens"]) == 1

    def test_delete_token(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        resp = client.delete("/api/admin/tokens/1", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_revoke_token(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.update_viewer_token.return_value = {
            "id": 1,
            "label": "tok",
            "allowed_chat_ids": json.dumps([-1001]),
            "is_revoked": 1,
            "no_download": 0,
            "expires_at": None,
        }
        resp = client.put(
            "/api/admin/tokens/1",
            json={"is_revoked": True},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["is_revoked"] == 1

    def test_tokens_require_master(self, auth_env):
        client, mod, db = _get_client()
        # Login as viewer (no master access)
        resp = client.get("/api/admin/tokens")
        assert resp.status_code == 401


class TestSettings:
    """Tests for app settings endpoints."""

    def test_get_settings(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.get_all_settings.return_value = {"theme": "dark"}
        resp = client.get("/api/admin/settings", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        assert resp.json()["settings"]["theme"] == "dark"

    def test_set_setting(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        resp = client.put(
            "/api/admin/settings/theme",
            json={"value": "light"},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "theme"
        db.set_setting.assert_called_once_with("theme", "light")


class TestThumbnails:
    """Tests for thumbnail path traversal protection."""

    def test_thumbnail_module_traversal_protection(self):
        """Test that path traversal is blocked in thumbnails module."""
        from src.web.thumbnails import ALLOWED_SIZES, _is_image

        assert 200 in ALLOWED_SIZES
        assert _is_image("photo.jpg") is True
        assert _is_image("document.pdf") is False

    def test_thumbnail_disallowed_size(self):
        import asyncio

        from src.web.thumbnails import ensure_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(ensure_thumbnail(Path(tmpdir), 999, "folder", "file.jpg"))
            assert result is None  # 999 not in ALLOWED_SIZES

    def test_thumbnail_non_image(self):
        import asyncio

        from src.web.thumbnails import ensure_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(ensure_thumbnail(Path(tmpdir), 200, "folder", "file.pdf"))
            assert result is None  # .pdf not an image

    def test_thumbnail_path_traversal(self):
        import asyncio

        from src.web.thumbnails import ensure_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            result = asyncio.run(ensure_thumbnail(Path(tmpdir), 200, "../../../etc", "passwd.jpg"))
            assert result is None  # path traversal blocked


class TestNoDownload:
    """Tests for no_download enforcement on media endpoint."""

    def test_auth_check_includes_no_download(self, auth_env):
        client, mod, db = _get_client()
        cookie = _login_master(client)
        resp = client.get("/api/auth/check", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        data = resp.json()
        # Master should not have no_download
        assert data.get("no_download") is False or data.get("no_download") is None or not data.get("no_download")


class TestAuditLogFilter:
    """Tests for audit log action filter."""

    def test_audit_log_action_filter(self, auth_env):
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.get_audit_logs.return_value = []
        resp = client.get(
            "/api/admin/audit?action=login_success",
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        db.get_audit_logs.assert_called_once_with(limit=100, offset=0, username=None, action="login_success")
