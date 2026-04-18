"""Tests for database migration utilities - SQLite to PostgreSQL migration."""

import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.migrate import _migrate_table, migrate_sqlite_to_postgres, verify_migration

# ============================================================
# Helper: build a mock DatabaseManager whose get_session()
# returns a proper @asynccontextmanager (not a coroutine).
# ============================================================


def _make_mock_manager(session_mock=None):
    """Create a mock DatabaseManager with a working get_session() context manager.

    get_session() in base.py is decorated with @asynccontextmanager, so we
    must replicate that protocol: calling it returns an async CM, not a coroutine.
    """
    manager = AsyncMock()
    mock_session = session_mock or AsyncMock()

    @asynccontextmanager
    async def fake_get_session():
        yield mock_session

    manager.get_session = fake_get_session
    return manager, mock_session


def _make_mock_engine_begin(conn_mock=None):
    """Create a mock for engine.begin() that returns an async CM."""
    mock_conn = conn_mock or AsyncMock()

    @asynccontextmanager
    async def fake_begin():
        yield mock_conn

    return fake_begin, mock_conn


# ============================================================
# migrate_sqlite_to_postgres: path resolution
# ============================================================


class TestMigrateSqliteToPostgresPathResolution:
    """Test SQLite path resolution from environment variables."""

    @pytest.mark.asyncio
    async def test_raises_file_not_found_when_sqlite_missing(self):
        """Raises FileNotFoundError when SQLite file does not exist."""
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(FileNotFoundError, match="SQLite database not found"),
        ):
            await migrate_sqlite_to_postgres(
                sqlite_path="/nonexistent/path.db",
                postgres_url="postgresql+asyncpg://u:p@h/d",
            )

    @pytest.mark.asyncio
    async def test_resolves_database_path_env(self):
        """DATABASE_PATH env var is used for SQLite path resolution."""
        env = {"DATABASE_PATH": "/nonexistent/from_env.db"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(FileNotFoundError, match="/nonexistent/from_env.db"),
        ):
            await migrate_sqlite_to_postgres(postgres_url="postgresql+asyncpg://u:p@h/d")

    @pytest.mark.asyncio
    async def test_resolves_database_dir_env(self):
        """DATABASE_DIR env var is used when DATABASE_PATH is not set."""
        env = {"DATABASE_DIR": "/nonexistent/dir"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(FileNotFoundError, match="/nonexistent/dir/telegram_backup.db"),
        ):
            await migrate_sqlite_to_postgres(postgres_url="postgresql+asyncpg://u:p@h/d")

    @pytest.mark.asyncio
    async def test_resolves_db_path_env(self):
        """DB_PATH env var is used when DATABASE_PATH and DATABASE_DIR are not set."""
        env = {"DB_PATH": "/nonexistent/v3path.db"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(FileNotFoundError, match="/nonexistent/v3path.db"):
            await migrate_sqlite_to_postgres(postgres_url="postgresql+asyncpg://u:p@h/d")

    @pytest.mark.asyncio
    async def test_resolves_backup_path_env_as_fallback(self):
        """BACKUP_PATH env var is used as the last-resort fallback."""
        env = {"BACKUP_PATH": "/nonexistent/backups"}
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(FileNotFoundError, match="/nonexistent/backups/telegram_backup.db"),
        ):
            await migrate_sqlite_to_postgres(postgres_url="postgresql+asyncpg://u:p@h/d")

    @pytest.mark.asyncio
    async def test_resolves_default_path_when_no_env_set(self):
        """Default path /data/backups is used when no env vars are set."""
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(FileNotFoundError, match="/data/backups/telegram_backup.db"),
        ):
            await migrate_sqlite_to_postgres(postgres_url="postgresql+asyncpg://u:p@h/d")


# ============================================================
# migrate_sqlite_to_postgres: PostgreSQL URL resolution
# ============================================================


class TestMigratePostgresUrlResolution:
    """Test PostgreSQL URL resolution from environment variables."""

    @pytest.mark.asyncio
    async def test_builds_postgres_url_from_env_vars(self):
        """PostgreSQL URL is built from POSTGRES_* env vars."""
        env = {
            "POSTGRES_HOST": "pghost",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "pguser",
            "POSTGRES_PASSWORD": "pgpass",
            "POSTGRES_DB": "pgdb",
        }

        with patch.dict(os.environ, env, clear=True), pytest.raises(FileNotFoundError):
            await migrate_sqlite_to_postgres(sqlite_path="/nonexistent.db")


