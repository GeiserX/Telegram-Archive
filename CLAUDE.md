# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram Archive is a Python-based automated backup system for Telegram chats with a web viewer. It performs incremental backups of messages and media on a configurable schedule using Docker, storing data in SQLite or PostgreSQL through SQLAlchemy adapters with a FastAPI-based web interface for browsing.

## Core Architecture

### Multi-Service Docker Setup

The application runs as three Docker services:
1. **telegram-backup**: Scheduled backup worker (runs `src.scheduler`)
2. **telegram-viewer**: FastAPI web server on port 8000 (runs `src.web.main`)
3. **postgres**: PostgreSQL 16 server (optional, when using PostgreSQL)

Both backup and viewer services share the same data volume (`./data:/data`) for session files, database, and media.

### Database Architecture with SQLAlchemy Adapters

The project uses SQLAlchemy with an adapter pattern to support multiple databases:

**src/db_adapters/adapter.py** - Abstract base interface
- `DatabaseAdapter` class defines the unified interface
- Methods for chats, messages, users, media operations
- Abstract methods for database-specific implementations

**src/db_adapters/sqlite_adapter.py** - SQLite implementation
- Uses SQLite with SQLAlchemy ORM
- WAL mode enabled for concurrent access
- JSON serialization for raw_data fields
- Connection pooling and retry mechanisms

**src/db_adapters/postgres_adapter.py** - PostgreSQL implementation
- PostgreSQL 16 with connection pooling
- Enhanced JSON support with native PostgreSQL JSON types
- Automatic schema creation and migrations
- Optimized for production workloads

**src/db_adapters/factory.py** - Adapter factory
- Creates appropriate adapter based on `DB_TYPE`
- Supported types: `sqlite` (default), `sqlite-alchemy` (alias), `postgres-alchemy`
- Unified interface across all database types

### Main Components

**src/telegram_backup.py** - Core backup logic
- `TelegramBackup` class: Manages Telegram client connection and backup operations
- `backup_all()`: Main entry point - fetches dialogs, filters chats, processes messages in batches
- Incremental backups using `last_synced_message_id` from database
- Profile photo tracking with historical copies
- Optional sync for deletions/edits (`SYNC_DELETIONS_EDITS`)
- Batch processing for efficiency (default 100 messages per batch)
- Uses SQLAlchemy adapters for all database operations

**src/scheduler.py** - Cron-based job scheduler
- Uses APScheduler with AsyncIO
- Runs initial backup on startup, then follows cron schedule
- Graceful shutdown on SIGINT/SIGTERM

**src/web/main.py** - FastAPI web viewer
- Optional cookie-based authentication (`VIEWER_USERNAME`/`VIEWER_PASSWORD`)
- REST API: `/api/chats`, `/api/chats/{id}/messages`, `/api/stats`, `/api/chats/{id}/export`
- Serves media files from `/media` mount
- Supports restricted viewer mode (`DISPLAY_CHAT_IDS`)

**src/config.py** - Environment-based configuration
- Loads from .env file via python-dotenv
- Database type selection (`DB_TYPE`) and PostgreSQL configuration
- Granular chat filtering (global/private/groups/channels include/exclude)
- Configurable database timeout (default 60s for locked database resilience)
- Timezone configuration for viewer display

### Data Storage Structure

```
data/
├── session/
│   └── telegram_backup.session  # Telethon session (Telegram auth)
└── backups/
    ├── telegram_backup.db        # SQLite database (WAL mode)
    ├── telegram_backup.db-wal    # Write-ahead log
    ├── telegram_backup.db-shm    # Shared memory
    └── media/
        ├── avatars/
        │   ├── users/{user_id}_{photo_id}.jpg
        │   └── chats/{chat_id}_{photo_id}.jpg
        └── {chat_id}/
            └── {telegram_file_id}_{original_name}
```

### Key Technical Details

**Database Architecture**
- **SQLite**: WAL mode enabled for concurrent read/write access
- **PostgreSQL**: Native connection pooling and ACID compliance
- **Unified Interface**: SQLAlchemy adapters provide consistent API across databases
- **Automatic Schema Creation**: Database schema initialized on first run
- **Database Concurrency**:
  - SQLite: `PRAGMA busy_timeout` increased to 60s (60000ms)
  - PostgreSQL: Connection pooling with configurable pool size
  - Retry mechanisms for handling transient database errors

**Media Deduplication**
- Filenames use Telegram's internal `file_id` for automatic deduplication
- Format: `{telegram_file_id}_{original_name}` or `{telegram_file_id}.{ext}`
- MIME type detection for proper extensions
- Profile photos versioned by `photo_id` (historical copies preserved)

**Message Processing**
- Batch inserts (default `BATCH_SIZE`=100) for efficiency
- SQLAlchemy ORM batch operations for optimal performance
- Reactions stored separately with user attribution when available
- Poll data serialized to JSON in `raw_data` field
- `is_outgoing` flag backfilled using owner_id from metadata
- Reply text truncated to 100 chars (Telegram-style)
- Database-agnostic JSON handling (SQLite: JSON strings, PostgreSQL: native JSON)

