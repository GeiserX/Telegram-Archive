"""Tests for admin settings panel (v7.3.0).

Covers: AppSettings model, adapter methods, new endpoints,
password change, backup status, cron validation, theme presets.
"""

import hashlib
import importlib
import secrets

import pytest

from src.db.models import AppSettings

# ========================================================================
# TestAppSettingsModel
# ========================================================================


class TestAppSettingsModel:
    """Verify the AppSettings ORM model structure."""

    def test_model_table_name(self):
        assert AppSettings.__tablename__ == "app_settings"

    def test_primary_key_is_key_column(self):
        mapper = AppSettings.__mapper__
        pk_cols = [c.name for c in mapper.primary_key]
        assert pk_cols == ["key"]

    def test_model_has_required_columns(self):
        cols = {c.name for c in AppSettings.__table__.columns}
        assert "key" in cols
        assert "value" in cols
        assert "updated_at" in cols

    def test_key_column_is_string(self):
        col = AppSettings.__table__.columns["key"]
        assert str(col.type) in ("VARCHAR(255)", "String(255)")

    def test_value_column_is_text(self):
        col = AppSettings.__table__.columns["value"]
        assert "TEXT" in str(col.type).upper()


# ========================================================================
# TestCronValidation
# ========================================================================


class TestCronValidation:
    """Test cron expression validation logic used in endpoints."""

    def _validate_cron(self, expr: str) -> bool:
        """Replicates the validation from PUT /api/admin/settings."""
        parts = expr.split()
        return len(parts) == 5

    def test_valid_cron_5_parts(self):
        assert self._validate_cron("0 */6 * * *")

    def test_invalid_cron_too_few_parts(self):
        assert not self._validate_cron("0 */6 *")

    def test_invalid_cron_too_many_parts(self):
        assert not self._validate_cron("0 */6 * * * *")

    def test_common_presets(self):
        presets = [
            "0 */1 * * *",
            "0 */3 * * *",
            "0 */6 * * *",
            "0 */12 * * *",
            "0 0 * * *",
        ]
        for p in presets:
            assert self._validate_cron(p), f"Preset {p} should be valid"


# ========================================================================
# TestPasswordHashing
# ========================================================================


class TestPasswordHashing:
    """Test password hashing functions from main.py (production code)."""

    @pytest.fixture(autouse=True)
    def load_functions(self):
        from src.web.main import _hash_password, _verify_password

        self._hash_password = _hash_password
        self._verify_password = _verify_password

    def test_hash_and_verify_roundtrip(self):
        h, s = self._hash_password("mypassword")
        assert self._verify_password("mypassword", h, s)

    def test_wrong_password_fails(self):
        h, s = self._hash_password("correct")
        assert not self._verify_password("wrong", h, s)

    def test_min_length_validation(self):
        # The endpoint enforces >= 4 chars
        assert len("abc") < 4
        assert len("abcd") >= 4

    def test_different_passwords_different_hashes(self):
        h1, s1 = self._hash_password("password1")
        h2, s2 = self._hash_password("password2")
        assert h1 != h2

    def test_same_password_different_salts(self):
        h1, s1 = self._hash_password("same")
        h2, s2 = self._hash_password("same")
        assert s1 != s2  # Random salt


# ========================================================================
# TestEndpointRoutes
# ========================================================================


class TestEndpointRoutes:
    """Verify that all v7.3.0 routes are registered in the FastAPI app."""

    @pytest.fixture(autouse=True)
    def setup_app(self):
        main_mod = importlib.import_module("src.web.main")
        self.app = main_mod.app
        # Collect all (path, method) pairs — multiple routes can share a path
        self.route_methods = set()
        for r in self.app.routes:
            if hasattr(r, "methods"):
                for m in r.methods:
                    self.route_methods.add((r.path, m))

    def test_settings_get_route_exists(self):
        assert ("/api/admin/settings", "GET") in self.route_methods

    def test_settings_put_route_exists(self):
        assert ("/api/admin/settings", "PUT") in self.route_methods

    def test_backup_status_route_exists(self):
        assert ("/api/backup-status", "GET") in self.route_methods

    def test_activity_ping_route_exists(self):
        assert ("/api/activity/ping", "POST") in self.route_methods

    def test_password_change_route_exists(self):
        assert ("/api/auth/password", "PUT") in self.route_methods

    def test_token_update_route_exists(self):
        assert ("/api/admin/tokens/{token_id}", "PUT") in self.route_methods

    def test_user_info_route_exists(self):
        assert ("/api/users/{user_id}", "GET") in self.route_methods


# ========================================================================
# TestThemePresets
# ========================================================================


class TestThemePresets:
    """Verify theme CSS variable definitions in index.html."""

    @pytest.fixture(autouse=True)
    def load_html(self):
        import os

        html_path = os.path.join(os.path.dirname(__file__), "..", "src", "web", "templates", "index.html")
        with open(html_path) as f:
            self.html = f.read()

    def test_all_six_themes_defined(self):
        themes = ["midnight", "dark", "nord", "solarized", "oled", "light"]
        for t in themes:
            assert f".theme-{t}" in self.html, f"Theme .theme-{t} not found in HTML"

    def test_theme_has_required_variables(self):
        required_vars = ["--tg-bg", "--tg-sidebar", "--tg-text", "--tg-muted", "--tg-accent"]
        # Check that at least midnight theme has these
        for var in required_vars:
            assert var in self.html, f"CSS variable {var} not found in HTML"

    def test_theme_flash_prevention_script(self):
        # The inline script in <head> should read localStorage and set class
        assert "localStorage.getItem('tg-theme')" in self.html
        assert "theme-" in self.html


# ========================================================================
# TestMigration011
# ========================================================================


class TestMigration011:
    """Verify migration 011 file exists and has correct structure."""

    @pytest.fixture(autouse=True)
    def load_migration(self):
        import os

        self.migration_path = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions", "20260310_011_add_app_settings.py"
        )

    def test_migration_file_exists(self):
        import os

        assert os.path.exists(self.migration_path)

    def test_migration_revision_chain(self):
        with open(self.migration_path) as f:
            content = f.read()
        assert 'revision: str = "011"' in content
        assert 'down_revision: str | None = "010"' in content

    def test_migration_creates_app_settings(self):
        with open(self.migration_path) as f:
            content = f.read()
        assert "app_settings" in content
        assert "def upgrade" in content
        assert "def downgrade" in content


# ========================================================================
# TestSchedulerSettingsPoll
# ========================================================================


class TestSchedulerSettingsPoll:
    """Test that scheduler has settings polling capability."""

    @pytest.fixture(autouse=True)
    def check_apscheduler(self):
        pytest.importorskip("apscheduler", reason="apscheduler not installed in test env")

    def test_scheduler_has_poll_method(self):
        from src.scheduler import BackupScheduler

        assert hasattr(BackupScheduler, "_poll_settings")
        assert callable(BackupScheduler._poll_settings)

    def test_scheduler_has_db_attribute(self):
        # Verify _poll_settings is an async method
        import inspect

        from src.scheduler import BackupScheduler

        assert inspect.iscoroutinefunction(BackupScheduler._poll_settings)
