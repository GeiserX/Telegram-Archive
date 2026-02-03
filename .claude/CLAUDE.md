# Telegram-Archive Project

## Overview

Telegram Archive is an automated backup system for Telegram messages and media using Docker/Podman. It performs incremental backups on a configurable schedule with a web viewer for browsing archived content.

## Project Structure

- **Language**: Python 3.11+
- **Package Manager**: pip with pyproject.toml
- **Main Components**:
  - `src/telegram_backup.py` - Main backup logic using Telethon
  - `src/scheduler.py` - Cron-based backup scheduling
  - `src/setup_auth.py` - Interactive Telegram authentication
  - `src/web/` - FastAPI-based web viewer
  - `src/db/` - SQLAlchemy database layer (SQLite/PostgreSQL)
  - `src/__main__.py` - Unified CLI interface
- **Container Images**:
  - `drumsergio/telegram-archive` - Full backup system (~300MB)
  - `drumsergio/telegram-archive-viewer` - Viewer only (~150MB)
- **Entry Points**:
  - `telegram-archive` - Console script (installed via pip)
  - `./telegram-archive` - Direct script (no installation needed)
  - `python -m src` - Python module invocation

## Dependencies

Managed via `pyproject.toml` (single source of truth):

- telethon>=1.37.0 - Telegram client library
- APScheduler>=3.10.4 - Task scheduling
- fastapi>=0.115.0, uvicorn - Web viewer
- sqlalchemy[asyncio]>=2.0.36 - Database ORM
- aiosqlite>=0.20.0 - Async SQLite
- asyncpg>=0.30.0, psycopg2-binary - PostgreSQL support
- alembic>=1.14.0 - Database migrations

Install with: `pip install -e .` (editable mode for development)

## Configuration

Environment-based configuration via `.env` file:

**Required**:
- `TELEGRAM_API_ID` - From my.telegram.org
- `TELEGRAM_API_HASH` - From my.telegram.org
- `TELEGRAM_PHONE` - With country code (+1234567890)

**Key Options**:
- `BACKUP_PATH=/data/backups` - Backup storage
- `SESSION_NAME=telegram_backup` - Session file name
- `SESSION_DIR` - Session directory (default: `/data/session`)
- `DATABASE_PATH` - SQLite database path
- `SCHEDULE=0 */6 * * *` - Cron schedule
- `ENABLE_LISTENER=false` - Real-time edit/deletion tracking

## Docker Architecture

The Dockerfile (Dockerfile:1-45):
- Base: python:3.11-slim
- Creates non-root user `telegram` (UID 1000)
- Working directory: `/app`
- Entrypoint: `/app/scripts/entrypoint.sh` (runs Alembic migrations for PostgreSQL)
- Default command: `python -m src.scheduler`
- Volumes: `/data` for persistent storage

**Important**: The container runs as UID 1000 by default, which can cause permission issues with Podman if the host user has a different UID.

## Session Storage

Session files are stored separately from backups:
- Default session directory: `/data/session/`
- Session file: `/data/session/{SESSION_NAME}.session`
- Derived from `BACKUP_PATH` parent directory (config.py:112-116)

## Database

Supports both SQLite (default) and PostgreSQL:
- SQLite: `$BACKUP_PATH/telegram_backup.db` by default
- PostgreSQL: Configured via `DB_TYPE=postgresql` + connection vars
- Migrations: Alembic (alembic/ directory)
- Schema: chats, messages, media_files, reactions, push_subscriptions

## Known Issues

### Issue #55: Session Directory Permission Problem

**Problem**: When running authentication via Podman/Docker, SQLite fails with "unable to open database file" because:

1. Container runs as UID 1000 (`telegram` user)
2. Host volume mount may have different UID ownership
3. The session directory `/data/session` either doesn't exist or has wrong permissions
4. Telethon's SQLite session creation fails

**Root Cause**: UID mismatch between host and container. Config's `_ensure_directories()` (config.py:318-328) tries to create the session directory, but if running as UID 1000 with a volume owned by a different UID, the mkdir fails or creates it with wrong permissions.

**Workarounds**:

1. **Pre-create with correct permissions**:
   ```bash
   mkdir -p ses-$SESSION_NAME
   chmod 777 ses-$SESSION_NAME  # or chown 1000:1000
   ```

2. **Run container with host UID** (Podman):
   ```bash
   podman run --userns=keep-id -it --rm \
     -e TELEGRAM_API_ID=$TELEGRAM_API_ID \
     -e TELEGRAM_API_HASH=$TELEGRAM_API_HASH \
     -e TELEGRAM_PHONE=$TELEGRAM_PHONE \
     -e SESSION_NAME=$SESSION_NAME \
     -v $PWD/ses-$SESSION_NAME:/data/session:Z \
     drumsergio/telegram-archive:latest \
     python -m src auth
   ```

3. **Use docker-compose** (recommended):
   The init_auth.sh script uses docker-compose which handles volumes better:
   ```bash
   ./init_auth.sh
   ```

**Proper Fix** (needs to be implemented):
- Add explicit directory creation with proper error handling in setup_auth.py before TelegramClient initialization
- Or modify Dockerfile to support user namespace remapping
- Or add a --user flag to allow running as current user

## Testing

No test framework currently set up. Would recommend:
- pytest for unit tests
- Mock Telegram API for integration tests
- Test fixtures in tests/ directory (currently empty except test_backup_process.py)

## Development Notes

- Use requirements.txt for dependencies (no pyproject.toml yet)
- Could benefit from pyproject.toml + tox setup per CLAUDE.md preferences
- Alembic migrations are PostgreSQL-focused (entrypoint.sh:5-113)
- SQLite migrations happen automatically via SQLAlchemy

## CLI Interface

**Unified Entry Point**: `telegram-archive` script or `python -m src` (src/__main__.py)

All commands route through this single interface:

```bash
# Local development (using telegram-archive script)
./telegram-archive --data-dir ./data auth
./telegram-archive --data-dir ./data backup
./telegram-archive --data-dir ./data list-chats

# Or using Python module directly
python -m src --data-dir ./data auth
python -m src --data-dir ./data backup
python -m src --data-dir ./data list-chats

# Docker (uses python -m src)
docker compose exec telegram-backup python -m src stats
```

**Available Commands**:
- `auth` - Authenticate with Telegram
- `backup` - Run backup once
- `schedule` - Run scheduled backups (Docker default)
- `export` - Export to JSON
- `stats` - Show statistics
- `list-chats` - List chats

**Options**:
- `--data-dir PATH` - Override default `/data` location (useful for local dev)

**Docker Integration**:
- Default CMD: `python -m src` (shows help, requires explicit command)
- docker-compose.yml: Uses `python -m src schedule` for continuous backups
- Entrypoint: Runs migrations if database exists (skips for `auth` command)

## Authentication Flow

1. Run `init_auth.sh` or `python -m src auth`
2. Config loads env vars and creates session directory (config.py:318-321)
3. TelegramClient created with session_path (setup_auth.py:34-38)
4. Interactive code input
5. Session saved to mounted volume for reuse

## Volume Mounts

Standard docker-compose.yml setup:
```yaml
volumes:
  - ./data:/data
```

This creates:
- `/data/backups/` - Media and database
- `/data/session/` - Telethon session files (derived from backup path parent)

## Tips

- For Podman users with non-standard UIDs, use `--userns=keep-id`
- Add `:Z` suffix to volume mounts for SELinux systems
- Session directory must be writable by UID 1000 (or remapped user)
- init_auth.sh creates `data/backups` but not `data/session` - this should be fixed