**Chat Filtering Priority**
1. Global Exclude → Skip
2. Type-Specific Exclude → Skip
3. Global Include → Backup
4. Type-Specific Include → Backup
5. CHAT_TYPES filter → Backup if matches

Explicitly excluded chats are deleted from database/filesystem during backup.

## Database Operations

### Database Adapter Selection

The system automatically selects the appropriate database adapter based on `DB_TYPE`:

```python
from src.db_adapters.factory import create_database_adapter

# SQLite (default)
DB_TYPE=sqlite  # or sqlite-alchemy

# PostgreSQL
DB_TYPE=postgres-alchemy
```

### Adapter Usage Examples

```python
from src.db_adapters.factory import create_database_adapter

# Create adapter (automatically selected based on config)
db = create_database_adapter(config)

# Database operations (same interface for all databases)
db.initialize_schema()

# Chat operations
chats = db.get_all_chats()
chat = db.get_chat(12345)

# Message operations
messages = db.get_messages(chat_id=12345, limit=50)
db.insert_messages([message_dict1, message_dict2])

# User operations
users = db.get_all_users()

# Statistics
stats = db.get_stats()
```

### Database Migration

**SQLite to PostgreSQL:**
1. Export data: `python -m src.export_backup export -o backup.json`
2. Change `DB_TYPE=postgres-alchemy` in `.env`
3. Set PostgreSQL credentials
4. Restart services: `docker compose restart`

**Schema Migrations:**
- Automatic schema creation on first run
- SQLAlchemy handles schema evolution
- Both databases use identical schema through models

## Common Development Commands

### Docker Commands (Preferred)

```bash
# Start services
docker compose up -d

# View logs
docker compose logs -f telegram-backup
docker compose logs -f telegram-viewer

# Manual backup run
docker compose exec telegram-backup python -m src.telegram_backup

# View statistics
docker compose exec telegram-backup python -m src.export_backup stats

# List chats
docker compose exec telegram-backup python -m src.export_backup list-chats

# Export to JSON
docker compose exec telegram-backup python -m src.export_backup export -o backup.json

# Test database adapters
docker compose exec telegram-backup python -c "from src.db_adapters.factory import create_database_adapter; from src.config import Config; print(create_database_adapter(Config()).__class__.__name__)"

# Run tests
docker compose exec telegram-backup python -m pytest tests/

# Shell access
docker compose exec telegram-backup bash
```

### Local Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup authentication (one-time)
python3 -m src.setup_auth

# Run backup manually
python3 -m src.telegram_backup

# Start scheduler
python3 -m src.scheduler

# Start web viewer
uvicorn src.web.main:app --host 0.0.0.0 --port 8000

