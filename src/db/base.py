"""
Database engine and session management for async SQLAlchemy.

Supports both SQLite and PostgreSQL with proper configuration for each.
"""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from .models import Base

logger = logging.getLogger(__name__)


def _busy_timeout_ms() -> int:
    """DATABASE_TIMEOUT (seconds) as PRAGMA busy_timeout milliseconds."""
    raw = os.getenv("DATABASE_TIMEOUT", "60.0")
    try:
        seconds = float(raw)
    except ValueError:
        return 60000
    if seconds <= 0:
        return 60000
    return int(seconds * 1000)


class DatabaseManager:
    """
    Manages async database connections for SQLite and PostgreSQL.

    Configuration priority:
    1. DATABASE_URL environment variable (if set)
    2. Individual DB_* environment variables
    3. Default to SQLite at /data/backups/telegram_backup.db
    """

    def __init__(self, database_url: str | None = None):
        """
        Initialize database manager.

        Args:
            database_url: Optional database URL. If not provided, reads from environment.
                          URLs with sync drivers (sqlite://, postgresql://) are automatically
                          converted to async drivers (sqlite+aiosqlite://, postgresql+asyncpg://).
        """
        if database_url:
            # Convert sync URLs to async URLs if needed
            self.database_url = self._convert_to_async_url(database_url)
        else:
            self.database_url = self._build_database_url()
        self.engine: AsyncEngine | None = None
        self.async_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._is_sqlite = self._check_is_sqlite()

    def _build_database_url(self) -> str:
        """Build database URL from environment variables."""
        # Priority 1: DATABASE_URL
        database_url = os.getenv("DATABASE_URL")
        if database_url:
            # Convert sync URLs to async URLs if needed
            return self._convert_to_async_url(database_url)

        # Priority 2: DB_TYPE and related variables
        db_type = os.getenv("DB_TYPE", "sqlite").lower()

        if db_type == "postgresql" or db_type == "postgres":
            host = os.getenv("POSTGRES_HOST", "localhost")
            port = os.getenv("POSTGRES_PORT", "5432")
            user = quote_plus(os.getenv("POSTGRES_USER", "telegram"))
            password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
            database = os.getenv("POSTGRES_DB", "telegram_backup")
            return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"

        # Default: SQLite
        # Check v2 environment variables first for backward compatibility
        db_path = os.getenv("DATABASE_PATH")  # v2: full path
        if not db_path:
            db_dir = os.getenv("DATABASE_DIR")  # v2: directory only
            if db_dir:
                db_path = os.path.join(db_dir, "telegram_backup.db")
        if not db_path:
            db_path = os.getenv("DB_PATH")  # v3: new variable
        if not db_path:
            # Default path (same as v2 default)
            backup_path = os.getenv("BACKUP_PATH", "/data/backups")
            db_path = os.path.join(backup_path, "telegram_backup.db")

        # Resolve to absolute path so relative paths don't silently resolve
        # against WORKDIR (/app) in Docker containers (fixes #144)
        db_path = os.path.abspath(db_path)

        # Ensure directory exists
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"

    def _convert_to_async_url(self, url: str) -> str:
        """Convert a sync database URL to async driver URL."""
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "sqlite+aiosqlite:///")
        elif url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://")
        elif url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://")
        # Already async or unknown - return as-is
        return url

    def _check_is_sqlite(self) -> bool:
        """Check if using SQLite database."""
        return "sqlite" in self.database_url.lower()

    async def init(self) -> None:
        """Initialize the database engine and create tables if needed."""
        logger.info(f"Initializing database: {self._safe_url()}")

        # Engine configuration differs by database type
        if self._is_sqlite:
            # SQLite: Use NullPool for better async compatibility
            # hide_parameters: DB errors must never embed bound values (message
            # text, chat ids) in logs — PII rule.
            self.engine = create_async_engine(
                self.database_url,
                echo=os.getenv("DB_ECHO", "false").lower() == "true",
                poolclass=NullPool,
                hide_parameters=True,
            )
            # Set up SQLite-specific pragmas
            self._setup_sqlite_pragmas()
        else:
            # PostgreSQL: Use connection pooling
            # hide_parameters: DB errors must never embed bound values (message
            # text, chat ids) in logs — PII rule.
            self.engine = create_async_engine(
                self.database_url,
                echo=os.getenv("DB_ECHO", "false").lower() == "true",
                hide_parameters=True,
                poolclass=AsyncAdaptedQueuePool,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )

        # Create async session factory
        self.async_session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        # Alembic is the schema authority on both backends: scripts/entrypoint.sh
        # runs `upgrade head` before this process starts, on SQLite whether or not
        # the file existed. This create_all is only a fallback for a process that
        # never passes through that entrypoint — the viewer image, which ships no
        # alembic/ directory, or a direct `python -m src` run — and it is limited
        # to SQLite because on PostgreSQL it would race a concurrently migrating
        # container into a deadlock. tests/test_schema_parity.py builds both
        # schemas on both backends and fails on any difference, so the fallback
        # cannot quietly produce a different schema from the authority again.
        if self._is_sqlite:
            try:
                async with self.engine.begin() as conn:
                    created = await conn.run_sync(self._create_schema_if_absent)
                if not created:
                    logger.info("Existing schema found - leaving it to Alembic, no tables created")
            except Exception as e:
                # Viewer containers may mount the database read-only — that's fine,
                # the backup container is responsible for creating tables.
                logger.warning(f"Could not create/verify tables (database may be read-only): {e}")

        logger.info(f"Database initialized successfully ({self._db_type()})")

    @staticmethod
    def _create_schema_if_absent(sync_conn: Connection) -> bool:
        """Build the ORM schema, but only into a database that has none yet.

        Returns True if the schema was created, False if one was already there.

        The whole check-and-create runs on one connection inside ``init``'s
        transaction, so this process cannot race itself.

        Why the guard is not optional
        -----------------------------
        The viewer image ships no ``alembic/`` and has no ENTRYPOINT
        (``Dockerfile.viewer``), so it never migrates — but it does reach this
        line on every SQLite start, and ``docker compose up -d`` starts it
        alongside the backup container that *is* migrating. Unguarded,
        ``create_all(checkfirst=True)`` adds whole missing tables to a database
        a migration is halfway through rebuilding: the migration's own
        ``CREATE TABLE`` then dies with "table already exists" and the backup
        container crash-loops, against what is usually the only copy of
        someone's Telegram history.

        Any table at all means this database is not ours to build.
        ``alembic_version`` says so outright — Alembic owns this schema and is
        the only thing allowed to change it. Any other table means a previous
        run already provisioned it, and the entrypoint's stamping ladder is
        about to read that shape to decide which revision it is; adding a table
        underneath that read gets it stamped wrong, which is the same
        crash-loop by a different route.

        Skipping costs a viewer nothing: ``checkfirst=True`` only ever added
        whole missing tables, never a column, so it could never have made an
        older archive readable by a newer viewer anyway. A viewer against an
        existing archive still opens it and still serves it — the reason this
        fallback exists is the *fresh* database, and that case still works.

        The fresh case is also the one narrow window left open: with no
        database at all, the viewer may win the race and build the full ORM
        schema unstamped. That is the shape ``scripts/entrypoint.sh`` already
        detects and stamps, and that every migration is already required to
        survive as a no-op, so it converges — and there is no history in an
        empty database to lose while it does.
        """
        if inspect(sync_conn).get_table_names():
            return False
        Base.metadata.create_all(sync_conn, checkfirst=True)
        return True

    def _setup_sqlite_pragmas(self) -> None:
        """Set up SQLite PRAGMA settings for optimal performance.

        Gracefully handles read-only databases (e.g., viewer containers with
        read-only volume mounts or non-root users without write permissions).
        WAL mode requires write access to create .db-wal and .db-shm files;
        if that fails the database still works in the default journal mode.
        """

        # DATABASE_TIMEOUT is documented (README, .env.example) as THE knob for
        # "database is locked", but it never reached this pragma — the 60s
        # default happening to equal the old hardcoded 60000ms kept that
        # invisible. Seconds in, milliseconds out; invalid or non-positive
        # values keep the old default. Read from the environment like every
        # other configuration value (the manager is URL-driven, not
        # Config-driven, so importing Config here would invert the layers).
        busy_timeout_ms = _busy_timeout_ms()

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                # WAL mode for better concurrent read/write
                cursor.execute("PRAGMA journal_mode=WAL")
                # Faster than FULL, still safe with WAL
                cursor.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                logger.warning(
                    "Could not enable WAL mode (database may be read-only). "
                    "This is expected for viewer containers with read-only mounts."
                )
            try:
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                # 64MB cache for better performance
                cursor.execute("PRAGMA cache_size=-64000")
            except Exception:
                pass  # Read-only PRAGMAs are non-critical
            cursor.close()

    def _db_type(self) -> str:
        """Get human-readable database type."""
        if self._is_sqlite:
            return "SQLite"
        elif "postgresql" in self.database_url:
            return "PostgreSQL"
        return "Unknown"

    def _safe_url(self) -> str:
        """Return database URL for logging with credentials redacted.

        Builds entirely from non-sensitive env vars to avoid taint tracking
        (CodeQL py/clear-text-logging-sensitive-data).
        """
        if self._is_sqlite:
            # Reconstruct from env vars — SQLite URLs have no credentials
            db_path = os.getenv("DATABASE_PATH") or os.getenv("DB_PATH")
            if not db_path:
                db_dir = os.getenv("DATABASE_DIR")
                if db_dir:
                    db_path = os.path.join(db_dir, "telegram_backup.db")
            if not db_path:
                backup_path = os.getenv("BACKUP_PATH", "/data/backups")
                db_path = os.path.join(backup_path, "telegram_backup.db")
            db_path = os.path.abspath(db_path)
            return f"sqlite+aiosqlite:///{db_path}"
        # PostgreSQL — build from non-sensitive env vars, mask password
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        user = os.getenv("POSTGRES_USER", "telegram")
        db = os.getenv("POSTGRES_DB", "telegram_backup")
        return f"postgresql://{user}:***@{host}:{port}/{db}"

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession]:
        """
        Get an async database session.

        Usage:
            async with db_manager.get_session() as session:
                result = await session.execute(...)
        """
        if not self.async_session_factory:
            raise RuntimeError("Database not initialized. Call init() first.")

        async with self.async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def session(self) -> async_sessionmaker[AsyncSession]:
        """
        Get the session factory for dependency injection.

        Usage with FastAPI:
            @app.get("/items")
            async def get_items(session: AsyncSession = Depends(db_manager.session)):
                ...
        """
        if not self.async_session_factory:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self.async_session_factory

    async def close(self) -> None:
        """Close database connections."""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database connections closed")

    async def health_check(self) -> bool:
        """Check if database is accessible."""
        try:
            async with self.async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False


# Global database manager instance
_db_manager: DatabaseManager | None = None


async def get_db_manager() -> DatabaseManager:
    """Get or create the global database manager."""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
        await _db_manager.init()
    return _db_manager


async def init_database(database_url: str | None = None) -> DatabaseManager:
    """
    Initialize the global database manager.

    Args:
        database_url: Optional database URL override

    Returns:
        Initialized DatabaseManager instance
    """
    global _db_manager
    _db_manager = DatabaseManager(database_url)
    await _db_manager.init()
    return _db_manager


async def close_database() -> None:
    """Close the global database connection."""
    global _db_manager
    if _db_manager:
        await _db_manager.close()
        _db_manager = None
