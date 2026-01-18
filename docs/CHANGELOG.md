# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [Unreleased]

## [5.0.1] - 2026-01-18

### Fixed
- Auth cookie missing `secure` flag causing iOS Safari to ignore authentication

## [5.0.0] - 2026-01-18

### ⚠️ Major Release - Real-time Sync & Media Path Changes

This release introduces **real-time message sync**, **zero-footprint mass operation protection**, and **consistent media path naming**. Migration scripts are provided for existing installations.

### Added

#### Real-time Listener Mode
- **`ENABLE_LISTENER`** - Background listener for instant sync (no waiting for scheduled backup)
- **`LISTEN_EDITS`** - Apply text edits to backed up messages in real-time
- **`LISTEN_DELETIONS`** - Mirror deletions from Telegram (with protection, see below)
- **`LISTEN_NEW_MESSAGES`** - Save new messages immediately (default: true)
- **`LISTEN_NEW_MESSAGES_MEDIA`** - Download media in real-time (default: false)
- **`LISTEN_CHAT_ACTIONS`** - Track chat photo/title changes, member joins/leaves
- **`LISTEN_ALBUMS`** - Detect and group album uploads together

#### Zero-Footprint Mass Operation Protection
- **Sliding-window rate limiter** protects against mass edit/deletion attacks
- **`MASS_OPERATION_THRESHOLD`** - Operations before protection triggers (default: 10)
- **`MASS_OPERATION_WINDOW_SECONDS`** - Time window for counting operations (default: 30)
- When triggered, **ALL pending operations are discarded** - zero changes to your backup

#### Priority Chats
- **`PRIORITY_CHAT_IDS`** - Process these chats FIRST in all backup/sync operations
- Useful for ensuring important chats are always backed up before others

#### Viewer Enhancements
- **WebSocket real-time updates** - New messages appear instantly without refresh
- **Infinite scroll** - Cursor/keyset pagination for large chats
- **Album grid display** - Photo/video albums shown as grids like Telegram
- **Compact stats dropdown** - Stats moved to dropdown next to header
- **Per-chat stats** - Message count, media count, total size per chat
- **"Real-time sync" indicator** - Shows when listener is active
- **`SHOW_STATS`** - Hide stats dropdown for restricted viewers (default: true)

#### Web Push Notifications
- **`PUSH_NOTIFICATIONS`** - Notification mode: `off`, `basic`, `full` (default: basic)
  - `off` - No notifications at all
  - `basic` - In-browser notifications (tab must be open)
  - `full` - **Persistent Web Push** (works even when browser is closed!)
- **Auto-generated VAPID keys** - Stored in database, persist across restarts
- **Subscription management** - Subscriptions survive container restarts and updates
- **Automatic cleanup** - Expired subscriptions removed automatically
- **Optional custom VAPID keys** via `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT`

#### Migration Scripts
- **`scripts/migrate_media_paths.py`** - ⚠️ **REQUIRED** - Normalizes media folder names to use marked IDs
- **`scripts/update_media_sizes.py`** - ⚠️ **REQUIRED** - Populates file_size for accurate stats
- **`scripts/detect_albums.py`** - ⚠️ **HIGHLY RECOMMENDED** - Detect albums in existing backups for album grid display
- **`scripts/deduplicate_media.py`** - ⚠️ **HIGHLY RECOMMENDED** - Global deduplication using symlinks (saves disk space)
- **`scripts/restore_chat.py`** - Repost archived messages to Telegram

### Changed
- **Shared Telethon client** - Backup and listener share connection (avoids session DB locks)
- **WAL mode for session DB** - Better concurrency for Telethon session
- **Media folder naming** - Groups/channels now use marked IDs (e.g., `-35258041/` not `35258041/`)
- **Bulk SQL operations** - Migration scripts use single queries per batch (10-100x faster)

### Fixed
- Media 404s due to inconsistent folder naming (positive vs negative IDs)
- Audio files served with wrong Content-Type (now audio/ogg, audio/mp3, etc.)
- Stats calculation error with Decimal types (JSON serialization)
- Session DB locking when running backup and listener simultaneously

### ⚠️ Migration Required

**If upgrading from v4.x with existing data:**

