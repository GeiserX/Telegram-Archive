# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram Archive is a Python application that performs automated incremental backups of Telegram messages and media, with a FastAPI web viewer. It runs as two Docker containers: a backup scheduler (requires Telegram credentials) and a standalone web viewer.

**Python 3.14** | **SQLite (default) or PostgreSQL** | **Telethon + FastAPI + SQLAlchemy async**

## Common Commands

```bash
# Install in editable mode (local dev)
pip install -e ".[dev]"

# Lint
ruff check .
ruff format --check .

# Fix lint issues
ruff check --fix .
ruff format .

# Run all tests
python -m pytest tests/ -v --tb=short

# Run single test file
python -m pytest tests/test_config.py -v

# Run tests with coverage
python -m pytest tests/ --cov=src --cov-report=term-missing

# Run the CLI locally
python -m src --data-dir ./data list-chats
python -m src --data-dir ./data backup

# Build Docker images
docker build -t drumsergio/telegram-archive:latest .
docker build -t drumsergio/telegram-archive-viewer:latest -f Dockerfile.viewer .
```

## Architecture

### Two-Container Design
- **Backup container** (`Dockerfile`): Runs `src/scheduler.py` which uses APScheduler + optional real-time listener. Requires Telegram API credentials.
- **Viewer container** (`Dockerfile.viewer`): Runs `src/web/main.py` (FastAPI/Uvicorn). No Telegram client needed. Both containers share the same database.

### Source Layout (`src/`)
| Module | Role |
|---|---|
| `__main__.py` | CLI entry point with subcommands: `auth`, `backup`, `schedule`, `export`, `stats`, `list-chats` |
| `config.py` | All env vars loaded in `Config.__init__()`. Two chat-filtering modes: whitelist (`CHAT_IDS`) or type-based (`CHAT_TYPES` + include/exclude). |
| `telegram_backup.py` | Core backup logic. `TelegramBackup` class handles incremental message fetching, media download, deduplication via symlinks. |
| `scheduler.py` | `BackupScheduler` manages cron-triggered backups + optional listener. Uses a **shared `TelegramClient`** connection for both. |
| `listener.py` | Real-time event handler for edits, deletions, new messages. `MassOperationProtector` buffers operations to block burst attacks. |
| `connection.py` | Shared Telethon client management. |
| `realtime.py` | Notification abstraction: PostgreSQL uses LISTEN/NOTIFY, SQLite uses HTTP webhook to viewer's `/internal/push`. |
| `avatar_utils.py` | Profile photo download/management. |
| `export_backup.py` | JSON export and CLI stats display. |
| `db/` | Database layer (see below). |
| `web/main.py` | FastAPI app: REST API, WebSocket real-time updates, auth, push notifications. |
| `web/push.py` | Web Push notification manager (VAPID/Web Push API). |
| `web/templates/` | Single `index.html` (SPA-style viewer). |
| `web/static/` | Service worker, PWA manifest, icons. |

### Database Layer (`src/db/`)
- **`models.py`** — SQLAlchemy ORM models. Composite PK on messages `(id, chat_id)` since Telegram message IDs are only unique within a chat. Media is normalized to its own table (v6.0.0+).
- **`base.py`** — `DatabaseManager`: async engine setup, SQLite pragmas (WAL mode), PostgreSQL connection pooling. SQLite uses `create_all`; PostgreSQL uses Alembic.
- **`adapter.py`** — `DatabaseAdapter`: all DB operations (upsert, batch insert, pagination, search). Uses dialect-specific `on_conflict_do_update` for SQLite vs PostgreSQL.
- **`migrate.py`** — SQLite-to-PostgreSQL migration utility.
- **`__init__.py`** — Global adapter singleton via `get_adapter()`.

### Schema Migrations
Alembic manages PostgreSQL migrations in `alembic/versions/`. SQLite schema is created via `Base.metadata.create_all`. When adding columns/tables, you need both:
1. Update `src/db/models.py`
2. Create an Alembic migration for PostgreSQL

### Key Patterns
- **Dual-database support**: Every DB operation in `adapter.py` has SQLite and PostgreSQL branches for upsert syntax (`sqlite_insert` vs `pg_insert`).
- **`retry_on_locked` decorator**: Retries on "database locked" (SQLite) or connection errors (PostgreSQL) with exponential backoff.
- **Cursor-based pagination**: Web API uses `before_date`/`before_id` for O(1) pagination instead of offset-based.
- **Config is env-var-only**: No config files; everything comes from environment variables (loaded via `python-dotenv`).
- **Shared Telegram client**: Scheduler and listener share one `TelegramClient` to avoid session file lock conflicts.

## Ruff Configuration

Configured in `pyproject.toml`: line length 120, target Python 3.14. Notable ignores: `E711` (SQLAlchemy `== None` idiom), `B008` (FastAPI `Depends()` defaults). Check `[tool.ruff.lint.per-file-ignores]` for file-specific rules.

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`. Test files are in `tests/`. Tests mock Telethon and use in-memory SQLite databases.