# ============================================================
# migrate_sqlite_to_postgres: full migration flow (mocked)
# ============================================================


class TestMigrateFullFlow:
    """Test the full migration flow with mocked database managers."""

    @pytest.mark.asyncio
    async def test_migration_calls_init_and_close_on_both(self):
        """Migration initializes and closes both source and target managers."""
        mock_source, mock_src_session = _make_mock_manager()
        mock_target, _ = _make_mock_manager()

        # Mock engine.begin() as an async context manager
        mock_conn = AsyncMock()
        mock_conn.run_sync = AsyncMock()
        fake_begin, _ = _make_mock_engine_begin(mock_conn)
        mock_target.engine.begin = fake_begin

        # Source session returns count=0 for all tables
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_count_result

        with (
            patch("src.db.migrate.DatabaseManager") as MockDM,
            patch("os.path.exists", return_value=True),
        ):
            MockDM.side_effect = [mock_source, mock_target]

            result = await migrate_sqlite_to_postgres(
                sqlite_path="/fake/path.db",
                postgres_url="postgresql+asyncpg://u:p@h/d",
            )

        mock_source.init.assert_awaited_once()
        mock_target.init.assert_awaited_once()
        mock_source.close.assert_awaited_once()
        mock_target.close.assert_awaited_once()

        # All tables should have 0 records since source is empty
        for table in ["users", "chats", "messages", "media", "reactions", "sync_status", "metadata"]:
            assert result[table] == 0

    @pytest.mark.asyncio
    async def test_migration_closes_connections_on_table_error(self):
        """Both connections are closed when a table migration raises an error."""
        mock_source, mock_src_session = _make_mock_manager()
        mock_target, _ = _make_mock_manager()

        # engine.begin() succeeds
        mock_conn = AsyncMock()
        mock_conn.run_sync = AsyncMock()
        fake_begin, _ = _make_mock_engine_begin(mock_conn)
        mock_target.engine.begin = fake_begin

        # Source session raises during the first _migrate_table call
        mock_src_session.execute.side_effect = Exception("table migration failed")

        with (
            patch("src.db.migrate.DatabaseManager") as MockDM,
            patch("os.path.exists", return_value=True),
        ):
            MockDM.side_effect = [mock_source, mock_target]

            with pytest.raises(Exception, match="table migration failed"):
                await migrate_sqlite_to_postgres(
                    sqlite_path="/fake/path.db",
                    postgres_url="postgresql+asyncpg://u:p@h/d",
                )

        # Both should be closed via the finally block
        mock_source.close.assert_awaited_once()
        mock_target.close.assert_awaited_once()


# ============================================================
# _migrate_table
# ============================================================


class TestMigrateTable:
    """Test _migrate_table for individual table migration.

    Uses real SQLAlchemy models because select().select_from() validates
    the model argument at SQL construction time.
    """

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_table(self):
        """Empty source table returns 0 records migrated."""
        from src.db.models import Metadata

        mock_source, mock_session = _make_mock_manager()
        mock_target, _ = _make_mock_manager()

        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_session.execute.return_value = mock_count_result

        result = await _migrate_table(mock_source, mock_target, Metadata, batch_size=100)
        assert result == 0

    @pytest.mark.asyncio
    async def test_migrates_records_in_batches(self):
        """Records are read from source and written to target in batches."""
        from src.db.models import Metadata

        mock_record1 = MagicMock()
        mock_record2 = MagicMock()

        # Source session: first call returns count=2, second returns records
        mock_src_session = AsyncMock()
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 2

        mock_batch_result = MagicMock()
        mock_batch_result.scalars.return_value.all.return_value = [mock_record1, mock_record2]

        mock_src_session.execute.side_effect = [mock_count_result, mock_batch_result]

        mock_source, _ = _make_mock_manager(session_mock=mock_src_session)

        # Target session
        mock_tgt_session = AsyncMock()
        mock_target, _ = _make_mock_manager(session_mock=mock_tgt_session)

        result = await _migrate_table(mock_source, mock_target, Metadata, batch_size=1000)
        assert result == 2

        # Records should be expunged from source and merged into target
        assert mock_src_session.expunge.call_count == 2
        assert mock_tgt_session.merge.await_count == 2
        mock_tgt_session.commit.assert_awaited_once()


# ============================================================
# verify_migration: path resolution
# ============================================================


