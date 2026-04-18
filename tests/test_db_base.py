"""Tests for database manager - initialization, URL building, session management."""

import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.base import DatabaseManager, close_database, get_db_manager, init_database

# ============================================================
# URL building from environment variables
# ============================================================


class TestBuildDatabaseUrl:
    """Test _build_database_url for various environment configurations."""

    def test_defaults_to_sqlite_when_no_env_set(self):
        """Default URL uses SQLite at /data/backups when no env vars are set."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "sqlite+aiosqlite" in manager.database_url
            assert "telegram_backup.db" in manager.database_url

    def test_database_url_env_takes_priority(self):
        """DATABASE_URL environment variable overrides all other settings."""
        env = {"DATABASE_URL": "postgresql://user:pass@host:5432/mydb"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "postgresql+asyncpg://" in manager.database_url
            assert "host:5432/mydb" in manager.database_url

    def test_db_type_postgresql_builds_pg_url(self):
        """DB_TYPE=postgresql builds a PostgreSQL asyncpg URL."""
        env = {
            "DB_TYPE": "postgresql",
            "POSTGRES_HOST": "dbhost",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "myuser",
            "POSTGRES_PASSWORD": "mypass",
            "POSTGRES_DB": "mydb",
        }
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "postgresql+asyncpg://" in manager.database_url
            assert "myuser" in manager.database_url
            assert "dbhost:5433" in manager.database_url
            assert "/mydb" in manager.database_url

    def test_db_type_postgres_alias_builds_pg_url(self):
        """DB_TYPE=postgres (alias) builds a PostgreSQL asyncpg URL."""
        env = {"DB_TYPE": "postgres", "POSTGRES_PASSWORD": "secret"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "postgresql+asyncpg://" in manager.database_url

    def test_database_path_env_overrides_default(self):
        """DATABASE_PATH env var takes priority for SQLite path."""
        env = {"DATABASE_PATH": "/custom/path/my.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "/custom/path/my.db" in manager.database_url
            assert "sqlite+aiosqlite" in manager.database_url

    def test_database_dir_env_builds_path(self):
        """DATABASE_DIR env var appends default filename."""
        env = {"DATABASE_DIR": "/custom/dir"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "/custom/dir/telegram_backup.db" in manager.database_url

    def test_db_path_env_used_as_fallback(self):
        """DB_PATH env var is used when DATABASE_PATH and DATABASE_DIR are not set."""
        env = {"DB_PATH": "/fallback/path.db"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "/fallback/path.db" in manager.database_url

    def test_backup_path_env_used_as_last_resort(self):
        """BACKUP_PATH env var is used for the default SQLite location."""
        env = {"BACKUP_PATH": "/data/mybackups"}
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert "/data/mybackups/telegram_backup.db" in manager.database_url

    def test_password_with_special_chars_is_url_encoded(self):
        """PostgreSQL password with special characters is URL-encoded."""
        env = {
            "DB_TYPE": "postgresql",
            "POSTGRES_PASSWORD": "x@y/z",
        }
        with patch.dict(os.environ, env, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            # @ should be encoded as %40
            assert "%40" in manager.database_url
            assert "x@y" not in manager.database_url


# ============================================================
# URL conversion (sync to async)
# ============================================================


class TestConvertToAsyncUrl:
    """Test _convert_to_async_url for driver replacement."""

    def _make_manager(self):
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            return DatabaseManager()

    def test_converts_sqlite_to_aiosqlite(self):
        """sqlite:/// URL is converted to sqlite+aiosqlite:///."""
        manager = self._make_manager()
        result = manager._convert_to_async_url("sqlite:///path/to/db.sqlite")
        assert result == "sqlite+aiosqlite:///path/to/db.sqlite"

    def test_converts_postgresql_to_asyncpg(self):
        """postgresql:// URL is converted to postgresql+asyncpg://."""
        manager = self._make_manager()
        result = manager._convert_to_async_url("postgresql://user:pass@host/db")
        assert result == "postgresql+asyncpg://user:pass@host/db"

    def test_converts_postgres_to_asyncpg(self):
        """postgres:// URL (Heroku-style) is converted to postgresql+asyncpg://."""
        manager = self._make_manager()
        result = manager._convert_to_async_url("postgres://user:pass@host/db")
        assert result == "postgresql+asyncpg://user:pass@host/db"

    def test_already_async_url_passes_through(self):
        """Already-async URLs pass through unchanged."""
        manager = self._make_manager()
        url = "sqlite+aiosqlite:///path/to/db.sqlite"
        assert manager._convert_to_async_url(url) == url

    def test_unknown_driver_passes_through(self):
        """Unknown driver URLs pass through unchanged."""
        manager = self._make_manager()
        url = "mysql+aiomysql://user:pass@host/db"
        assert manager._convert_to_async_url(url) == url


