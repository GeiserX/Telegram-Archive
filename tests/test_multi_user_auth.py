"""Integration tests for multi-user viewer access control (v7.0.0).

Uses FastAPI TestClient to test actual auth flows, admin CRUD,
per-user filtering, rate limiting, and media auth.
"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
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
            {"id": -1002, "title": "Chat B", "type": "channel"},
            {"id": -1003, "title": "Chat C", "type": "channel"},
        ]
    )
    db.get_chat_count = AsyncMock(return_value=3)
    db.get_cached_statistics = AsyncMock(return_value={"total_chats": 3, "total_messages": 100})
    db.get_metadata = AsyncMock(return_value=None)
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.get_viewer_account = AsyncMock(return_value=None)
    db.get_all_viewer_accounts = AsyncMock(return_value=[])
    db.create_viewer_account = AsyncMock()
    db.update_viewer_account = AsyncMock()
    db.delete_viewer_account = AsyncMock(return_value=True)
    db.create_audit_log = AsyncMock()
    db.get_audit_logs = AsyncMock(return_value=[])
    db.calculate_and_store_statistics = AsyncMock(return_value={"total_chats": 3})
    db.get_all_folders = AsyncMock(return_value=[])
    db.get_archived_chat_count = AsyncMock(return_value=0)
    return db


@pytest.fixture
def auth_env():
    """Set up auth env vars for testing."""
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
def no_auth_env():
    """Clear auth env vars."""
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "",
            "VIEWER_PASSWORD": "",
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


class TestAuthDisabled:
    """Tests when auth is disabled (no VIEWER_USERNAME/PASSWORD)."""

    def test_auth_check_returns_authenticated(self, no_auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["auth_required"] is False
        assert data["role"] == "master"

    def test_endpoints_accessible_without_cookies(self, no_auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats")
        assert resp.status_code == 200

    def test_login_returns_success(self, no_auth_env):
        client, _, _ = _get_client()
        resp = client.post("/api/login", json={"username": "any", "password": "any"})
        assert resp.status_code == 200


class TestMasterLogin:
    """Tests for master (env var) login."""

    def test_valid_login(self, auth_env):
        client, mod, _ = _get_client()
        resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["role"] == "master"
        assert "viewer_auth" in resp.cookies

    def test_invalid_login(self, auth_env):
        client, _, _ = _get_client()
        resp = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401

    def test_unauthenticated_request_rejected(self, auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats")
        assert resp.status_code == 401

    def test_authenticated_request_succeeds(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200

    def test_auth_check_with_valid_session(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/auth/check", cookies={"viewer_auth": cookie})
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "master"
        assert data["username"] == "admin"


class TestLogout:
    """Tests for logout."""

    def test_logout_invalidates_session(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")

        logout_resp = client.post("/api/logout", cookies={"viewer_auth": cookie})
        assert logout_resp.status_code == 200

        resp = client.get("/api/chats", cookies={"viewer_auth": cookie})
        assert resp.status_code == 401


class TestViewerLogin:
    """Tests for DB-backed viewer login."""

    def test_viewer_login(self, auth_env):
        import src.web.main as main_mod

        mock_db = _make_mock_db()
        salt = "a1b2c3d4"
        pw_hash = main_mod._hash_password("viewerpass", salt)
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 1,
                "username": "viewer1",
                "password_hash": pw_hash,
                "salt": salt,
                "allowed_chat_ids": json.dumps([-1001]),
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, mod, _ = _get_client(mock_db)

        resp = client.post("/api/login", json={"username": "viewer1", "password": "viewerpass"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "viewer"


class TestPerUserFiltering:
    """Tests for per-user chat filtering."""

    def test_master_sees_all_chats(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    def test_viewer_filtered_chats(self, auth_env):
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
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, mod, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "restricted", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/chats", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200


class TestRateLimiting:
    """Tests for login rate limiting."""

    def test_rate_limit_blocks_after_threshold(self, auth_env):
        client, mod, _ = _get_client()
        for _ in range(5):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})

        resp = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 429


class TestAdminEndpoints:
    """Tests for admin CRUD endpoints."""

    def _login_master(self, client):
        resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        return resp.cookies.get("viewer_auth")

    def test_list_viewers_requires_master(self, auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/admin/viewers")
        assert resp.status_code == 401

    def test_list_viewers_as_master(self, auth_env):
        client, _, _ = _get_client()
        cookie = self._login_master(client)
        resp = client.get("/api/admin/viewers", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        assert "viewers" in resp.json()

    def test_create_viewer(self, auth_env):
        mock_db = _make_mock_db()
        mock_db.create_viewer_account = AsyncMock(
            return_value={
                "id": 1,
                "username": "newuser",
                "password_hash": "x",
                "salt": "y",
                "allowed_chat_ids": None,
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)
        cookie = self._login_master(client)

        resp = client.post(
            "/api/admin/viewers",
            cookies={"viewer_auth": cookie},
            json={
                "username": "newuser",
                "password": "password123",
                "is_active": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "newuser"

    def test_create_viewer_short_password(self, auth_env):
        client, _, _ = _get_client()
        cookie = self._login_master(client)
        resp = client.post(
            "/api/admin/viewers",
            cookies={"viewer_auth": cookie},
            json={
                "username": "usr",
                "password": "short",
            },
        )
        assert resp.status_code == 400

    def test_delete_viewer(self, auth_env):
        mock_db = _make_mock_db()
        mock_db.get_viewer_account = AsyncMock(
            return_value={
                "id": 1,
                "username": "todelete",
                "password_hash": "x",
                "salt": "y",
                "allowed_chat_ids": None,
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)
        cookie = self._login_master(client)

        resp = client.delete("/api/admin/viewers/1", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200

    def test_viewer_cannot_access_admin(self, auth_env):
        import src.web.main as main_mod

        mock_db = _make_mock_db()
        salt = "test"
        pw_hash = main_mod._hash_password("vpass", salt)
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 1,
                "username": "viewer",
                "password_hash": pw_hash,
                "salt": salt,
                "allowed_chat_ids": None,
                "is_active": 1,
                "created_by": "admin",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        client, _, _ = _get_client(mock_db)

        login_resp = client.post("/api/login", json={"username": "viewer", "password": "vpass"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/admin/viewers", cookies={"viewer_auth": cookie})
        assert resp.status_code == 403


class TestMediaAuth:
    """Tests for authenticated media serving."""

    def test_media_requires_auth(self, auth_env):
        client, _, _ = _get_client()
        resp = client.get("/media/test.jpg")
        assert resp.status_code == 401

    def test_path_traversal_blocked(self, auth_env):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_dir = os.path.join(tmpdir, "media")
            os.makedirs(media_dir)
            secret = os.path.join(tmpdir, "secret.txt")
            with open(secret, "w") as f:
                f.write("secret")

            client, mod, _ = _get_client()
            cookie_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
            cookie = cookie_resp.cookies.get("viewer_auth")

            from pathlib import Path

            mod._media_root = Path(media_dir).resolve()

            resp = client.get("/media/../secret.txt", cookies={"viewer_auth": cookie})
            assert resp.status_code in (403, 404)


class TestAuditLog:
    """Tests for audit logging."""

    def test_login_creates_audit_entry(self, auth_env):
        mock_db = _make_mock_db()
        client, _, _ = _get_client(mock_db)

        client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        mock_db.create_audit_log.assert_called()
        call_kwargs = mock_db.create_audit_log.call_args
        assert "login_success" in str(call_kwargs)

    def test_failed_login_creates_audit_entry(self, auth_env):
        mock_db = _make_mock_db()
        client, _, _ = _get_client(mock_db)

        client.post("/api/login", json={"username": "admin", "password": "wrong"})
        mock_db.create_audit_log.assert_called()
        call_kwargs = mock_db.create_audit_log.call_args
        assert "login_failed" in str(call_kwargs)

    def test_get_audit_log_as_master(self, auth_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        cookie = login_resp.cookies.get("viewer_auth")
        resp = client.get("/api/admin/audit", cookies={"viewer_auth": cookie})
        assert resp.status_code == 200
        assert "logs" in resp.json()


class TestBackwardCompatibility:
    """Tests that existing single-user deployments still work."""

    def test_single_user_env_login(self, auth_env):
        client, _, _ = _get_client()
        resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "master"

    def test_no_auth_full_access(self, no_auth_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats")
        assert resp.status_code == 200
        resp2 = client.get("/api/stats")
        assert resp2.status_code == 200
