# Telegram Archive Roadmap

This document tracks the version history and planned features for Telegram Archive.

## Version History

### v4.x - Dual-Image Architecture & Real-time Sync

#### v4.1.0 (January 2026) - Real-time Listener
- **NEW:** Real-time listener mode (`ENABLE_LISTENER=true`)
  - `MessageEdited` handler: Updates edited message text immediately
  - `MessageDeleted` handler: Removes deleted messages from DB
  - `NewMessage` handler: Auto-tracks new chats for monitoring
- **NEW:** `TelegramListener` class with Telethon event handlers
- Integration with scheduler for background operation alongside scheduled backups

#### v4.0.7 (January 2026)
- **FIX:** Timezone handling in `edit_date` sync (PostgreSQL compatibility)
- Added data consistency tests (`tests/test_db_adapter.py`)

#### v4.0.6 (January 2026)
- **FIX:** Chat ID consistency - use marked IDs throughout
- Migration scripts for existing databases (`migrate_to_marked_ids.sql`)
- Fixed foreign key violations during backup

#### v4.0.5 (January 2026)
- **FIX:** Use marked IDs for channels/supergroups in include lists
- Fetch explicitly included chats not in dialog list

#### v4.0.4 (January 2026)
- **NEW:** `VERIFY_MEDIA` option to re-download missing/corrupted media files

#### v4.0.3 (January 2026)
- **FIX:** Handle `CHAT_TYPES=` (empty) for whitelist-only mode

#### v4.0.2 (January 2026)
- PostgreSQL timezone compatibility fixes

#### v4.0.1 (January 2026)
- Documentation updates for v4.0 upgrade

#### v4.0.0 (January 2026) - BREAKING CHANGE
- **BREAKING:** Split into two Docker images
  - `drumsergio/telegram-archive` - Backup scheduler (with Telegram client)
  - `drumsergio/telegram-archive-viewer` - Web viewer only (lighter, no Telegram)
- Smaller viewer image (~150MB vs ~300MB)
- Faster CI/CD - viewer changes don't rebuild backup image

---

### v3.x - Async Database & PostgreSQL Support

#### v3.0.5 (December 2025)
- Minor bug fixes and stability improvements

#### v3.0.0 (December 2025) - BREAKING CHANGE
- **NEW:** Async database operations with SQLAlchemy
- **NEW:** PostgreSQL support (in addition to SQLite)
- **NEW:** Alembic database migrations
- **NEW:** SQLite to PostgreSQL migration utility
- Improved concurrent access handling
- Full backward compatibility with v2.x SQLite databases

---

### v2.x - Web Viewer Enhancements

#### v2.3.0 (November 2025)
- Enhanced web viewer UI polish

#### v2.2.x (October-November 2025)
- Multiple incremental improvements to viewer
- Bug fixes for media handling
- Improved chat display

#### v2.1.0 (October 2025)
- Web viewer stability improvements

#### v2.0.0 (October 2025) - BREAKING CHANGE
- **FIX:** Group chat participant colors (consistent per-user)
- Database schema update for color consistency

---

### v1.x - Authentication & Sync Features

#### v1.2.x (September-October 2025)
- **NEW:** Sync deletions and edits (`SYNC_DELETIONS_EDITS`)
- **NEW:** Enhanced participant colors
- **FIX:** Database locking during concurrent backup and web access
- **NEW:** Chat ID display in sidebar
- **NEW:** Chat exclusion per-type (private, groups, channels)
- Enhanced login page with modern design
- Cookie persistence fixes

#### v1.1.0 (September 2025)
- **NEW:** Viewer authentication (`VIEWER_USERNAME`, `VIEWER_PASSWORD`)
- **NEW:** Per-user message colors in viewer
- **NEW:** Profile photo storage and display
- **NEW:** Avatar display in chat list

#### v1.0.0 (September 2025) - BREAKING CHANGE
- **BREAKING:** File ID deduplication - reduced storage
- **NEW:** Reply text extraction and display
- Database schema changes for deduplication

---

### v0.x - Initial Development

#### v0.3.0 (August 2025)
- Major web viewer UI improvements
- Telegram-like dark theme
- Mobile-responsive design

#### v0.2.0 (August 2025)
- **NEW:** Web viewer for browsing backups
- Basic chat list and message display
- Media preview support

#### v0.1.0 (August 2025) - Initial Release
- Automated Telegram backup with Docker
- Incremental message backup
- Media file download
- Scheduled execution via cron
- SQLite database storage

---

## Planned Features

### v5.0.0 - Real-time Sync & Notifications (Planned)

**Goal:** Enable real-time message sync and viewer notifications, with built-in safety to protect backup integrity.

#### 🎯 Headline Features
- [ ] **Real-time listener** - Catch message edits instantly as they happen
- [ ] **Viewer notifications** - Browser push notifications when new messages arrive
- [ ] **WebSocket updates** - Live message updates without page refresh
- [ ] **Unread indicators** - Show unread message counts in chat list

#### 🛡️ Zero-Footprint Protection (Backup First!)
Real-time features are powerful but could compromise your backup. v5 includes **military-grade protection**:

- [x] **Buffered operations** - Nothing written immediately, all ops wait in buffer
- [x] **Burst detection** - If >10 ops arrive, triggers protection BEFORE any writes
- [x] **Zero footprint** - On trigger, ENTIRE buffer discarded, no changes to DB
- [x] **LISTEN_DELETIONS=true by default** - With zero-footprint protection
- [x] **Configurable thresholds** - Tune protection to your needs

**How it works:**
1. Operations queue in a buffer (2-second delay)
2. If >10 ops for a chat → BURST DETECTED
3. Entire buffer discarded → ZERO changes to your backup
4. Chat blocked for 30 seconds → Attack neutralized

#### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LISTENER` | `false` | Enable real-time sync (opt-in) |
| `LISTEN_EDITS` | `true` | Apply text edits (safe) |
| `LISTEN_DELETIONS` | `true` | ⚠️ Delete from backup (protected!) |
| `MASS_OPERATION_THRESHOLD` | `10` | 🛡️ Aggressive default - triggers fast |
| `MASS_OPERATION_WINDOW_SECONDS` | `30` | Detection window |
| `ENABLE_NOTIFICATIONS` | `false` | Browser push notifications |

---

### Future Roadmap

#### Multi-tenancy & Access Control
- [ ] Multi-tenant architecture - Single instance, multiple users
- [ ] Shared channel access - Allow multiple users to view specific channels
- [ ] Role-based permissions - Admin, viewer, per-chat access

#### Authentication
- [ ] OAuth/Social login - Google, GitHub, Discord
- [ ] Magic link authentication - Passwordless email login
- [ ] OIDC/SAML support - Enterprise SSO

#### Viewer Enhancements
- [ ] Full-text search - Search message content across all chats
- [ ] Reactions display - Show message reactions
- [ ] Chat statistics - Analytics dashboard

#### Backup Features
- [ ] Multi-account support - Backup multiple Telegram accounts
- [ ] S3/Cloud storage - Store backups in AWS S3, MinIO
- [ ] Encryption at rest - Encrypt database and media

#### Integrations
- [ ] REST API - External integrations
- [ ] Scheduled reports - Email/webhook notifications
- [ ] Export formats - HTML, PDF archives

---

## Contributing

Have a feature request? [Open an issue](https://github.com/GeiserX/Telegram-Archive/issues)!

See [AGENTS.md](../AGENTS.md) for development guidelines.
