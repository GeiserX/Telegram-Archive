#!/bin/bash
set -e

# Determine if we should run migrations
# Skip migrations for 'auth' command (no database needed yet)
# For other commands, check if database exists and run migrations if needed

SKIP_MIGRATIONS=false
if [[ "$1" == "python" ]] && [[ "$2" == "-m" ]] && [[ "$3" == "src" ]] && [[ "$4" == "auth" ]]; then
    echo "Running auth command - skipping database migrations"
    SKIP_MIGRATIONS=true
fi

# Run Alembic migrations if database exists
if [ "$SKIP_MIGRATIONS" = "false" ]; then
  if [ "$DB_TYPE" = "postgresql" ] || [ "$DB_TYPE" = "postgres" ]; then
    echo "Running database migrations..."
    python -c "
from alembic.config import Config
from alembic import command
import os
import sys
import time
import psycopg2

# Build connection URL
host = os.getenv('POSTGRES_HOST', 'localhost')
port = os.getenv('POSTGRES_PORT', '5432')
user = os.getenv('POSTGRES_USER', 'telegram')
password = os.getenv('POSTGRES_PASSWORD', '')
db = os.getenv('POSTGRES_DB', 'telegram_backup')
url = f'postgresql://{user}:{password}@{host}:{port}/{db}'

print(f'Connecting to PostgreSQL at {host}:{port}...')

# Retry logic - wait for PostgreSQL to be ready
max_retries = 30
retry_delay = 2
conn = None

for attempt in range(max_retries):
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=db)
        print('PostgreSQL connection established.')
        break
    except psycopg2.OperationalError as e:
        if attempt < max_retries - 1:
            print(f'PostgreSQL not ready (attempt {attempt + 1}/{max_retries}), waiting {retry_delay}s...')
            time.sleep(retry_delay)
        else:
            print(f'ERROR: Could not connect to PostgreSQL at {host}:{port} after {max_retries} attempts')
            print(f'Error: {e}')
            sys.exit(1)

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
    # Check if viewer_accounts table exists (added in migration 007)
    cur.execute(\"\"\"
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'viewer_accounts'
        );
    \"\"\")
    has_007_table = cur.fetchone()[0]

    # Check if forum_topics table exists (added in migration 006)
    cur.execute(\"\"\"
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'forum_topics'
        );
    \"\"\")
    has_006_table = cur.fetchone()[0]

    # Check if idx_messages_reply_to index exists (added in migration 005)
    cur.execute(\"\"\"
        SELECT EXISTS (
            SELECT FROM pg_indexes
            WHERE indexname = 'idx_messages_reply_to'
        );
    \"\"\")
    has_005_index = cur.fetchone()[0]

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
    if has_007_table:
        stamp_version = '007'
    elif has_006_table:
        stamp_version = '006'
    elif has_005_index:
        stamp_version = '005'
    elif has_is_pinned:
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
  elif [ "$DB_TYPE" = "sqlite" ] || [ -z "$DB_TYPE" ]; then
    # SQLite - check if database file exists before running migrations
    DB_PATH="${DB_PATH:-${DATABASE_PATH:-${BACKUP_PATH:-/data/backups}/telegram_backup.db}}"

    if [ -f "$DB_PATH" ]; then
      echo "SQLite database found at $DB_PATH - running migrations..."
      python -c "
from alembic.config import Config
from alembic import command
import os
import sqlite3

db_path = os.getenv('DB_PATH', os.getenv('DATABASE_PATH', os.path.join(os.getenv('BACKUP_PATH', '/data/backups'), 'telegram_backup.db')))
url = f'sqlite:///{db_path}'

# Check if this is a pre-Alembic database that needs stamping
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Check if alembic_version table exists
cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'\")
has_alembic = cur.fetchone() is not None

# Check if chats table exists (pre-existing database)
cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='chats'\")
has_tables = cur.fetchone() is not None

if has_tables and not has_alembic:
    print('Detected pre-Alembic SQLite database. Stamping with current version...')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS alembic_version (
            version_num VARCHAR(32) NOT NULL,
            CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
        )
    ''')

    # Check if viewer_accounts table exists (added in migration 007)
    cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='viewer_accounts'\")
    has_007_table = cur.fetchone() is not None

    # Check if forum_topics table exists (added in migration 006)
    cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='forum_topics'\")
    has_006_table = cur.fetchone() is not None

    # Check for idx_messages_reply_to index (added in migration 005)
    cur.execute(\"SELECT name FROM sqlite_master WHERE type='index' AND name='idx_messages_reply_to'\")
    has_005_index = cur.fetchone() is not None

    # Check if is_pinned column exists (added in migration 004)
    cur.execute(\"PRAGMA table_info(messages)\")
    msg_columns = {row[1] for row in cur.fetchall()}
    has_is_pinned = 'is_pinned' in msg_columns

    # Check if push_subscriptions table exists (added in migration 003)
    cur.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='push_subscriptions'\")
    has_push_subs = cur.fetchone() is not None

    # Determine which version to stamp based on existing schema
    if has_007_table:
        stamp_version = '007'
    elif has_006_table:
        stamp_version = '006'
    elif has_005_index:
        stamp_version = '005'
    elif has_is_pinned:
        stamp_version = '004'
    elif has_push_subs:
        stamp_version = '003'
    else:
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
print('SQLite migrations complete.')
"
    else
      echo "No database found yet - skipping migrations (will be created automatically)"
    fi
  fi
fi

# Execute the main command
exec "$@"
