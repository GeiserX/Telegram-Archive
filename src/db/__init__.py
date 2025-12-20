"""
Database package for Telegram Backup.

Provides async database access using SQLAlchemy for both SQLite and PostgreSQL.

Usage:
    # Initialize database (call once at startup)
    from src.db import init_database, get_adapter
    
    db_manager = await init_database()
    db = await get_adapter()
    
    # Use the adapter
    await db.upsert_chat({'id': 123, 'type': 'private', 'title': 'Test'})
    chats = await db.get_all_chats()
    
    # Configuration via environment variables:
    #
    # Option 1: DATABASE_URL (takes priority)
    #   DATABASE_URL=sqlite:////data/backups/telegram_backup.db
    #   DATABASE_URL=postgresql://user:pass@localhost:5432/telegram_backup
    #
    # Option 2: Separate variables
    #   DB_TYPE=sqlite (default) or postgresql
    #   DB_PATH=/data/backups/telegram_backup.db (for SQLite)
    #   POSTGRES_HOST=localhost
    #   POSTGRES_PORT=5432
    #   POSTGRES_USER=telegram
    #   POSTGRES_PASSWORD=secret
    #   POSTGRES_DB=telegram_backup
"""

from typing import Optional
from .models import Base, Chat, Message, User, Media, Reaction, SyncStatus, Metadata
from .base import DatabaseManager, init_database, close_database, get_db_manager
from .adapter import DatabaseAdapter
from .migrate import migrate_sqlite_to_postgres, verify_migration

__all__ = [
    # Models
    'Base',
    'Chat',
    'Message',
    'User',
    'Media',
    'Reaction',
    'SyncStatus',
    'Metadata',
    # Database management
    'DatabaseManager',
    'init_database',
    'close_database',
    'get_db_manager',
    # Adapter
    'DatabaseAdapter',
    'get_adapter',
    # Migration
    'migrate_sqlite_to_postgres',
    'verify_migration',
]

# Global adapter instance
_adapter: Optional[DatabaseAdapter] = None


async def get_adapter() -> DatabaseAdapter:
    """
    Get or create the global database adapter.
    
    Returns:
        Initialized DatabaseAdapter instance
        
    Raises:
        RuntimeError: If database not initialized
    """
    global _adapter
    if _adapter is None:
        db_manager = await get_db_manager()
        _adapter = DatabaseAdapter(db_manager)
    return _adapter


async def create_adapter(database_url: Optional[str] = None) -> DatabaseAdapter:
    """
    Create a new database adapter with optional custom URL.
    
    Args:
        database_url: Optional database URL override
        
    Returns:
        New DatabaseAdapter instance
    """
    global _adapter
    db_manager = await init_database(database_url)
    _adapter = DatabaseAdapter(db_manager)
    return _adapter


async def close_adapter() -> None:
    """Close the global database adapter."""
    global _adapter
    if _adapter:
        await _adapter.close()
        _adapter = None
    await close_database()