# ============================================================
# _check_is_sqlite
# ============================================================


class TestCheckIsSqlite:
    """Test _check_is_sqlite flag detection."""

    def test_returns_true_for_sqlite_url(self):
        """SQLite URL sets _is_sqlite to True."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert manager._is_sqlite is True

    def test_returns_false_for_postgresql_url(self):
        """PostgreSQL URL sets _is_sqlite to False."""
        url = "postgresql+asyncpg://user:pass@host/db"
        manager = DatabaseManager(database_url=url)
        assert manager._is_sqlite is False


# ============================================================
# Constructor with explicit URL
# ============================================================


class TestConstructorWithUrl:
    """Test DatabaseManager constructor with explicit database_url."""

    def test_accepts_sync_sqlite_url_and_converts(self):
        """Sync sqlite URL is auto-converted to async."""
        manager = DatabaseManager(database_url="sqlite:///test.db")
        assert "aiosqlite" in manager.database_url

    def test_accepts_sync_postgresql_url_and_converts(self):
        """Sync postgresql URL is auto-converted to async."""
        manager = DatabaseManager(database_url="postgresql://u:p@h/d")
        assert "asyncpg" in manager.database_url

    def test_accepts_async_url_directly(self):
        """Already-async URL is used as-is."""
        url = "postgresql+asyncpg://user:pass@localhost/testdb"
        manager = DatabaseManager(database_url=url)
        assert manager.database_url == url


# ============================================================
# _db_type human-readable name
# ============================================================


class TestDbType:
    """Test _db_type returns human-readable database type."""

    def test_returns_sqlite_for_sqlite_url(self):
        """SQLite URL returns 'SQLite'."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            assert manager._db_type() == "SQLite"

    def test_returns_postgresql_for_pg_url(self):
        """PostgreSQL URL returns 'PostgreSQL'."""
        manager = DatabaseManager(database_url="postgresql+asyncpg://u:p@h/d")
        assert manager._db_type() == "PostgreSQL"

    def test_returns_unknown_for_other_url(self):
        """Unknown URL returns 'Unknown'."""
        manager = DatabaseManager(database_url="mysql+aiomysql://u:p@h/d")
        assert manager._db_type() == "Unknown"


# ============================================================
# _safe_url redaction
# ============================================================


