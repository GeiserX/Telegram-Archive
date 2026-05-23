"""Integration tests for trusted proxy header authentication (v7.9.0).

Tests the AUTH_PROXY_HEADER flow: header-based identity forwarding from
reverse proxies like Authelia, Authentik, and Keycloak.
"""

import json
import os
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
        ]
    )
    db.get_chat_count = AsyncMock(return_value=2)
    db.get_cached_statistics = AsyncMock(return_value={"total_chats": 2, "total_messages": 50})
    db.get_metadata = AsyncMock(return_value=None)
    db.get_viewer_by_username = AsyncMock(return_value=None)
    db.get_all_viewer_accounts = AsyncMock(return_value=[])
    db.create_viewer_account = AsyncMock(
        return_value={"id": 1, "username": "testuser", "allowed_chat_ids": "[]", "is_active": 1}
    )
    db.create_audit_log = AsyncMock()
    db.get_all_folders = AsyncMock(return_value=[])
    db.get_archived_chat_count = AsyncMock(return_value=0)
    db.get_session = AsyncMock(return_value=None)
    db.save_session = AsyncMock()
    db.delete_session = AsyncMock()
    return db


@pytest.fixture
def proxy_env():
    """Set up proxy auth env vars (no basic auth)."""
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "",
            "VIEWER_PASSWORD": "",
            "AUTH_PROXY_HEADER": "Remote-User",
            "AUTH_PROXY_ADMIN_USERS": "admin@example.com,root@example.com",
            "AUTH_PROXY_DEFAULT_ACCESS": "none",
            "SECURE_COOKIES": "false",
        },
    ):
        yield


@pytest.fixture
def proxy_env_all_access():
    """Proxy auth with default access = all."""
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "",
            "VIEWER_PASSWORD": "",
            "AUTH_PROXY_HEADER": "Remote-User",
            "AUTH_PROXY_ADMIN_USERS": "admin@example.com",
            "AUTH_PROXY_DEFAULT_ACCESS": "all",
            "SECURE_COOKIES": "false",
        },
    ):
        yield


@pytest.fixture
def proxy_with_basic_env():
    """Proxy auth combined with basic auth."""
    with patch.dict(
        os.environ,
        {
            "VIEWER_USERNAME": "admin",
            "VIEWER_PASSWORD": "testpass123",
            "AUTH_PROXY_HEADER": "X-Forwarded-User",
            "AUTH_PROXY_ADMIN_USERS": "sso-admin@corp.com",
            "AUTH_PROXY_DEFAULT_ACCESS": "none",
            "SECURE_COOKIES": "false",
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


class TestProxyAuthAdminUser:
    """Tests for proxy-authenticated admin users."""

    def test_admin_gets_master_role(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"Remote-User": "admin@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "master"
        assert data["username"] == "admin@example.com"
        assert data.get("proxy_auth") is True

    def test_admin_can_access_protected_endpoints(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats", headers={"Remote-User": "admin@example.com"})
        assert resp.status_code == 200

    def test_second_admin_also_works(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"Remote-User": "root@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "master"


class TestProxyAuthViewerUser:
    """Tests for proxy-authenticated regular (non-admin) users."""

    def test_unknown_user_auto_created_with_no_access(self, proxy_env):
        mock_db = _make_mock_db()
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/auth/check", headers={"Remote-User": "newuser@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "viewer"
        assert data["username"] == "newuser@example.com"

        mock_db.create_viewer_account.assert_called_once()
        call_kwargs = mock_db.create_viewer_account.call_args[1]
        assert call_kwargs["username"] == "newuser@example.com"
        assert call_kwargs["salt"] == "proxy-auth"
        assert call_kwargs["allowed_chat_ids"] == "[]"
        assert call_kwargs["created_by"] == "proxy-auth"

    def test_auto_created_user_default_all_access(self, proxy_env_all_access):
        mock_db = _make_mock_db()
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/auth/check", headers={"Remote-User": "newuser@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "viewer"

        call_kwargs = mock_db.create_viewer_account.call_args[1]
        assert call_kwargs["allowed_chat_ids"] is None

    def test_existing_viewer_account_used(self, proxy_env):
        mock_db = _make_mock_db()
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 5,
                "username": "existing@example.com",
                "password_hash": "",
                "salt": "proxy-auth",
                "allowed_chat_ids": json.dumps([-1001]),
                "is_active": 1,
                "no_download": 0,
            }
        )
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/auth/check", headers={"Remote-User": "existing@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "viewer"
        mock_db.create_viewer_account.assert_not_called()

    def test_disabled_account_rejected(self, proxy_env):
        mock_db = _make_mock_db()
        mock_db.get_viewer_by_username = AsyncMock(
            return_value={
                "id": 5,
                "username": "disabled@example.com",
                "password_hash": "",
                "salt": "proxy-auth",
                "allowed_chat_ids": None,
                "is_active": 0,
                "no_download": 0,
            }
        )
        client, _, _ = _get_client(mock_db)
        resp = client.get("/api/auth/check", headers={"Remote-User": "disabled@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False


class TestProxyAuthNoHeader:
    """Tests when proxy auth is enabled but no header is provided."""

    def test_no_header_returns_unauthenticated(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["auth_required"] is True

    def test_empty_header_returns_unauthenticated(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"Remote-User": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False

    def test_protected_endpoint_returns_401(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/chats")
        assert resp.status_code == 401


class TestProxyAuthCombinedWithBasic:
    """Tests when both proxy auth and basic auth are enabled together."""

    def test_proxy_header_takes_priority(self, proxy_with_basic_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"X-Forwarded-User": "sso-admin@corp.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "master"
        assert data.get("proxy_auth") is True

    def test_basic_auth_still_works_without_header(self, proxy_with_basic_env):
        client, _, _ = _get_client()
        resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["role"] == "master"

    def test_cookie_session_works_without_proxy_header(self, proxy_with_basic_env):
        client, _, _ = _get_client()
        login_resp = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
        assert login_resp.status_code == 200
        resp = client.get("/api/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert data["role"] == "master"


class TestProxyAuthSecurity:
    """Security-focused tests for proxy auth."""

    def test_wrong_header_name_ignored(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"X-Wrong-Header": "admin@example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False

    def test_whitespace_only_header_rejected(self, proxy_env):
        client, _, _ = _get_client()
        resp = client.get("/api/auth/check", headers={"Remote-User": "   "})
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False

    def test_proxy_auth_disabled_by_default(self):
        """Without AUTH_PROXY_HEADER env var, proxy headers are ignored."""
        with patch.dict(
            os.environ,
            {
                "VIEWER_USERNAME": "admin",
                "VIEWER_PASSWORD": "testpass123",
                "AUTH_PROXY_HEADER": "",
                "SECURE_COOKIES": "false",
            },
        ):
            client, _, _ = _get_client()
            resp = client.get("/api/auth/check", headers={"Remote-User": "admin@example.com"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["authenticated"] is False
            assert data.get("proxy_auth") is None
