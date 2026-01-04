#!/usr/bin/env python3
"""
SQLite to PostgreSQL Migration Script for Telegram Archive.

This script migrates your Telegram Archive data from SQLite to PostgreSQL.
It can be run standalone or via Docker.

Usage:
    # Via Docker (recommended)
    docker run --rm -it \
        -v /path/to/sqlite/db:/data/db:ro \
        -e POSTGRES_HOST=your-postgres-host \
        -e POSTGRES_PORT=5432 \
        -e POSTGRES_USER=telegram \
        -e POSTGRES_PASSWORD=your-password \
        -e POSTGRES_DB=telegram_backup \
        drumsergio/telegram-archive:latest \
        python scripts/migrate-sqlite-to-postgres.py

    # Or with explicit paths
    docker run --rm -it \
        -v /path/to/sqlite/telegram_backup.db:/sqlite.db:ro \
        -e DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname \
        drumsergio/telegram-archive:latest \
        python scripts/migrate-sqlite-to-postgres.py --sqlite /sqlite.db

Environment Variables:
    SQLITE_PATH or DATABASE_DIR or DB_PATH: Path to SQLite database
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
    Or: DATABASE_URL (full PostgreSQL URL)

Options:
    --sqlite PATH    : Explicit path to SQLite database file
    --postgres URL   : Explicit PostgreSQL connection URL  
    --batch-size N   : Records per batch (default: 1000)
    --verify-only    : Only verify migration, don't migrate
    --dry-run        : Show what would be migrated without doing it
"""

import asyncio
import argparse
import os
import sys
import logging
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.migrate import migrate_sqlite_to_postgres, verify_migration

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def resolve_sqlite_path(explicit_path: Optional[str] = None) -> str:
    """Resolve SQLite database path from various sources."""
    if explicit_path:
        return explicit_path
    
    # Check environment variables in order
    path = os.getenv('SQLITE_PATH')
    if path and os.path.exists(path):
        return path
    
    path = os.getenv('DATABASE_PATH')
    if path and os.path.exists(path):
        return path
    
    db_dir = os.getenv('DATABASE_DIR')
    if db_dir:
        path = os.path.join(db_dir, 'telegram_backup.db')
        if os.path.exists(path):
            return path
    
    path = os.getenv('DB_PATH')
    if path and os.path.exists(path):
        return path
    
    # Default locations
    default_paths = [
        '/data/db/telegram_backup.db',
        '/data/backups/telegram_backup.db',
        './telegram_backup.db',
    ]
    
    for path in default_paths:
        if os.path.exists(path):
            return path
    
    raise FileNotFoundError(
        "Could not find SQLite database. Please specify with --sqlite or set "
        "DATABASE_DIR/DATABASE_PATH/DB_PATH environment variable."
    )


def resolve_postgres_url(explicit_url: Optional[str] = None) -> str:
    """Resolve PostgreSQL URL from various sources."""
    from urllib.parse import quote_plus
    
    if explicit_url:
        return explicit_url
    
    url = os.getenv('DATABASE_URL')
    if url and 'postgresql' in url:
        return url
    
    host = os.getenv('POSTGRES_HOST')
    if not host:
        raise ValueError(
            "PostgreSQL not configured. Set POSTGRES_HOST or provide --postgres URL."
        )
    
    port = os.getenv('POSTGRES_PORT', '5432')
    user = quote_plus(os.getenv('POSTGRES_USER', 'telegram'))
    password = quote_plus(os.getenv('POSTGRES_PASSWORD', ''))
    db = os.getenv('POSTGRES_DB', 'telegram_backup')
    
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


