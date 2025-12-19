"""
Database engine and session management for async SQLAlchemy.

Supports both SQLite and PostgreSQL with proper configuration for each.
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator
from urllib.parse import urlparse, quote_plus

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine
)
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool

from .models import Base

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    Manages async database connections for SQLite and PostgreSQL.
    
    Configuration priority:
    1. DATABASE_URL environment variable (if set)
    2. Individual DB_* environment variables
    3. Default to SQLite at /data/backups/telegram_backup.db
    """
    
    def __init__(self, database_url: Optional[str] = None):
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
        self.engine: Optional[AsyncEngine] = None
        self.async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._is_sqlite = self._check_is_sqlite()
        
    def _build_database_url(self) -> str:
        """Build database URL from environment variables."""
        # Priority 1: DATABASE_URL
        database_url = os.getenv('DATABASE_URL')
        if database_url:
            # Convert sync URLs to async URLs if needed
            return self._convert_to_async_url(database_url)
        
        # Priority 2: DB_TYPE and related variables
        db_type = os.getenv('DB_TYPE', 'sqlite').lower()
        
        if db_type == 'postgresql' or db_type == 'postgres':
            host = os.getenv('POSTGRES_HOST', 'localhost')
            port = os.getenv('POSTGRES_PORT', '5432')
            user = quote_plus(os.getenv('POSTGRES_USER', 'telegram'))
            password = quote_plus(os.getenv('POSTGRES_PASSWORD', ''))
            database = os.getenv('POSTGRES_DB', 'telegram_backup')
            return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
        
        # Default: SQLite
        # Check v2 environment variables first for backward compatibility
        db_path = os.getenv('DATABASE_PATH')  # v2: full path
        if not db_path:
            db_dir = os.getenv('DATABASE_DIR')  # v2: directory only
            if db_dir:
                db_path = os.path.join(db_dir, 'telegram_backup.db')
        if not db_path:
            db_path = os.getenv('DB_PATH')  # v3: new variable
        if not db_path:
            # Default path (same as v2 default)
            backup_path = os.getenv('BACKUP_PATH', '/data/backups')
            db_path = os.path.join(backup_path, 'telegram_backup.db')
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"
    
    def _convert_to_async_url(self, url: str) -> str:
        """Convert a sync database URL to async driver URL."""
        if url.startswith('sqlite:///'):
            return url.replace('sqlite:///', 'sqlite+aiosqlite:///')
        elif url.startswith('postgresql://'):
            return url.replace('postgresql://', 'postgresql+asyncpg://')
        elif url.startswith('postgres://'):
            return url.replace('postgres://', 'postgresql+asyncpg://')
        # Already async or unknown - return as-is
        return url
    
    def _check_is_sqlite(self) -> bool:
        """Check if using SQLite database."""
        return 'sqlite' in self.database_url.lower()
    
    async def init(self) -> None:
        """Initialize the database engine and create tables if needed."""
        logger.info(f"Initializing database: {self._safe_url()}")
        
        # Engine configuration differs by database type
        if self._is_sqlite:
            # SQLite: Use NullPool for better async compatibility
            self.engine = create_async_engine(
                self.database_url,
                echo=os.getenv('DB_ECHO', 'false').lower() == 'true',
                poolclass=NullPool,
            )
            # Set up SQLite-specific pragmas
            self._setup_sqlite_pragmas()
        else:
            # PostgreSQL: Use connection pooling
            self.engine = create_async_engine(
                self.database_url,
                echo=os.getenv('DB_ECHO', 'false').lower() == 'true',
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
        
        # Create tables if they don't exist (checkfirst=True skips existing tables)
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=True))
        
        logger.info(f"Database initialized successfully ({self._db_type()})")
    
    def _setup_sqlite_pragmas(self) -> None:
        """Set up SQLite PRAGMA settings for optimal performance."""
        
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            # WAL mode for better concurrent read/write
            cursor.execute("PRAGMA journal_mode=WAL")
            # 60 second busy timeout
            cursor.execute("PRAGMA busy_timeout=60000")
            # Faster than FULL, still safe with WAL
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 64MB cache for better performance
            cursor.execute("PRAGMA cache_size=-64000")
            cursor.close()
            logger.debug("SQLite PRAGMAs set: WAL mode, 60s timeout, 64MB cache")
    
    def _db_type(self) -> str:
        """Get human-readable database type."""
        if self._is_sqlite:
            return "SQLite"
        elif 'postgresql' in self.database_url:
            return "PostgreSQL"
        return "Unknown"
    
    def _safe_url(self) -> str:
        """Return URL with password masked for logging."""
        parsed = urlparse(self.database_url)
        if parsed.password:
            return self.database_url.replace(parsed.password, '***')
        return self.database_url
    
    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
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
_db_manager: Optional[DatabaseManager] = None


async def get_db_manager() -> DatabaseManager:
    """Get or create the global database manager."""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
        await _db_manager.init()
    return _db_manager


async def init_database(database_url: Optional[str] = None) -> DatabaseManager:
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
