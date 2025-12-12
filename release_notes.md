# Release Notes

## v2.2.14
### Fixes
- **Timezone Display Fix (Critical):** Fixed moment-timezone library to include timezone data. The previous version loaded `moment-timezone.min.js` which doesn't include timezone definitions. Now uses `moment-timezone-with-data.min.js` which includes all timezone data needed for proper conversion.

---

## v2.2.13
### Fixes
- **Timezone Display Fix (Improved):** Fixed timezone conversion to always treat sync_status timestamps as UTC (common in Docker containers) and convert to configured timezone. This ensures accurate time display regardless of server timezone configuration.
- **Audio Player:** Ensured audio files (.ogg, .mp3, etc.) are properly detected and displayed with native browser controls and progress bar.
- **Reactions Debugging:** Added better logging for reaction extraction and storage to help diagnose any issues.

### Technical Details
- Timezone conversion now consistently treats all timestamps as UTC before converting to configured timezone
- Audio player uses native HTML5 controls with preload="metadata" for better UX
- Reactions extraction includes debug logging to track extraction success

---

## v2.2.12
### Fixes
- **Timezone Display Fix:** Fixed timezone conversion issue where last backup time from `sync_status` table (server local time) was being double-converted. Now correctly handles both UTC timestamps from metadata and local timestamps from sync_status, ensuring accurate time display regardless of timezone source.

---

## v2.2.11
### Features
- **Message Reactions Support:** Added full support for storing and displaying message reactions (emoji reactions) in the viewer. Reactions are automatically captured during backup and displayed below each message with emoji and count, styled similar to Telegram.

### Fixes
- **Timezone Display Fix:** Fixed timezone conversion issue where last backup time from `sync_status` table (server local time) was being double-converted. Now correctly handles both UTC timestamps from metadata and local timestamps from sync_status.

### Technical Details
- New `reactions` table added to database schema (automatically created for both new and existing users)
- Reactions are extracted from Telegram messages including user information when available
- Supports both regular emoji reactions and custom emoji reactions (animated stickers)
- Reactions are displayed in the web viewer with proper styling and counts
- Timezone conversion now tracks time source to avoid double conversion

---

## v2.2.10
### Features
- **Configurable Timezone for Last Backup Time:** Added `VIEWER_TIMEZONE` environment variable to configure the timezone for displaying last backup time. Defaults to `Europe/Madrid` if not specified. Can be set in docker-compose.yml.

### Fixes
- **Last Backup Time Always Visible:** Last backup time now always displays in the viewer sidebar (shows "Never" if no backup has occurred). Previously could be hidden if metadata wasn't available.
- **Improved Timezone Handling:** Better timezone conversion using moment-timezone library. Falls back to Europe/Madrid if browser timezone is unavailable or invalid.
- **Robust Date Parsing:** Improved date parsing to handle both UTC ISO format and SQLite timestamp formats reliably.

---

## v2.2.9
### Features
- **Timezone-Aware Last Backup Time:** Last backup time is now displayed in the viewer sidebar and automatically converts to the browser's local timezone. Shows relative times (e.g., "Today at 14:30" or "Yesterday at 10:15") for better user experience.

---

## v2.2.7
### Features
- **Automated GitHub Releases:** New workflow automatically creates GitHub Releases for new tags.

### Fixes
- **Zero Storage Statistics:** Fixed an issue where media file sizes were reported as 0MB. Added self-correction logic to `telegram_backup.py` and a repair script `scripts/fix_media_sizes.py` for existing databases.

---

## v2.2.6
### Features
- **Configurable Database Timeout:** Added `DATABASE_TIMEOUT` environment variable (default: 30.0s). Increase this value to prevent "database is locked" errors on slow filesystems (e.g., Unraid/FUSE).
- **Poll Support:** Added support for archiving and viewing Telegram Polls (including Quizzes and multiple choice). Polls now render natively in the viewer with results and progress bars.

### Fixes
- Fixed `database is locked` issues on initial backup for systems with slow I/O by enabling configurable timeouts.

---

## v2.2.5
### Features
- **Enhanced Branding:** New high-resolution favicon and logo.
- **Docker Release Workflow:** Automated Docker Hub builds via GitHub Actions.
- **Documentation:** Added screenshot verification support.

### Fixes
- Fixed CI permission errors on Windows.
- Fixed Docker volume mounting issues.