async def run_migration(
    sqlite_path: str,
    postgres_url: str,
    batch_size: int,
    dry_run: bool
) -> bool:
    """Run the migration."""
    # Mask password in logs
    safe_url = postgres_url.split('@')[-1] if '@' in postgres_url else postgres_url
    
    logger.info("=" * 60)
    logger.info("Telegram Archive: SQLite to PostgreSQL Migration")
    logger.info("=" * 60)
    logger.info(f"Source: {sqlite_path}")
    logger.info(f"Target: postgresql://...@{safe_url}")
    logger.info(f"Batch size: {batch_size}")
    
    if dry_run:
        logger.info("\n[DRY RUN] Would migrate the following tables:")
        # Just show what would be migrated
        from src.db.base import DatabaseManager
        from src.db.models import User, Chat, Message, Media, Reaction, SyncStatus, Metadata
        from sqlalchemy import select, func
        
        sqlite_url = f"sqlite+aiosqlite:///{sqlite_path}"
        source = DatabaseManager(sqlite_url)
        await source.init()
        
        models = [User, Chat, Message, Media, Reaction, SyncStatus, Metadata]
        try:
            for model in models:
                async with source.get_session() as session:
                    result = await session.execute(
                        select(func.count()).select_from(model)
                    )
                    count = result.scalar() or 0
                    logger.info(f"  - {model.__tablename__}: {count:,} records")
        finally:
            await source.close()
        return True
    
    logger.info("\nStarting migration...")
    logger.info("(This may take a while for large databases)\n")
    
    try:
        counts = await migrate_sqlite_to_postgres(
            sqlite_path=sqlite_path,
            postgres_url=postgres_url,
            batch_size=batch_size
        )
        
        logger.info("\n" + "=" * 60)
        logger.info("Migration Summary")
        logger.info("=" * 60)
        total = 0
        for table, count in counts.items():
            logger.info(f"  {table}: {count:,} records")
            total += count
        logger.info(f"  TOTAL: {total:,} records")
        
        return True
        
    except Exception as e:
        logger.error(f"\nMigration failed: {e}")
        return False


async def run_verification(sqlite_path: str, postgres_url: str) -> bool:
    """Verify migration by comparing counts."""
    safe_url = postgres_url.split('@')[-1] if '@' in postgres_url else postgres_url
    
    logger.info("=" * 60)
    logger.info("Migration Verification")
    logger.info("=" * 60)
    logger.info(f"SQLite: {sqlite_path}")
    logger.info(f"PostgreSQL: postgresql://...@{safe_url}")
    logger.info("")
    
    try:
        results = await verify_migration(
            sqlite_path=sqlite_path,
            postgres_url=postgres_url
        )
        
        all_match = True
        logger.info(f"{'Table':<15} {'SQLite':>12} {'PostgreSQL':>12} {'Status':>10}")
        logger.info("-" * 52)
        
        for table, counts in results.items():
            status = "✓ OK" if counts['match'] else "✗ MISMATCH"
            if not counts['match']:
                all_match = False
            logger.info(
                f"{table:<15} {counts['sqlite']:>12,} {counts['postgres']:>12,} {status:>10}"
            )
        
        logger.info("-" * 52)
        if all_match:
            logger.info("All tables match! Migration verified successfully.")
        else:
            logger.error("Some tables have mismatched counts. Please investigate.")
        
        return all_match
        
    except Exception as e:
        logger.error(f"Verification failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Migrate Telegram Archive from SQLite to PostgreSQL',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--sqlite', '-s',
        help='Path to SQLite database file'
    )
    parser.add_argument(
        '--postgres', '-p',
        help='PostgreSQL connection URL'
    )
    parser.add_argument(
        '--batch-size', '-b',
        type=int,
        default=1000,
        help='Records per batch (default: 1000)'
    )
    parser.add_argument(
        '--verify-only', '-v',
        action='store_true',
        help='Only verify migration, do not migrate'
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Show what would be migrated without doing it'
    )
    
    args = parser.parse_args()
    
    try:
        sqlite_path = resolve_sqlite_path(args.sqlite)
        postgres_url = resolve_postgres_url(args.postgres)
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        sys.exit(1)
    
    if args.verify_only:
        success = asyncio.run(run_verification(sqlite_path, postgres_url))
    else:
        success = asyncio.run(run_migration(
            sqlite_path=sqlite_path,
            postgres_url=postgres_url,
            batch_size=args.batch_size,
            dry_run=args.dry_run
        ))
        
        if success and not args.dry_run:
            logger.info("\nRunning verification...")
            asyncio.run(run_verification(sqlite_path, postgres_url))
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()

