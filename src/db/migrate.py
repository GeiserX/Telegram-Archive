"""
Migration utilities for Telegram Backup database.

Provides tools to migrate data between SQLite and PostgreSQL.
"""

import os
import logging
from typing import AsyncGenerator, Dict, Any, List
from urllib.parse import quote_plus

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Base, Chat, Message, User, Media, Reaction, SyncStatus, Metadata
from .base import DatabaseManager

logger = logging.getLogger(__name__)


async def migrate_sqlite_to_postgres(
    sqlite_path: str = None,
    postgres_url: str = None,
    batch_size: int = 1000
) -> Dict[str, int]:
    """
    Migrate data from SQLite to PostgreSQL.
    
    Args:
        sqlite_path: Path to SQLite database file. 
                    Defaults to DB_PATH env var or /data/backups/telegram_backup.db
        postgres_url: PostgreSQL connection URL.
                     Defaults to building from POSTGRES_* env vars
        batch_size: Number of records to migrate per batch
        
    Returns:
        Dict with counts of migrated records per table
        
    Example:
        from src.db.migrate import migrate_sqlite_to_postgres
        import asyncio
        
        result = asyncio.run(migrate_sqlite_to_postgres())
        print(f"Migrated: {result}")
    """
    # Resolve SQLite path (use same default as v2 for backward compatibility)
    if sqlite_path is None:
        sqlite_path = os.getenv('DB_PATH', '/data/backups/telegram_backup.db')
    
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    
    # Resolve PostgreSQL URL
    if postgres_url is None:
        host = os.getenv('POSTGRES_HOST', 'localhost')
        port = os.getenv('POSTGRES_PORT', '5432')
        user = quote_plus(os.getenv('POSTGRES_USER', 'telegram'))
        password = quote_plus(os.getenv('POSTGRES_PASSWORD', ''))
        db = os.getenv('POSTGRES_DB', 'telegram_backup')
        postgres_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    
    sqlite_url = f"sqlite+aiosqlite:///{sqlite_path}"
    
    logger.info(f"Migrating from SQLite ({sqlite_path}) to PostgreSQL")
    
    # Initialize both database connections
    source = DatabaseManager(sqlite_url)
    await source.init()
    
    target = DatabaseManager(postgres_url)
    await target.init()
    
    # Create tables in PostgreSQL
    async with target.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    counts = {}
    
    try:
        # Migration order matters due to foreign key relationships
        # 1. Users (no dependencies)
        # 2. Chats (no dependencies)  
        # 3. Messages (depends on chats, users)
        # 4. Media (depends on messages)
        # 5. Reactions (depends on messages, users)
        # 6. SyncStatus (depends on chats)
        # 7. Metadata (no dependencies)
        
        counts['users'] = await _migrate_table(source, target, User, batch_size)
        counts['chats'] = await _migrate_table(source, target, Chat, batch_size)
        counts['messages'] = await _migrate_table(source, target, Message, batch_size)
        counts['media'] = await _migrate_table(source, target, Media, batch_size)
        counts['reactions'] = await _migrate_table(source, target, Reaction, batch_size)
        counts['sync_status'] = await _migrate_table(source, target, SyncStatus, batch_size)
        counts['metadata'] = await _migrate_table(source, target, Metadata, batch_size)
        
        logger.info(f"Migration complete: {counts}")
        
    finally:
        await source.close()
        await target.close()
    
    return counts


async def _migrate_table(
    source: DatabaseManager,
    target: DatabaseManager,
    model,
    batch_size: int
) -> int:
    """Migrate a single table from source to target."""
    table_name = model.__tablename__
    total = 0
    
    async with source.get_session() as src_session:
        # Get total count
        count_result = await src_session.execute(
            select(func.count()).select_from(model)
        )
        total_records = count_result.scalar() or 0
        
        if total_records == 0:
            logger.info(f"  {table_name}: 0 records (empty)")
            return 0
        
        logger.info(f"  {table_name}: migrating {total_records} records...")
        
        # Stream records in batches
        offset = 0
        while offset < total_records:
            # Read batch from source
            result = await src_session.execute(
                select(model).offset(offset).limit(batch_size)
            )
            records = result.scalars().all()
            
            if not records:
                break
            
            # Write batch to target
            async with target.get_session() as tgt_session:
                for record in records:
                    # Detach from source session and merge into target
                    src_session.expunge(record)
                    tgt_session.merge(record)
                await tgt_session.commit()
            
            total += len(records)
            offset += batch_size
            
            if total % 10000 == 0:
                logger.info(f"    {table_name}: {total}/{total_records} migrated")
    
    logger.info(f"  {table_name}: {total} records migrated")
    return total


async def verify_migration(
    sqlite_path: str = None,
    postgres_url: str = None
) -> Dict[str, Dict[str, int]]:
    """
    Verify migration by comparing record counts.
    
    Returns:
        Dict with table names and counts from both databases
    """
    if sqlite_path is None:
        sqlite_path = os.getenv('DB_PATH', '/data/backups/telegram_backup.db')
    
    if postgres_url is None:
        host = os.getenv('POSTGRES_HOST', 'localhost')
        port = os.getenv('POSTGRES_PORT', '5432')
        user = quote_plus(os.getenv('POSTGRES_USER', 'telegram'))
        password = quote_plus(os.getenv('POSTGRES_PASSWORD', ''))
        db = os.getenv('POSTGRES_DB', 'telegram_backup')
        postgres_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    
    sqlite_url = f"sqlite+aiosqlite:///{sqlite_path}"
    
    source = DatabaseManager(sqlite_url)
    await source.init()
    
    target = DatabaseManager(postgres_url)
    await target.init()
    
    results = {}
    models = [User, Chat, Message, Media, Reaction, SyncStatus, Metadata]
    
    try:
        for model in models:
            table_name = model.__tablename__
            
            async with source.get_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(model)
                )
                sqlite_count = result.scalar() or 0
            
            async with target.get_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(model)
                )
                postgres_count = result.scalar() or 0
            
            results[table_name] = {
                'sqlite': sqlite_count,
                'postgres': postgres_count,
                'match': sqlite_count == postgres_count
            }
    
    finally:
        await source.close()
        await target.close()
    
    return results