class TestVerifyMigrationPathResolution:
    """Test verify_migration SQLite path resolution."""

    @pytest.mark.asyncio
    async def test_resolves_database_path_env(self):
        """DATABASE_PATH env var is used for verify_migration path resolution."""
        env = {"DATABASE_PATH": "/verify/path.db"}

        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with (
            patch.dict(os.environ, env, clear=True),
            patch("src.db.migrate.DatabaseManager") as MockDM,
        ):
            MockDM.side_effect = [mock_source, mock_target]

            result = await verify_migration(postgres_url="postgresql+asyncpg://u:p@h/d")

            # Source should be created with sqlite URL containing the path
            first_call_url = MockDM.call_args_list[0][0][0]
            assert "/verify/path.db" in first_call_url


# ============================================================
# verify_migration: full flow
# ============================================================


class TestVerifyMigrationFlow:
    """Test verify_migration compares record counts."""

    @pytest.mark.asyncio
    async def test_returns_matching_counts(self):
        """verify_migration returns matching counts for all tables."""
        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        # Both source and target return the same count
        mock_result = MagicMock()
        mock_result.scalar.return_value = 100
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with patch("src.db.migrate.DatabaseManager") as MockDM:
            MockDM.side_effect = [mock_source, mock_target]

            result = await verify_migration(
                sqlite_path="/fake/path.db",
                postgres_url="postgresql+asyncpg://u:p@h/d",
            )

        # Should have entries for all 7 tables
        assert len(result) == 7
        for _table_name, counts in result.items():
            assert counts["sqlite"] == 100
            assert counts["postgres"] == 100
            assert counts["match"] is True

    @pytest.mark.asyncio
    async def test_detects_count_mismatch(self):
        """verify_migration detects when source and target counts differ."""
        mock_src_session = AsyncMock()
        mock_tgt_session = AsyncMock()

        mock_src_result = MagicMock()
        mock_src_result.scalar.return_value = 100
        mock_src_session.execute.return_value = mock_src_result

        mock_tgt_result = MagicMock()
        mock_tgt_result.scalar.return_value = 50
        mock_tgt_session.execute.return_value = mock_tgt_result

        mock_source, _ = _make_mock_manager(session_mock=mock_src_session)
        mock_target, _ = _make_mock_manager(session_mock=mock_tgt_session)

        with patch("src.db.migrate.DatabaseManager") as MockDM:
            MockDM.side_effect = [mock_source, mock_target]

            result = await verify_migration(
                sqlite_path="/fake/path.db",
                postgres_url="postgresql+asyncpg://u:p@h/d",
            )

        # All tables should show mismatch
        for _table_name, counts in result.items():
            assert counts["sqlite"] == 100
            assert counts["postgres"] == 50
            assert counts["match"] is False

    @pytest.mark.asyncio
    async def test_closes_connections_after_verification(self):
        """verify_migration closes both connections even after success."""
        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with patch("src.db.migrate.DatabaseManager") as MockDM:
            MockDM.side_effect = [mock_source, mock_target]

            await verify_migration(
                sqlite_path="/fake/path.db",
                postgres_url="postgresql+asyncpg://u:p@h/d",
            )

        mock_source.close.assert_awaited_once()
        mock_target.close.assert_awaited_once()


# ============================================================
# migrate_sqlite_to_postgres: PostgreSQL URL from env (lines 69-74)
# ============================================================


class TestMigratePostgresUrlFromEnv:
    """Test PostgreSQL URL built from env vars when not provided (lines 69-74)."""

    @pytest.mark.asyncio
    async def test_builds_url_from_env_with_special_chars(self):
        """PostgreSQL password with special chars is URL-encoded (line 72)."""
        env = {
            "POSTGRES_HOST": "dbhost",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "admin",
            "POSTGRES_PASSWORD": "x@y/z",
            "POSTGRES_DB": "mydb",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(FileNotFoundError),
        ):
            await migrate_sqlite_to_postgres(sqlite_path="/nonexistent.db")


# ============================================================
# _migrate_table: empty batch breaks loop (line 144)
# ============================================================


class TestMigrateTableEmptyBatch:
    """Test _migrate_table breaks when batch is empty (line 144)."""

    @pytest.mark.asyncio
    async def test_empty_batch_breaks_loop(self):
        """When a batch query returns no records, migration loop breaks."""
        from src.db.models import Metadata

        mock_src_session = AsyncMock()

        # First call: count returns 5
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 5

        # Second call: batch returns empty
        mock_batch_result = MagicMock()
        mock_batch_result.scalars.return_value.all.return_value = []

        mock_src_session.execute.side_effect = [mock_count_result, mock_batch_result]

        mock_source, _ = _make_mock_manager(session_mock=mock_src_session)
        mock_target, _ = _make_mock_manager()

        result = await _migrate_table(mock_source, mock_target, Metadata, batch_size=100)
        assert result == 0


