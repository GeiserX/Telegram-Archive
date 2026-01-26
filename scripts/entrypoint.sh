#!/bin/bash
set -e

# Run Alembic migrations on startup (if using PostgreSQL)
if [ "$DB_TYPE" = "postgresql" ] || [ "$DB_TYPE" = "postgres" ]; then
    echo "Running database migrations..."
    python -c "
from alembic.config import Config
from alembic import command
import os
import psycopg2

# Build connection URL
host = os.getenv('POSTGRES_HOST', 'localhost')
port = os.getenv('POSTGRES_PORT', '5432')
user = os.getenv('POSTGRES_USER', 'telegram')
password = os.getenv('POSTGRES_PASSWORD', '')
db = os.getenv('POSTGRES_DB', 'telegram_backup')
url = f'postgresql://{user}:{password}@{host}:{port}/{db}'

# Check if this is a pre-Alembic database (tables exist but no alembic_version)
conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=db)
cur = conn.cursor()

# Check if alembic_version table exists
cur.execute(\"\"\"
    SELECT EXISTS (
        SELECT FROM information_schema.tables 
        WHERE table_name = 'alembic_version'
    );
\"\"\")
has_alembic = cur.fetchone()[0]

# Check if chats table exists (pre-existing database)
cur.execute(\"\"\"
    SELECT EXISTS (
        SELECT FROM information_schema.tables 
        WHERE table_name = 'chats'
    );
\"\"\")
has_tables = cur.fetchone()[0]

if has_tables and not has_alembic:
    print('Detected pre-Alembic database. Stamping with current version...')
    # Create alembic_version table and stamp with latest version
    cur.execute(\"\"\"
        CREATE TABLE IF NOT EXISTS alembic_version (
            version_num VARCHAR(32) NOT NULL,
            CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
        );
    \"\"\")
    # Check if is_pinned column exists (added in migration 004)
    cur.execute(\"\"\"
        SELECT EXISTS (
            SELECT FROM information_schema.columns 
            WHERE table_name = 'messages' AND column_name = 'is_pinned'
        );
    \"\"\")
    has_is_pinned = cur.fetchone()[0]
    
    # Check if push_subscriptions table exists (added in migration 003)
    cur.execute(\"\"\"
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'push_subscriptions'
        );
    \"\"\")
    has_push_subs = cur.fetchone()[0]
    
    # Determine which version to stamp based on existing schema
    if has_is_pinned:
        stamp_version = '004'
    elif has_push_subs:
        stamp_version = '003'
    else:
        # Assume at least 002 (chat_date_index) - indexes are harder to check
        stamp_version = '002'
    
    cur.execute(f\"INSERT INTO alembic_version (version_num) VALUES ('{stamp_version}')\")
    conn.commit()
    print(f'Database stamped at version {stamp_version}')

cur.close()
conn.close()

# Now run normal Alembic upgrade
config = Config('/app/alembic.ini')
config.set_main_option('sqlalchemy.url', url)
command.upgrade(config, 'head')
print('Migrations complete.')
"
fi

# Execute the main command
exec "$@"