class TestSafeUrl:
    """Test _safe_url redacts credentials for logging."""

    def test_sqlite_url_has_no_credentials(self):
        """SQLite safe URL has no sensitive data."""
        with patch.dict(os.environ, {"BACKUP_PATH": "/data"}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            safe = manager._safe_url()
            assert "sqlite+aiosqlite" in safe
            assert "password" not in safe.lower()

    def test_postgresql_url_masks_password(self):
        """PostgreSQL safe URL replaces password with ***."""
        env = {
            "DB_TYPE": "postgresql",
            "POSTGRES_HOST": "dbhost",
            "POSTGRES_PORT": "5432",
            "POSTGRES_USER": "admin",
            "POSTGRES_PASSWORD": "supersecret",
            "POSTGRES_DB": "mydb",
        }
        manager = DatabaseManager(database_url="postgresql+asyncpg://admin:supersecret@dbhost:5432/mydb")
        manager._is_sqlite = False
        with patch.dict(os.environ, env, clear=True):
            safe = manager._safe_url()
            assert "***" in safe
            assert "supersecret" not in safe
            assert "admin" in safe


# ============================================================
# session() factory
# ============================================================


class TestSessionFactory:
    """Test session() and get_session() methods."""

    def test_session_raises_when_not_initialized(self):
        """session() raises RuntimeError when database not initialized."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            # async_session_factory is None before init()
            with pytest.raises(RuntimeError, match="not initialized"):
                manager.session()

    @pytest.mark.asyncio
    async def test_get_session_raises_when_not_initialized(self):
        """get_session() raises RuntimeError when database not initialized."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            with pytest.raises(RuntimeError, match="not initialized"):
                async with manager.get_session():
                    pass

    def test_session_returns_factory_after_init(self):
        """session() returns the session factory when initialized."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            mock_factory = MagicMock()
            manager.async_session_factory = mock_factory
            assert manager.session() is mock_factory


# ============================================================
# close()
# ============================================================


class TestClose:
    """Test close() disposes the engine."""

    @pytest.mark.asyncio
    async def test_close_disposes_engine(self):
        """close() calls engine.dispose() when engine exists."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            mock_engine = AsyncMock()
            manager.engine = mock_engine

            await manager.close()
            mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_does_nothing_when_no_engine(self):
        """close() does nothing when engine is None."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            manager.engine = None
            # Should not raise
            await manager.close()


# ============================================================
# health_check()
# ============================================================


class TestHealthCheck:
    """Test health_check() method."""

    @pytest.mark.asyncio
    async def test_health_check_returns_true_on_success(self):
        """health_check returns True when SELECT 1 succeeds."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_factory = MagicMock(return_value=mock_ctx)
            manager.async_session_factory = mock_factory

            result = await manager.health_check()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_failure(self):
        """health_check returns False when database query fails."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            mock_session = AsyncMock()
            mock_session.execute.side_effect = Exception("connection refused")
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_factory = MagicMock(return_value=mock_ctx)
            manager.async_session_factory = mock_factory

            result = await manager.health_check()
            assert result is False


# ============================================================
# Global functions: get_db_manager, init_database, close_database
# ============================================================


class TestGlobalFunctions:
    """Test module-level get_db_manager, init_database, close_database."""

    @pytest.mark.asyncio
    async def test_init_database_creates_and_inits_manager(self):
        """init_database creates a DatabaseManager and calls init()."""
        with (
            patch("src.db.base.DatabaseManager") as MockManager,
            patch.dict(os.environ, {}, clear=True),
        ):
            mock_instance = AsyncMock()
            MockManager.return_value = mock_instance

            import src.db.base as base_mod

            base_mod._db_manager = None

            result = await init_database("sqlite+aiosqlite:///test.db")
            MockManager.assert_called_once_with("sqlite+aiosqlite:///test.db")
            mock_instance.init.assert_awaited_once()
            assert result is mock_instance

            # Cleanup
            base_mod._db_manager = None

    @pytest.mark.asyncio
    async def test_close_database_closes_and_clears_global(self):
        """close_database closes the manager and sets global to None."""
        import src.db.base as base_mod

        mock_manager = AsyncMock()
        base_mod._db_manager = mock_manager

        await close_database()

        mock_manager.close.assert_awaited_once()
        assert base_mod._db_manager is None

    @pytest.mark.asyncio
    async def test_close_database_does_nothing_when_no_manager(self):
        """close_database does nothing when global manager is None."""
        import src.db.base as base_mod

        base_mod._db_manager = None

        # Should not raise
        await close_database()
        assert base_mod._db_manager is None

    @pytest.mark.asyncio
    async def test_get_db_manager_creates_when_none(self):
        """get_db_manager creates and initializes a manager when global is None."""
        import src.db.base as base_mod

        base_mod._db_manager = None

        with patch("src.db.base.DatabaseManager") as MockManager:
            mock_instance = AsyncMock()
            MockManager.return_value = mock_instance

            result = await get_db_manager()
            mock_instance.init.assert_awaited_once()
            assert result is mock_instance

        # Cleanup
        base_mod._db_manager = None

    @pytest.mark.asyncio
    async def test_get_db_manager_returns_existing(self):
        """get_db_manager returns existing manager without re-initializing."""
        import src.db.base as base_mod

        mock_manager = MagicMock()
        base_mod._db_manager = mock_manager

        result = await get_db_manager()
        assert result is mock_manager

        # Cleanup
        base_mod._db_manager = None


# ============================================================
# init() PostgreSQL path (line 118)
# ============================================================


class TestInitPostgresql:
    """Test init() with PostgreSQL URL (line 118)."""

    @pytest.mark.asyncio
    async def test_init_postgresql_creates_pooled_engine(self):
        """init() creates a pooled engine for PostgreSQL URLs."""
        manager = DatabaseManager(database_url="postgresql+asyncpg://u:p@localhost/db")

        with (
            patch("src.db.base.create_async_engine") as mock_create,
            patch("src.db.base.async_sessionmaker"),
        ):
            mock_engine = AsyncMock()
            mock_create.return_value = mock_engine

            await manager.init()

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["pool_size"] == 5
        assert call_kwargs["pool_pre_ping"] is True


# ============================================================
# init() SQLite create_all exception (lines 141-144)
# ============================================================


class TestInitSqliteCreateAllException:
    """Test init() SQLite create_all failure (lines 141-144)."""

    @pytest.mark.asyncio
    async def test_create_all_exception_caught_for_readonly_db(self):
        """Exception in create_all is caught for read-only databases."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()

        mock_engine = AsyncMock()

        @asynccontextmanager
        async def fake_begin():
            mock_conn = AsyncMock()
            mock_conn.run_sync = AsyncMock(side_effect=Exception("read-only filesystem"))
            yield mock_conn

        mock_engine.begin = fake_begin
        mock_engine.sync_engine = MagicMock()

        with (
            patch("src.db.base.create_async_engine", return_value=mock_engine),
            patch("src.db.base.async_sessionmaker"),
            patch("src.db.base.event"),
        ):
            await manager.init()


# ============================================================
# _setup_sqlite_pragmas exception paths (lines 165-166, 175-176)
# ============================================================


class TestSetupSqlitePragmasExceptions:
    """Test _setup_sqlite_pragmas WAL failure and read-only PRAGMAs (lines 165-166, 175-176)."""

    @pytest.mark.asyncio
    async def test_wal_pragma_failure_caught(self):
        """WAL mode PRAGMA failure is caught for read-only databases."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()

        mock_engine = AsyncMock()

        @asynccontextmanager
        async def fake_begin():
            mock_conn = AsyncMock()
            mock_conn.run_sync = AsyncMock()
            yield mock_conn

        mock_engine.begin = fake_begin
        mock_engine.sync_engine = MagicMock()

        registered_listener = [None]

        def capture_listener(engine, event_name):
            def decorator(fn):
                registered_listener[0] = fn
                return fn

            return decorator

        with (
            patch("src.db.base.create_async_engine", return_value=mock_engine),
            patch("src.db.base.async_sessionmaker"),
            patch("src.db.base.event.listens_for", side_effect=capture_listener),
        ):
            await manager.init()

        # Now call the registered listener with a mock connection
        if registered_listener[0]:
            mock_dbapi = MagicMock()
            mock_cursor = MagicMock()
            mock_dbapi.cursor.return_value = mock_cursor
            # First two calls (WAL+synchronous) raise, busy_timeout + cache_size raise too
            mock_cursor.execute.side_effect = Exception("read-only")
            registered_listener[0](mock_dbapi, None)
            mock_cursor.close.assert_called_once()


# ============================================================
# _safe_url SQLite with DATABASE_DIR (line 199)
# ============================================================


class TestSafeUrlDatabaseDir:
    """Test _safe_url when DATABASE_DIR is set (line 199)."""

    def test_safe_url_uses_database_dir(self):
        """_safe_url uses DATABASE_DIR when DATABASE_PATH is not set."""
        with patch.dict(os.environ, {"DATABASE_DIR": "/my/custom/dir"}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()
            safe = manager._safe_url()
            assert "/my/custom/dir/telegram_backup.db" in safe


# ============================================================
# get_session rollback on exception (lines 223-229)
# ============================================================


class TestGetSessionRollback:
    """Test get_session() rollback on exception (lines 223-229)."""

    @pytest.mark.asyncio
    async def test_get_session_rolls_back_on_exception(self):
        """get_session() rolls back and re-raises when body raises."""
        with patch.dict(os.environ, {}, clear=True), patch("os.makedirs"):
            manager = DatabaseManager()

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_factory = MagicMock(return_value=mock_ctx)
        manager.async_session_factory = mock_factory

        with pytest.raises(ValueError, match="test error"):
            async with manager.get_session() as session:
                raise ValueError("test error")

        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()
