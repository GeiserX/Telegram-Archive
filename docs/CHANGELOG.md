# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [Unreleased]

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
