# Release Notes

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
