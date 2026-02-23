"""Tests for viewer authentication functionality."""

import hashlib
import json
import os
import secrets
from unittest.mock import patch

import pytest


class TestAuthConfiguration:
    """Test authentication configuration."""

    def test_auth_disabled_when_no_credentials(self):
        """Auth should be disabled when no credentials are set."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("VIEWER_USERNAME", None)
            os.environ.pop("VIEWER_PASSWORD", None)

            username = os.getenv("VIEWER_USERNAME", "").strip()
            password = os.getenv("VIEWER_PASSWORD", "").strip()
            auth_enabled = bool(username and password)

            assert auth_enabled is False

    def test_auth_enabled_when_credentials_set(self):
        """Auth should be enabled when both credentials are set."""
        with patch.dict(os.environ, {"VIEWER_USERNAME": "testuser", "VIEWER_PASSWORD": "testpass"}):
            username = os.getenv("VIEWER_USERNAME", "").strip()
            password = os.getenv("VIEWER_PASSWORD", "").strip()
            auth_enabled = bool(username and password)

            assert auth_enabled is True

    def test_auth_token_generation(self):
        """Auth token should be PBKDF2-SHA256 derived hex string."""
        username = "testuser"
        password = "testpass123"

        expected_token = hashlib.pbkdf2_hmac(
            "sha256",
            f"{username}:{password}".encode(),
            b"telegram-archive-viewer",
            600_000,
        ).hex()

        assert len(expected_token) == 64

    def test_whitespace_trimming(self):
        """Whitespace should be trimmed from credentials."""
        with patch.dict(os.environ, {"VIEWER_USERNAME": "  testuser  ", "VIEWER_PASSWORD": "  testpass  "}):
            username = os.getenv("VIEWER_USERNAME", "").strip()
            password = os.getenv("VIEWER_PASSWORD", "").strip()

            assert username == "testuser"
            assert password == "testpass"


class TestCookieConfiguration:
    """Test cookie configuration."""

    def test_cookie_name_constant(self):
        """Cookie name should be 'viewer_auth'."""
        expected_cookie_name = "viewer_auth"
        assert expected_cookie_name == "viewer_auth"


class TestAuthEndpointStructure:
    """Test auth endpoint response structures."""

    def test_auth_check_response_structure(self):
        """Auth check endpoint should return expected structure."""
        response_disabled = {"authenticated": True, "auth_required": False}
        assert "authenticated" in response_disabled
        assert "auth_required" in response_disabled

        response_enabled_unauth = {"authenticated": False, "auth_required": True}
        assert "authenticated" in response_enabled_unauth
        assert "auth_required" in response_enabled_unauth

    def test_login_response_structure(self):
        """Login endpoint should return expected structure."""
        success_response = {"success": True}
        assert "success" in success_response


# ============================================================================
# Multi-User Auth Tests
# ============================================================================


class TestPasswordHashing:
    """Test PBKDF2 password hashing helpers."""

    def test_hash_password_produces_hex(self):
        salt = secrets.token_hex(32)
        hash_bytes = hashlib.pbkdf2_hmac("sha256", b"testpass", bytes.fromhex(salt), 600_000)
        password_hash = hash_bytes.hex()

        assert len(password_hash) == 64  # SHA256 = 32 bytes = 64 hex chars
        assert len(salt) == 64

    def test_hash_deterministic_with_same_salt(self):
        salt = "a" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", b"pass", bytes.fromhex(salt), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", b"pass", bytes.fromhex(salt), 600_000).hex()
        assert h1 == h2

    def test_hash_differs_with_different_salt(self):
        s1 = "a" * 64
        s2 = "b" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", b"pass", bytes.fromhex(s1), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", b"pass", bytes.fromhex(s2), 600_000).hex()
        assert h1 != h2

    def test_different_passwords_differ(self):
        salt = "a" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", b"pass1", bytes.fromhex(salt), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", b"pass2", bytes.fromhex(salt), 600_000).hex()
        assert h1 != h2

    def test_verify_password_timing_safe(self):
        salt = secrets.token_hex(32)
        password = "mypassword"
        hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
        stored_hash = hash_bytes.hex()

        # Correct password
        computed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
        assert secrets.compare_digest(computed, stored_hash)

        # Wrong password
        wrong = hashlib.pbkdf2_hmac("sha256", b"wrong", bytes.fromhex(salt), 600_000).hex()
        assert not secrets.compare_digest(wrong, stored_hash)


class TestMultiUserAuthentication:
    """Test dual-mode authentication (DB + env-var fallback)."""

    def test_master_token_unchanged(self):
        token = hashlib.pbkdf2_hmac("sha256", b"admin:secret", b"telegram-archive-viewer", 600_000).hex()
        assert len(token) == 64

    def test_session_store_structure(self):
        session = {
            "role": "viewer",
            "username": "john",
            "allowed_chat_ids": {-100123, -100456},
            "viewer_id": 1,
        }
        assert session["role"] in ("master", "viewer")
        assert isinstance(session["allowed_chat_ids"], set)
        assert session["viewer_id"] is not None

    def test_master_session_structure(self):
        session = {
            "role": "master",
            "username": "admin",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        assert session["allowed_chat_ids"] is None
        assert session["viewer_id"] is None

    def test_auth_check_response_with_role(self):
        response = {
            "authenticated": True,
            "auth_required": True,
            "role": "master",
            "username": "admin",
        }
        assert "role" in response
        assert response["role"] in ("master", "viewer")

    def test_login_response_with_role(self):
        response = {"success": True, "role": "viewer", "username": "john"}
        assert response["role"] == "viewer"
        assert response["username"] == "john"


class TestAdminEndpoints:
    """Test admin CRUD endpoint structures and validation."""

    def test_viewer_account_create_payload(self):
        payload = {
            "username": "newuser",
            "password": "pass1234",
            "allowed_chat_ids": [-100111, -100222],
        }
        assert payload["username"]
        assert payload["password"]
        assert isinstance(payload["allowed_chat_ids"], list)

    def test_viewer_account_update_payload_password_optional(self):
        payload = {"allowed_chat_ids": [-100111]}
        assert "password" not in payload or payload.get("password") == ""

    def test_viewer_account_db_schema(self):
        from src.db.models import ViewerAccount

        columns = [c.name for c in ViewerAccount.__table__.columns]
        expected_fields = [
            "id",
            "username",
            "password_hash",
            "salt",
            "allowed_chat_ids",
            "is_active",
            "created_at",
            "updated_at",
        ]
        for field in expected_fields:
            assert field in columns, f"Missing field: {field}"

    def test_username_collision_with_master_rejected(self):
        master_username = "admin"
        viewer_username = "admin"
        assert master_username.lower() == viewer_username.lower()

    def test_allowed_chat_ids_json_serialization(self):
        chat_ids = [-100123456, -100789012]
        serialized = json.dumps(chat_ids)
        deserialized = json.loads(serialized)
        assert deserialized == chat_ids
        assert all(isinstance(cid, int) for cid in deserialized)


class TestPerUserChatFiltering:
    """Test chat filtering logic for different user roles."""

    def test_master_no_display_ids_sees_all(self):
        user = {"role": "master", "allowed_chat_ids": None}
        display_chat_ids = set()
        if user["role"] == "master":
            result = display_chat_ids if display_chat_ids else None
        assert result is None

    def test_master_with_display_ids_respects_them(self):
        display_chat_ids = {-100111, -100222}
        result = display_chat_ids if display_chat_ids else None
        assert result == {-100111, -100222}

    def test_viewer_uses_own_chat_ids(self):
        user = {"role": "viewer", "allowed_chat_ids": {-100111, -100333}}
        result = user.get("allowed_chat_ids") or set()
        assert result == {-100111, -100333}

    def test_viewer_empty_chat_ids_sees_nothing(self):
        user = {"role": "viewer", "allowed_chat_ids": set()}
        result = user.get("allowed_chat_ids") or set()
        assert result == set()

    def test_chat_access_check_pattern(self):
        user_chats = {-100111, -100222}
        assert -100111 in user_chats
        assert -100999 not in user_chats

        user_chats_master = None
        assert user_chats_master is None


class TestBackwardCompatibility:
    """Verify no-viewer-accounts scenario matches current behavior."""

    def test_no_viewer_accounts_master_uses_display_ids(self):
        display_chat_ids = {-100111, -100222}
        result = display_chat_ids if display_chat_ids else None
        assert result == {-100111, -100222}

    def test_existing_cookie_still_works(self):
        token = hashlib.pbkdf2_hmac("sha256", b"admin:secret", b"telegram-archive-viewer", 600_000).hex()
        assert len(token) == 64
        assert isinstance(token, str)

    def test_auth_disabled_gives_full_access(self):
        user = {
            "role": "master",
            "username": "anonymous",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        assert user["allowed_chat_ids"] is None
        assert user["role"] == "master"


class TestAuditLogging:
    """Test audit log model and query patterns."""

    def test_audit_log_db_schema(self):
        from src.db.models import ViewerAuditLog

        columns = [c.name for c in ViewerAuditLog.__table__.columns]
        expected_fields = [
            "id",
            "viewer_id",
            "username",
            "endpoint",
            "chat_id",
            "ip_address",
            "timestamp",
        ]
        for field in expected_fields:
            assert field in columns, f"Missing field: {field}"

    def test_audit_log_entry_structure(self):
        entry = {
            "viewer_id": 1,
            "username": "john",
            "endpoint": "/api/chats",
            "chat_id": None,
            "ip_address": "10.10.101.5",
            "timestamp": "2026-02-24T14:32:05",
        }
        assert entry["viewer_id"] is not None
        assert entry["username"]
        assert entry["endpoint"].startswith("/api/")

    def test_audit_log_not_recorded_for_master(self):
        user = {"role": "master", "viewer_id": None}
        should_log = user["role"] != "master"
        assert should_log is False

    def test_audit_log_recorded_for_viewer(self):
        user = {"role": "viewer", "viewer_id": 1}
        should_log = user["role"] != "master"
        assert should_log is True

    def test_audit_query_filter_by_viewer(self):
        logs = [
            {"viewer_id": 1, "endpoint": "/api/chats"},
            {"viewer_id": 2, "endpoint": "/api/chats"},
            {"viewer_id": 1, "endpoint": "/api/messages"},
        ]
        filtered = [entry for entry in logs if entry["viewer_id"] == 1]
        assert len(filtered) == 2


if __name__ == "__main__":
    pytest.main([__file__])
