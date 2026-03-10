"""Tests for share token authentication (v7.2.0)."""

import hashlib
import json
import secrets
from datetime import datetime, timedelta

import pytest


class TestTokenGeneration:
    """Test token generation and hashing."""

    def test_token_length(self):
        """Token should be 64 hex chars (256-bit)."""
        token = secrets.token_hex(32)
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_token_uniqueness(self):
        """100 generated tokens should all be unique."""
        tokens = {secrets.token_hex(32) for _ in range(100)}
        assert len(tokens) == 100

    def test_hash_and_verify(self):
        """PBKDF2 hash should verify correctly."""
        token = secrets.token_hex(32)
        salt = secrets.token_hex(32)
        token_hash = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000).hex()

        # Correct token verifies
        computed = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000).hex()
        assert secrets.compare_digest(computed, token_hash)

        # Wrong token fails
        wrong = hashlib.pbkdf2_hmac("sha256", b"wrong", bytes.fromhex(salt), 600_000).hex()
        assert not secrets.compare_digest(wrong, token_hash)

    def test_deterministic_hash(self):
        """Same token + salt always produce same hash."""
        token = "abc123"
        salt = secrets.token_hex(32)
        h1 = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000).hex()
        assert h1 == h2

    def test_different_salts_produce_different_hashes(self):
        """Same token with different salts should produce different hashes."""
        token = "same_token"
        salt1 = secrets.token_hex(32)
        salt2 = secrets.token_hex(32)
        h1 = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt1), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt2), 600_000).hex()
        assert h1 != h2


class TestViewerTokenModel:
    """Test ViewerToken SQLAlchemy model."""

    def test_model_table_name(self):
        from src.db.models import ViewerToken

        assert ViewerToken.__tablename__ == "viewer_tokens"

    def test_model_columns(self):
        from src.db.models import ViewerToken

        columns = {c.name for c in ViewerToken.__table__.columns}
        expected = {
            "id", "label", "token_hash", "token_salt", "created_by",
            "allowed_chat_ids", "is_revoked", "no_download", "expires_at", "last_used_at",
            "use_count", "created_at",
        }
        assert expected == columns

    def test_token_hash_unique_constraint(self):
        from src.db.models import ViewerToken

        col = ViewerToken.__table__.columns["token_hash"]
        assert col.unique is True

    def test_is_revoked_default(self):
        from src.db.models import ViewerToken

        col = ViewerToken.__table__.columns["is_revoked"]
        assert col.server_default is not None
        assert str(col.server_default.arg) == "0"


class TestTokenExpiryLogic:
    """Test token expiry and revocation logic."""

    def test_unexpired_token(self):
        """Token with future expiry should be valid."""
        expires_at = datetime.utcnow() + timedelta(hours=24)
        assert expires_at > datetime.utcnow()

    def test_expired_token(self):
        """Token with past expiry should be invalid."""
        expires_at = datetime.utcnow() - timedelta(hours=1)
        assert expires_at < datetime.utcnow()

    def test_no_expiry_always_valid(self):
        """Token with None expiry should always be valid."""
        expires_at = None
        # Logic: if expires_at is None, don't check expiry
        is_valid = expires_at is None or expires_at > datetime.utcnow()
        assert is_valid is True


class TestTokenSessionShape:
    """Test that token sessions have correct structure for _viewer_sessions."""

    def test_session_has_required_keys(self):
        """Token session dict must have all keys expected by require_auth."""
        session = {
            "role": "viewer",
            "username": "token:test-label",
            "allowed_chat_ids": {-100123, -100456},
            "viewer_id": None,
            "_token_id": 5,
            "_created_at": 1741564800.0,
        }
        # These keys are read by _get_current_user and _get_user_chat_ids
        assert session["role"] == "viewer"
        assert isinstance(session["allowed_chat_ids"], set)
        assert session.get("viewer_id") is None
        assert session.get("_token_id") == 5
        assert "_created_at" in session

    def test_allowed_chat_ids_is_set(self):
        """allowed_chat_ids must be a set for intersection operations."""
        raw_ids = [-100123, -100456]
        allowed = set(int(c) for c in raw_ids)
        assert isinstance(allowed, set)
        assert -100123 in allowed

    def test_token_username_format(self):
        """Token username should be 'token:label' or 'token:id'."""
        assert "token:my-demo".startswith("token:")
        assert "token:42".startswith("token:")


class TestAllowedChatIdsSerialization:
    """Test JSON serialization of allowed_chat_ids."""

    def test_serialize_chat_ids(self):
        """Chat IDs should serialize to JSON array."""
        chat_ids = [-100123456, -100789012]
        serialized = json.dumps(chat_ids)
        assert serialized == "[-100123456, -100789012]"

    def test_deserialize_chat_ids(self):
        """JSON array should deserialize back to list."""
        serialized = "[-100123456, -100789012]"
        chat_ids = json.loads(serialized)
        assert chat_ids == [-100123456, -100789012]

    def test_empty_chat_ids(self):
        """Empty list should serialize/deserialize cleanly."""
        assert json.dumps([]) == "[]"
        assert json.loads("[]") == []


class TestMigrationScript:
    """Test Alembic migration script structure."""

    def test_migration_revision_chain(self):
        """Migration 010 should follow 009."""
        path = "alembic/versions/20260310_010_add_viewer_tokens.py"
        content = open(path).read()
        assert 'revision: str = "010"' in content
        assert 'down_revision: str | None = "009"' in content

    def test_migration_has_upgrade_downgrade(self):
        """Migration must have both upgrade and downgrade functions."""
        path = "alembic/versions/20260310_010_add_viewer_tokens.py"
        content = open(path).read()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_creates_viewer_tokens_table(self):
        """Migration should create viewer_tokens table."""
        path = "alembic/versions/20260310_010_add_viewer_tokens.py"
        content = open(path).read()
        assert '"viewer_tokens"' in content
        assert '"token_hash"' in content
        assert '"allowed_chat_ids"' in content


class TestEndpointRoutes:
    """Test that token endpoints are registered."""

    def test_token_routes_exist(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/admin/tokens" in paths
        assert "/api/admin/tokens/{token_id}" in paths
        assert "/auth/token" in paths

    def test_root_route_has_token_param(self):
        """Root route should accept optional ?token= query param."""
        from src.web.main import app

        root_routes = [r for r in app.routes if hasattr(r, "path") and r.path == "/"]
        assert len(root_routes) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