1. **Run migration scripts** (inside Docker container):
   ```bash
   # 1. Normalize media paths (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.migrate_media_paths
   
   # 2. Update file sizes for accurate stats (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.update_media_sizes
   
   # 3. Detect albums for grid display (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.detect_albums
   
   # 4. Deduplicate media files (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.deduplicate_media
   ```

2. **Update docker-compose.yml** with new env variables (see README)

See [Upgrading to v5.0.0](#upgrading-to-v500-from-v4x) below for detailed instructions.

### Related Issues
- Fixes #12 - Timezone-aware datetime sorting
- Fixes #20 - Real-time sync for edits/deletions
- Fixes #21 - Mass operation protection
- Fixes #22 - Media path consistency

## [4.1.5] - 2026-01-15

### Improved
- **Quick Start guide** - Expanded with step-by-step instructions for beginners
- **Database configuration** - Added prominent warning about viewer needing same DB path
- **Troubleshooting table** - Common permission and setup issues
- **docker-compose.yml** - Clearer comments about matching DB settings

### Added
- `scripts/release.sh` - Validates changelog entry before allowing tag creation

## [4.1.4] - 2026-01-15

### Changed
- Moved all upgrade notices from README to `docs/CHANGELOG.md`
- README now references CHANGELOG for upgrade instructions

### Improved
- Release workflow now extracts changelog notes for GitHub releases
- Added release guidelines to AGENTS.md
- Documented chat ID format requirements

## [4.1.3] - 2026-01-15

### Added
- Prominent startup banner showing SYNC_DELETIONS_EDITS status
- Makes it clear why backup re-checks all messages from the start

## [4.1.2] - 2026-01-15

### Fixed
- **PostgreSQL reactions sequence out of sync** - Auto-detect and recover from sequence drift
- Prevents `UniqueViolationError` on reactions table after database restores

### Added
- `scripts/fix_reactions_sequence.sql` - Manual fix script for affected users
- Troubleshooting section in README for this issue

## [4.1.1] - 2026-01-15

### Added
- **Auto-correct DISPLAY_CHAT_IDS** - Viewer automatically corrects positive IDs to marked format (-100...)
- Helps users who forget the -100 prefix for channels/supergroups

## [4.1.0] - 2026-01-14

### Added
- **Real-time listener** for message edits and deletions (`ENABLE_LISTENER=true`)
- Catches changes between scheduled backups
- `SYNC_DELETIONS_EDITS` option for batch sync of all messages

### Fixed
- Timezone handling for `edit_date` field (PostgreSQL compatibility)
- Tests updated for pytest compatibility

## [4.0.7] - 2026-01-14

### Fixed
- Strip timezone from `edit_date` before database insert/update
- Prevents `asyncpg.DataError` with PostgreSQL TIMESTAMP columns

## [4.0.6] - 2026-01-14

### Fixed
- **CRITICAL: Chat ID format mismatch** - Use marked IDs consistently
- Chats now stored with proper format (-100... for channels/supergroups)

### ⚠️ Breaking Change
**Database migration required if upgrading from v4.0.5!**

See [Upgrading to v4.0.6](#upgrading-to-v406-from-v405) below.

## [4.0.5] - 2026-01-13

### Added
- CI workflow for dev builds on PRs
- Tests for timezone and ID format handling

### Known Issues
- Chat ID format bug (fixed in v4.0.6)

## [4.0.4] - 2026-01-12

### Fixed
- `CHAT_TYPES=` (empty string) now works for whitelist-only mode
- Previously caused ValueError due to incorrect env parsing

## [4.0.3] - 2026-01-11

### Fixed
- Environment variable parsing for empty CHAT_TYPES

## [4.0.0] - 2026-01-10

### ⚠️ Breaking Change
**Docker image names changed!**

| Old (v3.x) | New (v4.0+) |
|------------|-------------|
| `drumsergio/telegram-backup-automation` | `drumsergio/telegram-archive` |
| Same image with command override | `drumsergio/telegram-archive-viewer` |

See [Upgrading from v3.x to v4.0](#upgrading-from-v3x-to-v40) below.

### Changed
- Split into two Docker images (backup + viewer)
- Viewer image is smaller (~150MB vs ~300MB)

## [3.0.0] - 2025-12-XX

### Added
- PostgreSQL support
- Async database operations with SQLAlchemy
- Alembic migrations

### Changed
- Database layer rewritten for async

## [2.x] - 2025-XX-XX

### Features
- SQLite database
- Web viewer
- Media download support

---

# Upgrading

## Upgrading to v5.0.0 (from v4.x)

> ⚠️ **Migration Scripts Recommended**

v5.0.0 changes media folder naming to use marked IDs consistently. While the backup will work without migration, **running the migration scripts is highly recommended** for:
- Correct media display in viewer (no 404s)
- Accurate file size statistics
- Album grid display for existing photos/videos

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker-compose stop telegram-backup
   ```

2. **Pull the new image:**
   ```bash
   docker-compose pull
   ```

3. **Run migration scripts** (one at a time, wait for each to finish):

   ```bash
   # Replace with your actual values
   NETWORK=telegram-backup_default
   DB_HOST=your-postgres-container
   DB_PASS=your-password
   BACKUP_PATH=/path/to/backups
   
   # 1. Media path migration (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.migrate_media_paths
   
   # 2. Update file sizes (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.update_media_sizes
   
   # 3. Detect albums (optional but recommended)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.detect_albums
   ```

4. **Update docker-compose.yml** with new env variables:
   ```yaml
   environment:
     # ... existing vars ...
     # Real-time listener (recommended)
     ENABLE_LISTENER: true
     LISTEN_EDITS: true
     LISTEN_DELETIONS: true  # ⚠️ Will delete from backup!
     LISTEN_NEW_MESSAGES: true
     # Mass operation protection
     MASS_OPERATION_THRESHOLD: 10
     MASS_OPERATION_WINDOW_SECONDS: 30
     # Optional: Priority chats (processed first)
     # PRIORITY_CHAT_IDS: -1002240913478,-1001234567890
   ```

5. **Start the new version:**
   ```bash
   docker-compose up -d
   ```

**If starting fresh:** No migration needed, just use the new image.

---

## Upgrading to v4.0.6 (from v4.0.5)

> 🚨 **Database Migration Required**

v4.0.5 had a bug where chats were stored with positive IDs while messages used negative (marked) IDs, causing foreign key violations.

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker-compose stop telegram-backup
   ```

2. **Run the migration script:**

   **PostgreSQL:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/migrate_to_marked_ids.sql
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < migrate_to_marked_ids.sql
   ```

   **SQLite:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/migrate_to_marked_ids_sqlite.sql
   sqlite3 /path/to/telegram_backup.db < migrate_to_marked_ids_sqlite.sql
   ```

3. **Pull and restart:**
   ```bash
   docker-compose pull
   docker-compose up -d
   ```

**If upgrading from v4.0.4 or earlier:** No migration needed.
**If starting fresh:** No migration needed.

---

## Upgrading from v3.x to v4.0

> ⚠️ **Docker image names changed**

### Update your docker-compose.yml:

```yaml
# Before (v3.x)
telegram-backup:
  image: drumsergio/telegram-backup-automation:latest

telegram-viewer:
  image: drumsergio/telegram-backup-automation:latest
  command: uvicorn src.web.main:app --host 0.0.0.0 --port 8000

# After (v4.0+)
telegram-backup:
  image: drumsergio/telegram-archive:latest

telegram-viewer:
  image: drumsergio/telegram-archive-viewer:latest
  # No command needed
```

Then:
```bash
docker-compose pull
docker-compose up -d
```

**Your data is safe** - no database migration needed.

---

## Upgrading from v2.x to v3.0

Transparent upgrade - just pull and restart:
```bash
docker-compose pull
docker-compose up -d
```

Your existing SQLite data works automatically. v3 detects v2 environment variables for backward compatibility.

**Optional:** Migrate to PostgreSQL - see README for instructions.

---

## Chat ID Format (Important!)

Since v4.0.6, all chat IDs use Telegram's "marked" format:

| Entity Type | Format | Example |
|-------------|--------|---------|
| Users | Positive | `123456789` |
| Basic groups | Negative | `-123456789` |
| Supergroups/Channels | -100 prefix | `-1001234567890` |

**Finding Chat IDs:** Forward a message to @userinfobot on Telegram.

When configuring `GLOBAL_EXCLUDE_CHAT_IDS`, `DISPLAY_CHAT_IDS`, etc., use the marked format.