# ============================================================
# _migrate_table: progress logging at 10000 intervals (line 158)
# ============================================================


class TestMigrateTableProgressLogging:
    """Test _migrate_table logs progress at 10000 intervals (line 158)."""

    @pytest.mark.asyncio
    async def test_logs_progress_at_10000_records(self):
        """Progress is logged when total reaches 10000 records."""
        from src.db.models import Metadata

        mock_src_session = AsyncMock()

        # Count: 10000 records
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 10000

        # Return 10000 records in one batch, then empty
        records = [MagicMock() for _ in range(10000)]
        mock_batch_result = MagicMock()
        mock_batch_result.scalars.return_value.all.return_value = records

        mock_empty_result = MagicMock()
        mock_empty_result.scalars.return_value.all.return_value = []

        mock_src_session.execute.side_effect = [mock_count_result, mock_batch_result, mock_empty_result]

        mock_source, _ = _make_mock_manager(session_mock=mock_src_session)

        mock_tgt_session = AsyncMock()
        mock_target, _ = _make_mock_manager(session_mock=mock_tgt_session)

        result = await _migrate_table(mock_source, mock_target, Metadata, batch_size=20000)
        assert result == 10000


# ============================================================
# verify_migration: path resolution variants (lines 175-177, 179, 181-182, 185-190)
# ============================================================


class TestVerifyMigrationPathVariants:
    """Test verify_migration path resolution from various env vars."""

    @pytest.mark.asyncio
    async def test_resolves_database_dir_env(self):
        """DATABASE_DIR env is used when DATABASE_PATH is not set (lines 175-177)."""
        env = {"DATABASE_DIR": "/verify/dir"}

        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with (
            patch.dict(os.environ, env, clear=True),
            patch("src.db.migrate.DatabaseManager") as MockDM,
        ):
            MockDM.side_effect = [mock_source, mock_target]
            await verify_migration(postgres_url="postgresql+asyncpg://u:p@h/d")

            first_call_url = MockDM.call_args_list[0][0][0]
            assert "/verify/dir/telegram_backup.db" in first_call_url

    @pytest.mark.asyncio
    async def test_resolves_db_path_env(self):
        """DB_PATH env is used as fallback (line 179)."""
        env = {"DB_PATH": "/verify/v3.db"}

        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with (
            patch.dict(os.environ, env, clear=True),
            patch("src.db.migrate.DatabaseManager") as MockDM,
        ):
            MockDM.side_effect = [mock_source, mock_target]
            await verify_migration(postgres_url="postgresql+asyncpg://u:p@h/d")

            first_call_url = MockDM.call_args_list[0][0][0]
            assert "/verify/v3.db" in first_call_url

    @pytest.mark.asyncio
    async def test_resolves_backup_path_fallback(self):
        """BACKUP_PATH is used as last resort (lines 181-182)."""
        env = {"BACKUP_PATH": "/verify/backups"}

        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with (
            patch.dict(os.environ, env, clear=True),
            patch("src.db.migrate.DatabaseManager") as MockDM,
        ):
            MockDM.side_effect = [mock_source, mock_target]
            await verify_migration(postgres_url="postgresql+asyncpg://u:p@h/d")

            first_call_url = MockDM.call_args_list[0][0][0]
            assert "/verify/backups/telegram_backup.db" in first_call_url

    @pytest.mark.asyncio
    async def test_builds_postgres_url_from_env(self):
        """PostgreSQL URL built from env when not provided (lines 185-190)."""
        env = {
            "POSTGRES_HOST": "pghost",
            "POSTGRES_PORT": "5433",
            "POSTGRES_USER": "pguser",
            "POSTGRES_PASSWORD": "pgpass",
            "POSTGRES_DB": "pgdb",
        }

        mock_source, mock_src_session = _make_mock_manager()
        mock_target, mock_tgt_session = _make_mock_manager()

        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_src_session.execute.return_value = mock_result
        mock_tgt_session.execute.return_value = mock_result

        with (
            patch.dict(os.environ, env, clear=True),
            patch("src.db.migrate.DatabaseManager") as MockDM,
        ):
            MockDM.side_effect = [mock_source, mock_target]
            await verify_migration(sqlite_path="/fake/path.db")

            second_call_url = MockDM.call_args_list[1][0][0]
            assert "pghost:5433" in second_call_url
            assert "pguser" in second_call_url
