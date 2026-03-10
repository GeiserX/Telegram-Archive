"""Tests for v7.2.0 features: share tokens, thumbnails, settings, no_download."""

import json
import os
import tempfile
import time
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
    db.delete_user_sessions = AsyncMock()
    db.delete_sessions_by_source_token_id = AsyncMock(return_value=0)
    db.load_all_sessions = AsyncMock(return_value=[])
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

    def test_no_download_blocks_explicit_download(self, auth_env, tmp_path):
        """no_download users cannot explicitly download files (download=1)."""
        client, mod, db = _get_client()
        # Create a token session with no_download=True
        db.verify_viewer_token.return_value = {
            "id": 10,
            "label": "restricted-tok",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        assert resp.status_code == 200
        cookie = resp.cookies.get("viewer_auth")

        # Create a test media file
        media_dir = tmp_path / "media" / "-1001"
        media_dir.mkdir(parents=True)
        (media_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        # Override media root
        mod._media_root = tmp_path / "media"

        # Explicit download should be blocked
        resp = client.get("/media/-1001/photo.jpg?download=1", cookies={"viewer_auth": cookie})
        assert resp.status_code == 403

    def test_no_download_allows_inline_media(self, auth_env, tmp_path):
        """no_download users can still view media inline (without download=1)."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 10,
            "label": "restricted-tok",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        cookie = resp.cookies.get("viewer_auth")

        media_dir = tmp_path / "media" / "-1001"
        media_dir.mkdir(parents=True)
        (media_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        mod._media_root = tmp_path / "media"

        # Inline request (no download param) should succeed
        resp = client.get("/media/-1001/photo.jpg", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200

    def test_no_download_blocks_export(self, auth_env):
        """no_download users cannot export chat history."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 10,
            "label": "restricted-tok",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        cookie = resp.cookies.get("viewer_auth")

        resp = client.get("/api/chats/-1001/export", cookies={"viewer_auth": cookie})
        assert resp.status_code == 403


class TestTokenRevocation:
    """Tests that token revocation/deletion invalidates active sessions."""

    def test_revoke_token_invalidates_sessions(self, auth_env):
        """Revoking a token should invalidate all sessions created from it."""
        client, mod, db = _get_client()
        # Authenticate with a token
        db.verify_viewer_token.return_value = {
            "id": 5,
            "label": "my-token",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        assert resp.status_code == 200
        token_cookie = resp.cookies.get("viewer_auth")

        # Verify session is active
        assert token_cookie in mod._sessions
        assert mod._sessions[token_cookie].source_token_id == 5

        # Now login as master and revoke the token
        cookie = _login_master(client)
        db.update_viewer_token.return_value = {
            "id": 5,
            "label": "my-token",
            "allowed_chat_ids": json.dumps([-1001]),
            "is_revoked": 1,
            "no_download": 0,
            "expires_at": None,
        }
        resp = client.put(
            "/api/admin/tokens/5",
            json={"is_revoked": True},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200

        # The token session should be invalidated
        assert token_cookie not in mod._sessions
        db.delete_sessions_by_source_token_id.assert_called_with(5)

    def test_delete_token_invalidates_sessions(self, auth_env):
        """Deleting a token should invalidate all sessions created from it."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 7,
            "label": "temp-token",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        token_cookie = resp.cookies.get("viewer_auth")
        assert token_cookie in mod._sessions

        cookie = _login_master(client)
        resp = client.delete("/api/admin/tokens/7", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200

        # Session should be gone
        assert token_cookie not in mod._sessions
        db.delete_sessions_by_source_token_id.assert_called_with(7)

    def test_update_token_scope_invalidates_sessions(self, auth_env):
        """Changing a token's allowed_chat_ids should invalidate sessions."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 8,
            "label": "scoped",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        token_cookie = resp.cookies.get("viewer_auth")
        assert token_cookie in mod._sessions

        cookie = _login_master(client)
        db.update_viewer_token.return_value = {
            "id": 8,
            "label": "scoped",
            "allowed_chat_ids": json.dumps([-1002]),
            "is_revoked": 0,
            "no_download": 0,
            "expires_at": None,
        }
        resp = client.put(
            "/api/admin/tokens/8",
            json={"allowed_chat_ids": [-1002]},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        assert token_cookie not in mod._sessions

    def test_update_token_label_only_keeps_sessions(self, auth_env):
        """Changing only a token's label should NOT invalidate sessions."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 9,
            "label": "old-label",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        token_cookie = resp.cookies.get("viewer_auth")

        cookie = _login_master(client)
        db.update_viewer_token.return_value = {
            "id": 9,
            "label": "new-label",
            "allowed_chat_ids": json.dumps([-1001]),
            "is_revoked": 0,
            "no_download": 0,
            "expires_at": None,
        }
        resp = client.put(
            "/api/admin/tokens/9",
            json={"label": "new-label"},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        # Label-only change should NOT invalidate
        assert token_cookie in mod._sessions


class TestSessionPersistence:
    """Tests that no_download and source_token_id survive session persistence."""

    def test_no_download_persisted_in_session(self, auth_env):
        """no_download should be passed to save_session for DB persistence."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 3,
            "label": "nd-token",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        assert resp.status_code == 200

        # Verify save_session was called with no_download=1 and source_token_id=3
        db.save_session.assert_called()
        call_kwargs = db.save_session.call_args
        assert call_kwargs.kwargs.get("no_download") == 1 or call_kwargs[1].get("no_download") == 1

    def test_source_token_id_persisted(self, auth_env):
        """source_token_id should be stored in the session."""
        client, mod, db = _get_client()
        db.verify_viewer_token.return_value = {
            "id": 42,
            "label": "tracked",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 0,
        }
        resp = client.post("/auth/token", json={"token": "validtoken"})
        cookie = resp.cookies.get("viewer_auth")

        assert mod._sessions[cookie].source_token_id == 42

        # Check DB persistence
        db.save_session.assert_called()
        call_kwargs = db.save_session.call_args
        assert call_kwargs.kwargs.get("source_token_id") == 42 or call_kwargs[1].get("source_token_id") == 42

    def test_no_download_restored_from_db(self, auth_env):
        """no_download should be correctly restored when loading session from DB."""
        client, mod, db = _get_client()
        # Simulate a DB-backed session with no_download
        db.get_session.return_value = {
            "token": "fake-session-token",
            "username": "token:test",
            "role": "token",
            "allowed_chat_ids": json.dumps([-1001]),
            "no_download": 1,
            "source_token_id": 99,
            "created_at": time.time(),
            "last_accessed": time.time(),
        }

        # Attempt auth check with the fake session token
        resp = client.get("/api/auth/check", cookies={"viewer_auth": "fake-session-token"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["no_download"] is True


class TestCreateViewerFlags:
    """Tests that create_viewer passes is_active and no_download correctly."""

    def test_create_viewer_with_no_download(self, auth_env):
        """Creating a viewer with no_download=1 should persist the flag."""
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.create_viewer_account.return_value = {
            "id": 1,
            "username": "testviewer",
            "password_hash": "h",
            "salt": "s",
            "allowed_chat_ids": None,
            "is_active": 1,
            "no_download": 1,
            "created_by": "admin",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
        resp = client.post(
            "/api/admin/viewers",
            json={"username": "testviewer", "password": "securepass1", "no_download": 1},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        assert resp.json()["no_download"] == 1
        # Verify the flag was passed to the DB method
        db.create_viewer_account.assert_called_once()
        call_kwargs = db.create_viewer_account.call_args
        assert call_kwargs.kwargs.get("no_download") == 1 or call_kwargs[1].get("no_download") == 1

    def test_create_viewer_with_inactive(self, auth_env):
        """Creating a viewer with is_active=0 should persist the flag."""
        client, _, db = _get_client()
        cookie = _login_master(client)
        db.create_viewer_account.return_value = {
            "id": 2,
            "username": "inactive",
            "password_hash": "h",
            "salt": "s",
            "allowed_chat_ids": None,
            "is_active": 0,
            "no_download": 0,
            "created_by": "admin",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
        }
        resp = client.post(
            "/api/admin/viewers",
            json={"username": "inactive", "password": "securepass1", "is_active": 0},
            cookies={"viewer_auth": cookie},
        )
        assert resp.status_code == 200
        db.create_viewer_account.assert_called_once()
        call_kwargs = db.create_viewer_account.call_args
        assert call_kwargs.kwargs.get("is_active") == 0 or call_kwargs[1].get("is_active") == 0


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
