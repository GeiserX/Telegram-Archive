import os
import pytest
import importlib
import tempfile
import hashlib
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# We need to control how src.web.main is imported
import sys

@pytest.fixture
def mock_env_and_config():
    """Setup safe environment and mock Config for web.main import."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Patch environment to prevent Config from creating real directories
        env_vars = {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': temp_dir,
            'DATABASE_DIR': temp_dir,
        }
        with patch.dict(os.environ, env_vars, clear=False):
            yield
            
        # Cleanup: close database connection if it exists to allow temp dir removal
        if 'src.web.main' in sys.modules:
            try:
                if hasattr(sys.modules['src.web.main'], 'db'):
                    sys.modules['src.web.main'].db.close()
            except:
                pass

@pytest.fixture
def client_with_auth(mock_env_and_config, monkeypatch):
    """Create a test client with authentication enabled."""
    monkeypatch.setenv("VIEWER_USERNAME", "testuser")
    monkeypatch.setenv("VIEWER_PASSWORD", "testpass123")
    
    # We must reload the module to pick up the new env vars for AUTH_ENABLED logic
    if 'src.web.main' in sys.modules:
        import src.web.main
        importlib.reload(src.web.main)
    else:
        import src.web.main
        
    return TestClient(src.web.main.app)


@pytest.fixture
def client_no_auth(mock_env_and_config, monkeypatch):
    """Create a test client with authentication disabled."""
    monkeypatch.delenv("VIEWER_USERNAME", raising=False)
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    
    # Reload to ensure AUTH_ENABLED is False
    if 'src.web.main' in sys.modules:
        import src.web.main
        importlib.reload(src.web.main)
    else:
        import src.web.main
        
    return TestClient(src.web.main.app)


def test_auth_status_when_disabled(client_no_auth):
    """Test that auth status returns correct values when auth is disabled."""
    response = client_no_auth.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["auth_required"] is False
    assert data["authenticated"] is True


def test_auth_status_when_enabled_no_cookie(client_with_auth):
    """Test that auth status shows unauthenticated when no cookie is present."""
    response = client_with_auth.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["auth_required"] is True
    assert data["authenticated"] is False


def test_login_with_correct_credentials(client_with_auth):
    """Test login with correct credentials sets the cookie."""
    response = client_with_auth.post(
        "/api/login",
        json={"username": "testuser", "password": "testpass123"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    
    # Verify cookie was set with correct name
    assert "viewer_auth" in response.cookies
    cookie_value = response.cookies["viewer_auth"]
    assert cookie_value  # Should be a non-empty hash


def test_login_with_wrong_credentials(client_with_auth):
    """Test login with wrong credentials returns 401."""
    response = client_with_auth.post(
        "/api/login",
        json={"username": "testuser", "password": "wrongpassword"}
    )
    assert response.status_code == 401


def test_login_with_whitespace_credentials(client_with_auth):
    """Test login handles whitespace in credentials correctly."""
    response = client_with_auth.post(
        "/api/login",
        json={"username": " testuser ", "password": " testpass123 "}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True


def test_protected_endpoint_with_cookie(client_with_auth):
    """Test accessing protected endpoint with valid cookie."""
    # First login to get the cookie
    login_response = client_with_auth.post(
        "/api/login",
        json={"username": "testuser", "password": "testpass123"}
    )
    assert login_response.status_code == 200
    
    # Now check auth status with the cookie
    response = client_with_auth.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["authenticated"] is True


def test_cookie_name_consistency(client_with_auth):
    """Test that cookie name matches AUTH_COOKIE_NAME constant."""
    from src.web.main import AUTH_COOKIE_NAME
    
    response = client_with_auth.post(
        "/api/login",
        json={"username": "testuser", "password": "testpass123"}
    )
    
    # Cookie name in response should match the constant
    assert AUTH_COOKIE_NAME in response.cookies
    assert AUTH_COOKIE_NAME == "viewer_auth"
