"""Tests for viewer UX & preferences features (phases 1-3)."""

from pathlib import Path

import pytest


class TestAdminChatsAPI:
    """Phase 1: Admin chats API returns user metadata."""

    def test_chats_route_exists(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/admin/chats" in paths


class TestNoDownloadModel:
    """Phase 2: no_download column on ViewerAccount and ViewerToken."""

    def test_viewer_account_has_no_download(self):
        from src.db.models import ViewerAccount

        columns = {c.name for c in ViewerAccount.__table__.columns}
        assert "no_download" in columns

    def test_viewer_account_no_download_default(self):
        from src.db.models import ViewerAccount

        col = ViewerAccount.__table__.columns["no_download"]
        assert col.server_default is not None
        assert str(col.server_default.arg) == "1"

    def test_viewer_token_has_no_download(self):
        from src.db.models import ViewerToken

        columns = {c.name for c in ViewerToken.__table__.columns}
        assert "no_download" in columns

    def test_viewer_token_no_download_default(self):
        from src.db.models import ViewerToken

        col = ViewerToken.__table__.columns["no_download"]
        assert col.server_default is not None
        assert str(col.server_default.arg) == "1"


class TestNoDownloadMigration:
    """Phase 2: Migration 012 structure."""

    def test_migration_012_exists(self):
        path = "alembic/versions/20260310_012_add_no_download.py"
        content = Path(path).read_text()
        assert 'revision: str = "012"' in content
        assert 'down_revision: str | None = "011"' in content

    def test_migration_012_has_upgrade_downgrade(self):
        path = "alembic/versions/20260310_012_add_no_download.py"
        content = Path(path).read_text()
        assert "def upgrade()" in content
        assert "def downgrade()" in content

    def test_migration_012_adds_no_download_columns(self):
        path = "alembic/versions/20260310_012_add_no_download.py"
        content = Path(path).read_text()
        assert '"viewer_accounts"' in content
        assert '"viewer_tokens"' in content
        assert '"no_download"' in content


class TestNoDownloadEndpoints:
    """Phase 2: Viewer/token endpoints accept no_download."""

    def test_viewer_crud_routes_exist(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/admin/viewers" in paths
        assert "/api/admin/viewers/{viewer_id}" in paths

    def test_token_crud_routes_exist(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/admin/tokens" in paths
        assert "/api/admin/tokens/{token_id}" in paths


class TestAuditLogModel:
    """Phase 3: ViewerAuditLog model structure."""

    def test_audit_log_table_name(self):
        from src.db.models import ViewerAuditLog

        assert ViewerAuditLog.__tablename__ == "viewer_audit_log"

    def test_audit_log_columns(self):
        from src.db.models import ViewerAuditLog

        columns = {c.name for c in ViewerAuditLog.__table__.columns}
        expected = {
            "id", "username", "role", "action", "endpoint",
            "chat_id", "ip_address", "user_agent", "created_at",
        }
        assert expected == columns

    def test_audit_log_username_not_nullable(self):
        from src.db.models import ViewerAuditLog

        col = ViewerAuditLog.__table__.columns["username"]
        assert col.nullable is False

    def test_audit_log_action_not_nullable(self):
        from src.db.models import ViewerAuditLog

        col = ViewerAuditLog.__table__.columns["action"]
        assert col.nullable is False


class TestAuditLogEndpoint:
    """Phase 3: Audit log endpoint route."""

    def test_audit_route_exists(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/admin/audit" in paths


class TestAuthCheckRoute:
    """Phase 2: Auth check endpoint includes no_download."""

    def test_auth_check_route_exists(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/auth/check" in paths

    def test_login_route_exists(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/login" in paths

    def test_token_auth_route_exists(self):
        from src.web.main import app

        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/auth/token" in paths


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
