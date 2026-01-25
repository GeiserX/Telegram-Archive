#!/bin/bash
set -e

# Run Alembic migrations on startup (if using PostgreSQL)
if [ "$DB_TYPE" = "postgresql" ] || [ "$DB_TYPE" = "postgres" ]; then
    echo "Running database migrations..."
    python -c "
from alembic.config import Config
from alembic import command
import os

config = Config('/app/alembic.ini')

# Override sqlalchemy.url with environment variables
host = os.getenv('POSTGRES_HOST', 'localhost')
port = os.getenv('POSTGRES_PORT', '5432')
user = os.getenv('POSTGRES_USER', 'telegram')
password = os.getenv('POSTGRES_PASSWORD', '')
db = os.getenv('POSTGRES_DB', 'telegram_backup')
url = f'postgresql://{user}:{password}@{host}:{port}/{db}'
config.set_main_option('sqlalchemy.url', url)

command.upgrade(config, 'head')
print('Migrations complete.')
"
fi

# Execute the main command
exec "$@"
