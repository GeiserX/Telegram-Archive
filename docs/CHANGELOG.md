# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [Unreleased]

## [6.2.9] - 2026-02-07

### Fixed

- **Viewer blank blue page** — Vue.js 3 in-browser template compiler requires `'unsafe-eval'` in the CSP `script-src` directive (it uses `new Function()` internally). Without it, Vue loads but silently fails to compile templates, leaving a blank page. Added `'unsafe-eval'` to fix rendering. Bug present since v6.2.3.

## [6.2.8] - 2026-02-07

### Fixed

- **Viewer CSS/JS broken since v6.2.3** — Content-Security-Policy header blocked all CDN resources (Tailwind CSS, Vue.js, Google Fonts, FontAwesome, Flatpickr), causing the viewer to render without styling or interactivity. Added required CDN domains to `script-src`, `style-src`, and `font-src` directives.

## [6.2.7] - 2026-02-07

### Changed

- **Python 3.14 base image** — Bumped Docker base from `python:3.11-slim` to `python:3.14-slim` in both `Dockerfile` and `Dockerfile.viewer`. All dependencies have pre-built cp314 wheels.
- **Python 3.14 type annotations** — Removed string quotes from forward references (PEP 649 deferred evaluation), replaced `Optional[X]` with `X | None`, simplified `AsyncGenerator` type args (PEP 585).
- **PEP 758 except formatting** — Unparenthesized except clauses now used where applicable.
- **CI updated to Python 3.14** — Tests and lint workflows now run on Python 3.14.
- **Dependabot dev image builds skipped** — `docker-publish-dev` workflow no longer fails on Dependabot PRs (they lack Docker Hub secrets).

## [6.2.6] - 2026-02-07

### Fixed

