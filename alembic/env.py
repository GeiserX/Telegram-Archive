"""
Alembic environment configuration for async SQLAlchemy.

This file configures Alembic to work with:
- Async SQLAlchemy (using asyncpg for PostgreSQL, aiosqlite for SQLite)
- Environment-based database URL configuration
- Automatic model detection for autogenerate
"""

import asyncio
import os
import sys
from logging.config import fileConfig
from urllib.parse import quote_plus

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Add the src directory to the path so we can import our models
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import our models - this registers them with the Base metadata
from src.db.models import Base

# This is the Alembic Config object
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set the target metadata for 'autogenerate' support
target_metadata = Base.metadata


def get_database_url() -> str:
    """
    Get database URL from environment, converting to async driver if needed.
    
    Priority:
    1. DATABASE_URL environment variable
    2. DB_TYPE + related variables
    3. Default SQLite path
    """
    # Check for DATABASE_URL first
    database_url = os.getenv('DATABASE_URL')
    if database_url:
        # Convert to async driver if needed
        if database_url.startswith('sqlite:///'):
            return database_url.replace('sqlite:///', 'sqlite+aiosqlite:///')
        elif database_url.startswith('postgresql://'):
            return database_url.replace('postgresql://', 'postgresql+asyncpg://')
        elif database_url.startswith('postgres://'):
            return database_url.replace('postgres://', 'postgresql+asyncpg://')
        return database_url
    
    # Build from individual variables
    db_type = os.getenv('DB_TYPE', 'sqlite').lower()
    
    if db_type in ('postgresql', 'postgres'):
        host = os.getenv('POSTGRES_HOST', 'localhost')
        port = os.getenv('POSTGRES_PORT', '5432')
        user = quote_plus(os.getenv('POSTGRES_USER', 'telegram'))
        password = quote_plus(os.getenv('POSTGRES_PASSWORD', ''))
        database = os.getenv('POSTGRES_DB', 'telegram_backup')
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"
    
    # Default: SQLite - check v2 env vars first for backward compatibility
    db_path = os.getenv('DATABASE_PATH')  # v2: full path
    if not db_path:
        db_dir = os.getenv('DATABASE_DIR')  # v2: directory only
        if db_dir:
            db_path = os.path.join(db_dir, 'telegram_backup.db')
    if not db_path:
        db_path = os.getenv('DB_PATH')  # v3: new variable
    if not db_path:
        backup_path = os.getenv('BACKUP_PATH', '/data/backups')
        db_path = os.path.join(backup_path, 'telegram_backup.db')
    return f"sqlite+aiosqlite:///{db_path}"


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    
    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.
    
    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations within a connection context."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Compare types for detecting column type changes
        compare_type=True,
        # Compare server defaults
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations in 'online' mode with async engine.
    
    Creates an async Engine and associates a connection with the context.
    """
    # Override the sqlalchemy.url in the config
    configuration = config.get_section(config.config_ini_section) or {}
    configuration['sqlalchemy.url'] = get_database_url()
    
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