# Run tests
pytest tests/
```

## Testing Strategy

Tests are located in `tests/` directory:
- `test_config.py` - Configuration validation
- `test_database_viewer.py` - Database operations
- `test_telegram_backup.py` - Backup logic
- `test_web_messages_api.py` - Web API endpoints
- `test_auth.py` - Authentication flows

When making changes, run the full test suite to ensure nothing breaks.

## Critical Workflows

### Initial Setup Flow
1. User runs `./init_auth.sh` (or `init_auth.bat`)
2. Script runs `python -m src.setup_auth` in container
3. Telethon creates session file at `data/session/telegram_backup.session`
4. Session file must exist before scheduler starts

### Backup Execution Flow
1. Scheduler triggers `backup_all()`
2. Connect to Telegram, fetch dialogs
3. Filter dialogs by type and ID filters
4. Delete explicitly excluded chats from database
5. For each chat:
   - Upsert chat metadata
   - Download/update profile photo if changed
   - Fetch new messages (min_id > last_synced_message_id)
   - Process messages in batches (default 100)
   - Download media if configured
   - Insert reactions separately
   - Update sync_status
   - Optionally sync deletions/edits
6. Store `last_backup_time` in metadata table (UTC timestamp)

### Web Viewer Flow
1. Frontend checks `/api/auth/status`
2. If auth required and not authenticated, show login form
3. Login sets `viewer_auth` cookie (SHA256 hash)
4. All API calls protected by `require_auth` dependency
5. Frontend fetches `/api/chats` with avatar URLs
6. User selects chat, frontend fetches `/api/chats/{id}/messages`
7. Messages loaded with pagination (limit/offset)
8. Reactions and reply previews hydrated from database

## Environment Variables

### Required (Backup)
- `TELEGRAM_API_ID` - From my.telegram.org
- `TELEGRAM_API_HASH` - From my.telegram.org
- `TELEGRAM_PHONE` - Phone with country code

### Optional (Database)
- `DB_TYPE` - Database type: `sqlite` (default) or `postgres-alchemy`
- `DATABASE_TIMEOUT` - Database connection timeout in seconds (default: 60.0)
- `DATABASE_DIR` - Override database directory
- `DATABASE_PATH` - Override full database path

**PostgreSQL Settings (when DB_TYPE=postgres-alchemy):**
- `POSTGRES_HOST` - PostgreSQL server host (default: postgres)
- `POSTGRES_PORT` - PostgreSQL server port (default: 5432)
- `POSTGRES_DB` - Database name (default: telegram_backup)
- `POSTGRES_USER` - Database user (default: telegram)
- `POSTGRES_PASSWORD` - Database password (required)
- `POSTGRES_POOL_SIZE` - Connection pool size (default: 5)

### Optional (Common)
- `SCHEDULE` - Cron format (default: `0 */6 * * *`)
- `BACKUP_PATH` - Storage path (default: `/data/backups`)
- `DOWNLOAD_MEDIA` - Download media files (default: true)
- `MAX_MEDIA_SIZE_MB` - Max media size (default: 100)
- `BATCH_SIZE` - Messages per batch (default: 100)
- `CHAT_TYPES` - Comma-separated: private,groups,channels
- `LOG_LEVEL` - INFO, DEBUG, WARNING, ERROR

### Optional (Filtering)
- `GLOBAL_INCLUDE_CHAT_IDS` - Whitelist (comma-separated IDs)
- `GLOBAL_EXCLUDE_CHAT_IDS` - Blacklist (comma-separated IDs)
- `PRIVATE_INCLUDE_CHAT_IDS` - Private chat whitelist
- `PRIVATE_EXCLUDE_CHAT_IDS` - Private chat blacklist
- `GROUPS_INCLUDE_CHAT_IDS` - Group whitelist
- `GROUPS_EXCLUDE_CHAT_IDS` - Group blacklist
- `CHANNELS_INCLUDE_CHAT_IDS` - Channel whitelist
- `CHANNELS_EXCLUDE_CHAT_IDS` - Channel blacklist

### Optional (Viewer)
- `VIEWER_USERNAME` - Enable auth with username
- `VIEWER_PASSWORD` - Enable auth with password
- `VIEWER_TIMEZONE` - Display timezone (default: Europe/Madrid)
- `DISPLAY_CHAT_IDS` - Restrict viewer to specific chats

### Optional (Advanced)
- `SYNC_DELETIONS_EDITS` - Sync deletions/edits from Telegram (default: false, expensive!)
- `DATABASE_DIR` - Override database directory
- `DATABASE_PATH` - Override full database path
- `SESSION_DIR` - Override session directory

## File Naming Conventions

- Python modules use snake_case
- Classes use PascalCase
- Constants use UPPER_SNAKE_CASE
- Database fields use snake_case
- Environment variables use UPPER_SNAKE_CASE

## Code Patterns to Follow

**Error Handling**
- Database operations use `@retry_on_locked` decorator
- Log errors with `exc_info=True` for stack traces
- Graceful degradation (e.g., skip media on download failure)

**Async Operations**
- Telethon operations are async (use `await`)
- Database operations are synchronous (blocking)
- Scheduler runs async jobs with `AsyncIOScheduler`

**Configuration**
- All config from environment variables via `Config` class
- No hardcoded paths or credentials
- Validate required fields in `validate_credentials()`

**Database Access**
- Always use parameterized queries (no string interpolation)
- Commit after write operations
- Use `INSERT OR REPLACE` for upserts
- Enable WAL mode for concurrent access

## Known Limitations

- Secret chats not supported (Telegram API limitation)
- Edit history not tracked (only latest version stored)
- Deleted messages before first backup cannot be recovered
- Profile photos: only latest downloaded (unless photo changes)
- Custom emoji reactions stored as `custom_{document_id}`

## Dependencies

Key Python packages (from requirements.txt):
- `telethon>=1.34.0` - Telegram client library
- `APScheduler>=3.10.4` - Job scheduling
- `python-dotenv>=1.0.0` - Environment variable loading
- `cryptg>=0.4.0` - Cryptography for Telethon
- `fastapi>=0.109.0` - Web framework
- `uvicorn>=0.27.0` - ASGI server
- `jinja2>=3.1.3` - Template engine
- `httpx>=0.27.0` - HTTP client

## Maintenance Notes

**Database Maintenance**
- WAL checkpoint runs automatically
- Consider `VACUUM` for database compaction after large deletions
- Monitor `-wal` and `-shm` file sizes

**Media Cleanup**
- Old profile photos accumulate by design (historical preservation)
- Explicitly excluded chats have media deleted during backup
- No automatic cleanup of orphaned media files

**Logging**
- Telethon logging set to WARNING to reduce noise
- Application logging configurable via `LOG_LEVEL`
- Docker logs accessible via `docker compose logs`

## Development Environment

Write code comments and documentation in English only.

**Python Version**
- Use `python3` command (not `python`)
- Create project-specific virtual environment with `python3 -m venv venv`
- Always activate venv before development: `source venv/bin/activate`

**Docker Preference**
- Prefer `docker compose` commands over standalone `docker` commands
- All services defined in `docker-compose.yml`