- **SQLite viewer crash** — Viewer container failed to start when using SQLite because `PRAGMA journal_mode=WAL` requires write access to create `.db-wal` and `.db-shm` sidecar files. WAL and `create_all` are now wrapped in try/except so the viewer degrades gracefully to default journal mode instead of crashing. (#61)
- **Read-only volume mount** — Removed `:ro` from the viewer volume in `docker-compose.yml` since SQLite WAL needs write access. Added comment explaining when `:ro` is safe (PostgreSQL only).

## [6.2.5] - 2026-02-07

### Fixed

- **CodeQL security alerts resolved** — Replaced weak SHA256 auth token with PBKDF2-SHA256 (600k iterations), fixed stack trace exposure in `/internal/push`, and eliminated clear-text password logging by constructing log-safe strings from non-sensitive env vars.
- **CORS credentials with wildcard origins** — Disabled `allow_credentials` when `CORS_ORIGINS=*` (browser security requirement).
- **Auth cookie `Secure` flag** — Cookie now sets `Secure=true` by default, configurable via `SECURE_COOKIES` env var.
- **`/internal/push` access control** — Endpoint restricted to private IPs only (loopback + RFC 1918).
- **Dependabot config** — Removed invalid duplicate Docker ecosystem entry.

### Changed

- **Roadmap updated** — Reflects current v6.x implementation, reordered milestones, added new feature ideas.

## [6.2.4] - 2026-02-07

### Changed

- **Unified environment variables reference** — Consolidated 8+ scattered subsections into one comprehensive table with Scope column (B=backup, V=viewer, B/V=both) and bold category separators.
- **Documented missing env vars** — Added `CORS_ORIGINS`, `SECURE_COOKIES`, and `MASS_OPERATION_BUFFER_DELAY` to the reference table.
- **`ENABLE_LISTENER` master switch** — Prominently documented that `ENABLE_LISTENER=false` disables all `LISTEN_*` and `MASS_OPERATION_*` variables.
- **docker-compose.yml** — Added all missing env vars to both backup and viewer services (listener sub-settings, mass operation, CORS, secure cookies, notifications).
- **.env.example** — Complete rewrite with all variables organized into clear sections.

## [6.2.3] - 2026-02-07

### Added

- **Dependabot configuration** — Automated dependency updates for pip (weekly), GitHub Actions (monthly), and Docker base images (weekly). Groups minor/patch updates, ignores major bumps.
- **Ruff linter and formatter** — Configured in `pyproject.toml` with CI workflow. Replaces flake8/black/isort with a single fast tool. Entire codebase auto-formatted.
- **Pre-commit hooks** — `.pre-commit-config.yaml` with Ruff + standard hooks (check-yaml, trailing-whitespace, etc.).
- **CodeQL security scanning** — Weekly SAST analysis plus on every PR.
- **SECURITY.md** — Responsible disclosure policy with supported versions and scope.
- **CONTRIBUTING.md** — Developer setup guide, branch naming, commit conventions, and testing instructions.
- **PR template** — Checklists for type of change, database changes, data consistency, testing, and security.
- **CODEOWNERS** — Routes all PR reviews to @GeiserX.
- **`.editorconfig`** — Consistent formatting across editors (UTF-8, LF, Python 4-space, YAML 2-space).
- **Content-Security-Policy headers** — CSP, X-Frame-Options, X-Content-Type-Options, and Referrer-Policy on all responses.
- **`CORS_ORIGINS` environment variable** — Configure allowed CORS origins (default: `*` without credentials).
- **`SECURE_COOKIES` environment variable** — Control `secure` flag on auth cookie (default: `true`; set `false` for local HTTP development).

### Fixed

- **CORS misconfiguration** — Removed `allow_credentials=True` when using wildcard origins (browser security requirement). Restricted allowed methods to GET/POST.
- **`/internal/push` access control** — Endpoint now enforces private IP allowlist (loopback + RFC 1918 ranges) instead of silently allowing all requests.
- **Auth cookie missing `secure` flag** — Cookie now sets `secure=True` by default, preventing transmission over plain HTTP.

### Changed

- **Docker Compose security hardening** — Both services now use `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and `tmpfs: [/tmp]`. Viewer volume mounted read-only.
- **GitHub Actions bumped** — `docker/build-push-action` v5→v6, `codecov/codecov-action` v4→v5.
- **Removed `.cursor/rules/project.mdc`** — Redundant with `AGENTS.md` which is the single source of truth for AI assistant configuration.

## [6.1.1] - 2026-02-06

### Fixed

- **Critical: `schedule` command would silently do nothing** - The `run_schedule` function in the CLI called the async `scheduler.main()` without `asyncio.run()`, causing the scheduler to never actually start. This affected all Docker deployments using `python -m src schedule`.

### Changed

- **Removed `:latest` tag from CLI help text** - Docker examples in `--help` output now use `<version>` placeholder instead of `:latest`, following the project convention of always using specific version tags.

## [6.1.0] - 2026-02-06

### Community Contributions

This release includes a major contribution from **[@yarikoptic](https://github.com/yarikoptic)** (Yaroslav Halchenko) - thank you for this substantial improvement to the project!

### Added

- **Unified CLI interface** (`python -m src <command>`) - All operations now route through a single entry point with intuitive subcommands: `auth`, `backup`, `schedule`, `export`, `stats`, `list-chats`. Includes comprehensive `--help` with workflow guidance. (contributed by @yarikoptic, PR #57)

- **Python packaging with `pyproject.toml`** - Proper PEP 621 package definition with centralized dependencies. Install locally with `pip install -e .` to get the `telegram-archive` command. (contributed by @yarikoptic, PR #57)

- **`--data-dir` option for local development** - Override the default `/data` directory to avoid permission issues when developing outside Docker:
  ```bash
  telegram-archive --data-dir ./data list-chats
  python -m src --data-dir ./data backup
  ```

- **`telegram-archive` executable script** - Direct execution without installation (`./telegram-archive --help`). (contributed by @yarikoptic, PR #57)

- **Smart database migrations in entrypoint** - Migrations now skip for `auth` command (no DB needed yet) and check database existence before running SQLite migrations. (contributed by @yarikoptic, PR #57)

### Changed

- **Dockerfile default CMD now shows help** - Running the container without an explicit command displays help instead of silently starting the scheduler. The `docker-compose.yml` explicitly runs `schedule`. This is a behavioral change for users running `docker run` without a command - add `python -m src schedule` to your command.

- **Unified command syntax** - Old module-based commands (`python -m src.telegram_backup`, `python -m src.export_backup stats`) are replaced by `python -m src backup`, `python -m src stats`, etc.

## [6.0.3] - 2026-02-02

### Community Contributions

This release includes contributions from **[@yarikoptic](https://github.com/yarikoptic)** - welcome to the project! 🎉

### Improved

- **Better error messages for permission issues** (#54, #55) - Authentication setup now provides clear troubleshooting guidance when encountering permission errors (common with Podman or Docker UID mismatches):
  ```
  PERMISSION ERROR - Unable to write to session directory
  
  For Podman users:
    Add --userns=keep-id to your run command
  
  For Docker users:
    mkdir -p data && sudo chown -R 1000:1000 data
  ```

### Changed

- **Standardized on `docker compose` (v2) syntax** - All documentation and scripts now use the modern `docker compose` command instead of the deprecated `docker-compose` (v1). Docker Compose v2 has been built into Docker CLI since mid-2021, and v1 was deprecated in July 2023. (contributed by @yarikoptic)

- **`init_auth.sh` is now executable by default** - No need to manually run `chmod +x init_auth.sh` before using the script. (contributed by @yarikoptic)

### Added

- **Shellcheck CI workflow** - Added GitHub Actions workflow to lint shell scripts on push/PR, improving code quality for bash scripts. (contributed by @yarikoptic)

## [6.0.2] - 2026-02-02

### Fixed
- **Reduced Telethon disconnect warnings** (#50) - Added graceful disconnect handling to reduce "Task was destroyed but it is pending" asyncio warnings during shutdown or reconnection. These warnings are caused by a [known Telethon issue](https://github.com/LonamiWebs/Telethon/issues/782) and don't affect functionality.

### Technical
- Added small delay after `client.disconnect()` to allow internal task cleanup
- Wrapped disconnect in try/except to handle cleanup errors gracefully

## [6.0.1] - 2026-01-30

### Fixed
- **Graceful handling of inaccessible chats** (fixes #49) - When you lose access to a channel/group (kicked, banned, left, or it went private), the backup now logs a clean warning instead of a full error traceback:
  ```
  WARNING - → Skipped (no access): ChannelPrivateError
  ```
  Previously this would show a confusing multi-line error that looked like a bug.

### Technical
- Added specific error handling for `ChannelPrivateError`, `ChatForbiddenError`, and `UserBannedInChannelError`
- These Telegram API responses are now treated as expected conditions, not application errors

## [6.0.0] - 2026-01-28

### ⚠️ Breaking Changes

This is a major release with breaking schema changes. **Backup your database before upgrading.**

#### Normalized Media Storage

Media metadata is now stored exclusively in the `media` table instead of being duplicated in the `messages` table.

**Removed columns from `messages` table:**
- `media_type`
- `media_id`
- `media_path`

**API response format changed:**

Before (v5.x):
```json
{
  "id": 123,
  "media_type": "photo",
  "media_path": "/data/backups/media/123/file.jpg",
  "media_file_name": "photo.jpg",
  "media_mime_type": "image/jpeg"
}
```

After (v6.0.0):
```json
{
  "id": 123,
  "media": {
    "type": "photo",
    "file_path": "/data/backups/media/123/file.jpg",
    "file_name": "photo.jpg",
    "file_size": 12345,
    "mime_type": "image/jpeg",
    "width": 1920,
    "height": 1080
  }
}
```

#### Service Messages and Polls

- Service messages: Now detected by `raw_data.service_type === 'service'` instead of `media_type === 'service'`
- Polls: Now detected by presence of `raw_data.poll` instead of `media_type === 'poll'`

### Added

#### Simple Whitelist Mode with `CHAT_IDS` (fixes #48)

New `CHAT_IDS` environment variable provides a simple way to backup only specific chats:

```bash
# Backup ONLY these 2 channels - nothing else
CHAT_IDS=-1001234567890,-1009876543210
```

**Two filtering modes:**

| Mode | When | How it works |
|------|------|--------------|
| **Whitelist** | `CHAT_IDS` is set | Backup ONLY the listed chats. All other settings ignored. |
| **Type-based** | `CHAT_IDS` not set | Use `CHAT_TYPES` + `INCLUDE`/`EXCLUDE` filters (existing behavior). |

This solves the common confusion where users expected `CHANNELS_INCLUDE_CHAT_IDS` to act as a whitelist, but it was actually additive.

#### Removed `LISTEN_ALBUMS` Setting (fixes #46)

The `LISTEN_ALBUMS` setting was redundant and has been removed. Albums are now automatically handled via `grouped_id` in the NewMessage handler. The viewer groups messages by `grouped_id` to display albums correctly.

#### Foreign Key Constraints
- `media(message_id, chat_id)` → `messages(id, chat_id)` (ON DELETE CASCADE)
- `reactions.user_id` → `users.id` (nullable, ON DELETE SET NULL)

**Note:** `messages.sender_id` does NOT have a FK constraint because sender_id can contain channel/group IDs that aren't in the users table. The relationship is maintained at ORM level only.

#### New Indexes
- `idx_messages_reply_to` - Fast reply message lookups
- `idx_media_downloaded` - Find undownloaded media by chat
- `idx_media_type` - Filter media by type
- `idx_reactions_user` - User reaction queries
- `idx_chats_username` - Chat username lookups
- `idx_users_username` - User username lookups

### Changed

- **Media file_path column type**: Changed from `String(500)` to `Text` to support longer paths
- **Media relationship**: Messages now have a `media_items` relationship for direct access

### Migration Guide

The Alembic migration handles data migration automatically:

1. **Backup your database** before upgrading
2. The migration will:
   - Copy any missing media data from `messages` to `media` table
   - Create a backup table `_messages_media_backup` for rollback
   - Drop the `media_type`, `media_id`, `media_path` columns
   - Add foreign key constraints
   - Create new indexes

**Run the migration:**
```bash
# If using Docker
docker exec telegram-backup alembic upgrade head

# If running locally
alembic upgrade head
```

**Rollback if needed:**
```bash
alembic downgrade 004
```

### Technical Notes

- SQLite: Uses table recreation for schema changes (SQLite doesn't support DROP COLUMN in older versions)
- PostgreSQL: Uses direct ALTER TABLE operations
- Migration is reversible - downgrade restores columns from backup table

## [5.4.1] - 2026-01-25

### Fixed
- **Scroll-to-bottom button not appearing** - Fixed detection logic for `flex-col-reverse` containers where `scrollTop` is negative when scrolled up

## [5.4.0] - 2026-01-25

### Added

#### Multiple Pinned Messages Support
- **Pinned message banner** - Shows currently pinned message at the top of the chat, matching Telegram's UI
- **Pin navigation** - Click the message content to scroll to that pinned message and cycle through others
- **Pin count indicator** - Shows "(1 of N)" when multiple messages are pinned
- **Pinned Messages view** - Click the list icon to view all pinned messages in a dedicated view
- **Real-time pin sync** - Listener now catches pin/unpin events when `ENABLE_LISTENER=true`
- **Automatic pin sync** - Pinned messages are synced on every backup (no manual migration needed)
- **API endpoint** - `GET /api/chats/{chat_id}/pinned` returns all pinned messages

#### Database
- **`is_pinned` column** - New column on messages table to track pinned status
- **Alembic migration** - Migration `004` adds the column and index automatically

### Fixed
- **Auto-load older messages** - Replaced manual "Load older messages" button with automatic Intersection Observer loading
- **Telegram-style loading spinner** - Shows spinning indicator while fetching older messages
- **Alembic migrations auto-run** - Docker image now includes Alembic and runs migrations automatically on startup for PostgreSQL

### Upgrade Notes

**Database Migration Required:**

The migration runs automatically on startup. If you're using PostgreSQL, ensure the backup container has write access.

After upgrading, pinned messages will be populated on the next backup run. If you want to populate them immediately without waiting for the next backup:

```bash
# Trigger a manual backup to sync pinned messages
docker exec telegram-backup python -m src backup
```

If using the real-time listener (`ENABLE_LISTENER=true`), pin/unpin events will be captured automatically going forward.

## [5.3.7] - 2026-01-22

### Fixed
- **Avatar filename mismatch** (#35, #41) - Avatars are now saved as `{chat_id}_{photo_id}.jpg` to match what the viewer expects. Previously saved as `{chat_id}.jpg` which caused avatars to not display.

### Added
- **`scripts/cleanup_legacy_avatars.py`** - Utility script to remove old `{chat_id}.jpg` avatar files after they've been replaced by the new format. Run with `--dry-run` to preview changes.

### Changed
- **Shared avatar utility** - Avatar path generation moved to `src/avatar_utils.py` for consistency between backup and listener
- **Skip redundant downloads** - Avatars are only downloaded when the file doesn't exist or is empty

### Upgrade Notes
Legacy avatar files (`{chat_id}.jpg`) are still supported via fallback. To clean up old files after new-format avatars are downloaded:
```bash
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py --dry-run  # Preview
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py            # Apply
```

## [5.3.3] - 2026-01-20

### Fixed
- **Listener media deduplication** - Real-time listener now uses the same deduplication logic as scheduled backups, creating symlinks to `_shared` directory instead of downloading duplicates

## [5.3.2] - 2026-01-20

### Added
- **Forwarded message info** - Shows the original sender's name for forwarded messages (resolved from Telegram when possible)
- **Channel post author** - Shows the post author (signature) for channel messages when enabled in the channel

### Fixed
- **Avatar refresh not working** (#35) - Simplified avatar logic to always update on each backup. Removed `AVATAR_REFRESH_HOURS` config (was unreliable)

### Removed
- `AVATAR_REFRESH_HOURS` environment variable - Avatars now update on every backup run automatically

## [5.3.1] - 2026-01-20

### Fixed
- **Album duplicates showing** - Fixed `grouped_id` comparison (string vs integer) causing albums to show duplicate placeholder messages. Added `getGroupedId()` helper that converts to string for consistent comparison.

### Added
- **Service messages** - Chat actions (photo changed, title changed, user joined/left) now display as centered service messages in the viewer, like the real Telegram client
- **`scripts/normalize_grouped_ids.py`** - Migration script to normalize old `grouped_id` values to strings. Run with `--dry-run` to preview changes.

### Upgrade Notes
If you have existing albums showing as duplicates, run the migration script:
```bash
docker exec telegram-backup python scripts/normalize_grouped_ids.py --dry-run  # Preview
docker exec telegram-backup python scripts/normalize_grouped_ids.py            # Apply
```

## [5.3.0] - 2026-01-19

### Fixed

#### Bug Fixes
- **Long message notification error** (#36) - Truncate notification payload to avoid PostgreSQL NOTIFY 8KB limit
- **Non-Latin export encoding** (#34) - JSON export now uses UTF-8 encoding with RFC 5987 filename encoding
- **ChatAction photo_removed error** (#28) - Fixed `AttributeError: 'Event' object has no attribute 'photo_removed'`
- **Album grouping flaky** (#29) - Albums now save correct media_type (photo/video) instead of generic 'album'
- **Album media not downloading** (#31) - Album handler now downloads media when `LISTEN_NEW_MESSAGES_MEDIA=true`
- **Sender name position** - Fixed sender names appearing at bottom instead of top with flex-col-reverse layout

### Changed
- Improved documentation for chat filtering options (`GLOBAL_INCLUDE_CHAT_IDS` vs type-specific) (#33)

## [5.2.0] - 2026-01-18

### Fixed

#### Critical Bug Fixes
- **`get_statistics` missing** - Fixed `AttributeError: 'DatabaseAdapter' object has no attribute 'get_statistics'` at end of backup (#23)
- **FK violation on new chats** - Listener now creates chat record before inserting messages, fixing foreign key violations when adding new `PRIORITY_CHAT_IDS` (#25)
- **VIEWER_TIMEZONE not applied** - Times were showing in UTC instead of configured timezone; now properly converts from UTC to viewer timezone (#24)
- **LOG_LEVEL=WARN not working** - Added alias mapping from `WARN` to `WARNING` for Python compatibility (#26)
- **Date separators position** - Fixed date separators appearing at wrong position with flex-col-reverse layout

#### Mobile UI Improvements (iOS/Android)
- **Avatar distortion** - Chat avatars were rendering as ellipsoids on mobile; now perfectly round with `aspect-square` and `shrink-0`
- **Chat name overflow** - Long channel names caused massive header bars; now truncated with `max-width` on mobile
- **Search bar too wide** - Reduced from fixed 256px to responsive `w-28 sm:w-48 md:w-64`
- **Export button hidden** - Was pushed off-screen on small devices; now always visible with compact sizing
- **White status bar strips** - Added `theme-color` meta tag and safe area insets for proper iOS status bar theming

### Added

#### Integrated Media Lightbox
- **Image lightbox** - Click images to view fullscreen instead of opening new tab
- **Video lightbox** - Videos now open in integrated player with autoplay
- **Media navigation** - Navigate between all media (photos, videos, GIFs) with arrow keys or buttons
- **Keyboard shortcuts** - `←`/`→` to navigate, `Esc` to close
- **Play button overlay** - Video thumbnails show play button for clear affordance
- **Download button** - Download media directly from lightbox

#### Performance & UX
- **flex-col-reverse scroll** - Messages container uses CSS-based instant scroll-to-bottom (no JS hacks, better mobile performance)
- iOS Safe Area support (`env(safe-area-inset-*)`) for notch/Dynamic Island devices
- `apple-mobile-web-app-capable` meta tag for PWA-like experience
- Responsive header padding (`px-2 py-2` on mobile, `px-4 py-3` on desktop)

## [5.1.0] - 2026-01-18

### Fixed

#### iOS Safari / In-App Browser Compatibility
- **Critical**: Fixed JavaScript crash when `Notification` API is undefined (iOS Safari, in-app browsers)
  - The Vue app would crash before auth check could run, showing "Authentication is disabled"
  - Now uses `typeof Notification !== 'undefined'` check instead of optional chaining
- **Fixed**: Auth check returning `null` instead of `false` when cookie is missing
  - Python's `None and X` returns `None`, not `False` - now wrapped in `bool()`
- Added `authCheckFailed` state with helpful message for in-app browser users

#### Notification Improvements
- Added "Notifications blocked" banner when push is subscribed but browser has denied permission
- Users can unsubscribe from push directly from the banner

### Added
- **`AUTH_SESSION_DAYS`** - Configure authentication session duration (default: 30 days)
- Auth test page at `/static/test-auth.html` for debugging (temporary)

### Documentation
- Added missing env vars: `AUTH_SESSION_DAYS`, `BATCH_SIZE`, `DATABASE_TIMEOUT`, `SESSION_NAME`
- Updated mass operation protection docs to reflect actual behavior (rate limiting, not zero-footprint)

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

#### Mass Operation Rate Limiting
- **Sliding-window rate limiter** protects against mass edit/deletion attacks
- **`MASS_OPERATION_THRESHOLD`** - Max operations per chat before blocking (default: 10)
- **`MASS_OPERATION_WINDOW_SECONDS`** - Time window for counting operations (default: 30)
- First N operations are applied, then chat is blocked for remainder of window
- To prevent ANY deletions from affecting your backup, set `LISTEN_DELETIONS=false`

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
   docker compose stop telegram-backup
   ```

2. **Pull the new image:**
   ```bash
   docker compose pull
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
   docker compose up -d
   ```

**If starting fresh:** No migration needed, just use the new image.

---

## Upgrading to v4.0.6 (from v4.0.5)

> 🚨 **Database Migration Required**

v4.0.5 had a bug where chats were stored with positive IDs while messages used negative (marked) IDs, causing foreign key violations.

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker compose stop telegram-backup
   ```

2. **Run the migration script:**

   **PostgreSQL:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids.sql
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < migrate_to_marked_ids.sql
   ```

   **SQLite:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids_sqlite.sql
   sqlite3 /path/to/telegram_backup.db < migrate_to_marked_ids_sqlite.sql
   ```

3. **Pull and restart:**
   ```bash
   docker compose pull
   docker compose up -d
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
docker compose pull
docker compose up -d
```

**Your data is safe** - no database migration needed.

---

## Upgrading from v2.x to v3.0

Transparent upgrade - just pull and restart:
```bash
docker compose pull
docker compose up -d
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
