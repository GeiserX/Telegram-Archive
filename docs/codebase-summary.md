# Codebase Summary

**Version:** 7.0 | **Last Updated:** 2026-02-17

## Project Overview

Telegram Archive is an automated backup system for Telegram messages and media with a modern web viewer. It supports incremental backups, real-time message syncing, and full-text search with advanced filtering.

## Architecture

### Core Components

**Backup Engine** (`src/telegram_backup.py`)
- Incremental message/media backup via Telethon API
- Scheduled execution (cron-based)
- Batch processing with checkpoint recovery
- Media deduplication via symlinks

**Real-time Listener** (`src/listener.py`)
- Tracks edits and deletions between scheduled backups
- Message observer pattern
- Rate-limited deletion protection

**Database Layer** (`src/db/`)
- Dual-mode: SQLite (default) or PostgreSQL
- Models: Message, Chat, User, Reaction, Forward, Transaction
- Async adapter pattern for query abstraction

**Web Viewer** (`src/web/`)
- FastAPI backend with 30+ endpoints
- Vue 3 frontend with Tailwind CSS
- WebSocket real-time updates
- Authentication & push notifications

### Key Modules

| Module | Purpose |
|--------|---------|
| `src/config.py` | Environment variable parsing |
| `src/connection.py` | Telegram client management |
| `src/scheduler.py` | Cron scheduling & job management |
| `src/avatar_utils.py` | Profile photo handling |
| `src/db/migrate.py` | Alembic migrations |
| `src/transaction_detector.py` | Monetary pattern detection |
| `src/export_backup.py` | JSON export functionality |

## Database Models (v7.0)

### Core Tables

**Message** - Main chat content
- id, chat_id, sender_id, text, media_type
- date, edit_date, reactions, forwards_count
- is_topic_message, topic_id, is_outgoing

**Transaction** (NEW) - Accounting view
- id, message_id, chat_id, sender_id
- credit, debit, category, confidence
- auto_detected, notes, created_at, updated_at

**Chat** - Channel/group metadata
- id, title, is_channel, is_group, type
- is_private, photo_url, last_update

**User** - Sender profiles
- id, first_name, username, is_bot, phone

**Reaction** - Message reactions
- id, message_id, emoji, count, recent_senders

## API Endpoints (v7.0)

### Search & Discovery
- `GET /api/search` - Global full-text search with filters (sender, media_type, date range)
- `GET /api/chats/{chat_id}/messages` - Chat messages with search highlighting
- `GET /api/chats/{chat_id}/media` - Media gallery with type filters

### Transaction View (NEW)
- `GET /api/chats/{chat_id}/transactions` - Paginated transaction list
- `POST /api/chats/{chat_id}/transactions/scan` - Auto-detect from messages
- `PUT /api/transactions/{txn_id}` - Manual override
- `DELETE /api/transactions/{txn_id}` - Remove transaction
- `GET /api/chats/{chat_id}/transactions/summary` - Running balance
- `GET /api/chats/{chat_id}/transactions/export` - CSV export

### Message Navigation
- `GET /api/chats/{chat_id}/messages/{message_id}/context` - Deep linking
- `GET /api/chats/{chat_id}/messages/by-date` - Date-based lookup

### Chat Management
- `GET /api/chats` - List chats (filtered by DISPLAY_CHAT_IDS)
- `GET /api/chats/{chat_id}/stats` - Per-chat statistics
- `GET /api/stats` - Global backup statistics
- `GET /api/chats/{chat_id}/pinned` - Pinned messages
- `GET /api/archived/count` - Archived chat count

### Real-time
- `GET /api/push/config` - Web Push configuration
- `POST /api/push/subscribe` - Subscribe to notifications
- `WebSocket /ws/updates` - Real-time message sync

## Frontend Features (v7.0)

### Search Enhancement
- Advanced filters: sender, media type, date range
- Global cross-chat search
- Search result highlighting in context

### Message Display
- Copy message link functionality
- Deep link URL hash routing
- Rich message formatting

### Performance & UX
- Skeleton loading states
- Keyboard shortcuts: Esc, Ctrl+K, ?
- URL hash-based routing
- Responsive mobile layout

### Media Gallery
- Grid view with drag-scrolling
- Type filters: photo, video, document, audio
- Lightbox with fullscreen mode

### Transaction View (NEW)
- Auto-detect monetary patterns
- Spreadsheet-like table interface
- Inline editing of transactions
- CSV export with running balance
- Category classification

## v7.0 Technical Changes

### New Files
- `src/transaction_detector.py` - Pattern matching for amounts and keywords
- Database migration 007 - Transaction table schema

### Modified Files
- `src/db/models.py` - Added Transaction model
- `src/db/adapter.py` - Added transaction methods
- `src/web/main.py` - Added 15+ new API endpoints
- `src/web/templates/index.html` - Vue 3 transaction UI components

### Data Migration
Alembic migration handles:
- Create transactions table
- Add indexes on (chat_id, date)
- Seed from existing messages (optional)

## Code Quality Standards

**Python Style**
- Type hints throughout (mypy compatible)
- Async/await for I/O operations
- Error logging with context
- Database transaction safety

**Frontend**
- Vue 3 Composition API
- Tailwind CSS utilities
- Mobile-first responsive design
- Progressive enhancement

**Testing**
- pytest for unit tests
- Async test fixtures
- Database seeding for integration tests

## Security

- Optional viewer authentication (username/password)
- Session-based auth with configurable timeout
- Chat filtering via DISPLAY_CHAT_IDS
- CORS configuration per deployment
- Rate limiting on mass operations
- Secure cookie handling

## Deployment

**Docker Images**
- `drumsergio/telegram-archive:v7.0` - Full backup + viewer
- `drumsergio/telegram-archive-viewer:v7.0` - Viewer only

**Database**
- SQLite: Single-file, zero-config (default)
- PostgreSQL: Recommended for large deployments

**Configuration**
- 30+ environment variables
- .env file or docker-compose environment
- Dynamic timezone support
