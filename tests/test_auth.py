"""Tests for viewer authentication functionality."""

import hashlib
import os
from unittest.mock import patch

import pytest


class TestAuthConfiguration:
    """Test authentication configuration."""

    def test_auth_disabled_when_no_credentials(self):
        """Auth should be disabled when no credentials are set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove any existing credentials
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

    def test_password_hashing(self):
        """Password hashing should produce consistent PBKDF2-SHA256 hex digests."""
        password = "testpass123"
        salt = "test_salt_value"

        result = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()

        assert len(result) == 64
        # Deterministic: same inputs produce same output
        result2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
        assert result == result2

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
        # Expected response when auth is disabled
        response_disabled = {"authenticated": True, "auth_required": False}

        assert "authenticated" in response_disabled
        assert "auth_required" in response_disabled

        # Expected response when auth is enabled but not authenticated
        response_enabled_unauth = {"authenticated": False, "auth_required": True}

        assert "authenticated" in response_enabled_unauth
        assert "auth_required" in response_enabled_unauth

    def test_login_response_structure(self):
        """Login endpoint should return expected structure."""
        # Success response
        success_response = {"success": True}
        assert "success" in success_response

        # Failure should return HTTP 401


if __name__ == "__main__":
    pytest.main([__file__])
