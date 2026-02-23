# Codebase Summary

**Version:** 7.1 | **Last Updated:** 2026-02-24

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
- Models: Message, Chat, User, Reaction, Forward, Transaction, ViewerAccount, ViewerAuditLog
- Async adapter pattern for query abstraction
- Multi-user viewer support with PBKDF2-SHA256 auth

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

## Database Models (v7.1)

### Core Tables

**Message** - Main chat content
- id, chat_id, sender_id, text, media_type
- date, edit_date, reactions, forwards_count
- is_topic_message, topic_id, is_outgoing

**Transaction** - Accounting view
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

### Multi-user Tables (NEW v7.1)

**ViewerAccount** - Per-viewer authentication
- id, username (unique), password_hash (PBKDF2-SHA256)
- assigned_chat_ids (JSON), is_active, created_at, updated_at

**ViewerAuditLog** - Access tracking
- id, viewer_id, action (login/logout/view/export)
- chat_id, timestamp, ip_address, user_agent

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
- `GET /api/chats` - List chats (filtered by viewer permissions or DISPLAY_CHAT_IDS for master)
- `GET /api/chats/{chat_id}/stats` - Per-chat statistics
- `GET /api/stats` - Global backup statistics
- `GET /api/chats/{chat_id}/pinned` - Pinned messages
- `GET /api/archived/count` - Archived chat count

### Admin Multi-user Endpoints (NEW v7.1)
- `GET /api/admin/viewers` - List all viewer accounts (admin only)
- `POST /api/admin/viewers` - Create new viewer account (admin only)
- `PUT /api/admin/viewers/{viewer_id}` - Update viewer permissions (admin only)
- `DELETE /api/admin/viewers/{viewer_id}` - Delete viewer account (admin only)
- `GET /api/admin/chats` - List all chats with counts (admin only)
- `GET /api/admin/audit` - View access audit log (admin only)

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

## v7.1 Technical Changes (Multi-user Auth)

### New Files
- `alembic/versions/20260224_007_add_viewer_accounts.py` - ViewerAccount + ViewerAuditLog tables

### Modified Files
- `src/db/models.py` - Added ViewerAccount, ViewerAuditLog models
- `src/db/adapter.py` - 7 new viewer CRUD + audit methods
- `src/web/main.py` - 6 new admin endpoints + dual-mode auth (DB viewer + env-var master)
- `src/web/templates/index.html` - Admin UI: viewer management, chat picker, audit log viewer

### Authentication Changes
- **PBKDF2-SHA256 hashing** (600k iterations) for stored viewer passwords
- **In-memory session cache** with 24h TTL for fast auth checks
- **Dual-mode login:** DB viewer accounts + env-var master account (VIEWER_USERNAME/VIEWER_PASSWORD)
- **Per-user chat filtering** - Replaces global DISPLAY_CHAT_IDS for viewer roles
- **Master backward compatible** - DISPLAY_CHAT_IDS respected when master user logs in

### Data Migration
Alembic migration 007 handles:
- Create viewer_accounts table with unique username, password_hash
- Create viewer_audit_log table with action/chat/timestamp tracking
- Add indexes on (viewer_id, timestamp) and (username)

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

- **Multi-user authentication** with PBKDF2-SHA256 (v7.1)
- **Per-user chat permissions** - Each viewer assigned specific chats
- **Audit logging** - All viewer actions tracked (login, logout, access, export)
- **Dual-mode auth** - Master admin account via env vars, secondary viewers in DB
- **Session tokens** with 24h TTL, in-memory cache for fast validation
- **Chat filtering** - DISPLAY_CHAT_IDS for master, per-viewer assignments for others
- CORS configuration per deployment
- Rate limiting on mass operations
- Secure cookie handling (PBKDF2 replaces old SHA256)

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
- VIEWER_USERNAME + VIEWER_PASSWORD define master/admin account (v7.1)
