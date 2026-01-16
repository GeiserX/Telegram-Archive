<div align="center">
  <img src="assets/Telegram-Archive.png" alt="Telegram Archive Logo" width="200"/>
</div>

# Telegram Archive

Automated Telegram backup with Docker. Performs incremental backups of messages and media on a configurable schedule.

## Features

✨ **Incremental Backups** - Only downloads new messages since last backup  
⚡ **Real-time Sync** - Track edits and deletions instantly with listener mode (v4.1+)  
📅 **Scheduled Execution** - Configurable cron schedule  
🐳 **Docker Ready** - Easy deployment with Docker Compose  
🌐 **Web Viewer** - Browse chats with Telegram-like UI (mobile-friendly)  
🔐 **Restricted Viewer** - Share specific chats via `DISPLAY_CHAT_IDS`  
🎵 **Voice/Audio Player** - Play audio messages in browser  
📤 **Chat Export** - Export chat history to JSON  
🎬 **GIF Autoplay** - Animated GIFs play when visible  
📁 **Media Support** - Photos, videos, documents, stickers  
🔒 **Secure** - Optional authentication, runs as non-root  
🗄️ **Multiple Databases** - SQLite (default) or PostgreSQL support (v3.0+)  

## 🗺️ Roadmap

See **[docs/ROADMAP.md](docs/ROADMAP.md)** for:
- Complete version history with all features and fixes
- Planned features for v5.0 and beyond
- Future roadmap items

Have a feature request? [Open an issue](https://github.com/GeiserX/Telegram-Archive/issues)!

## 📸 Screenshots

<details>
<summary>Click to view Desktop and Mobile screenshots</summary>

### Desktop
![Desktop View](assets/Telegram-Archive-1.png)

### Mobile
<img src="assets/Telegram-Archive-2.png" width="300" alt="Mobile View">

</details>

## Docker Images

Two separate Docker images are available (v4.0+):

| Image | Purpose | Size |
|-------|---------|------|
| `drumsergio/telegram-archive` | Backup scheduler (requires Telegram credentials) | ~300MB |
| `drumsergio/telegram-archive-viewer` | Web viewer only (no Telegram client) | ~150MB |

> 📦 **Upgrading from v3.x?** See [Upgrading from v3.x to v4.0](#upgrading-from-v3x-to-v40) for migration instructions.

## Quick Start

### 1. Get Telegram API Credentials

1. Go to https://my.telegram.org/apps
2. Log in with your phone number
3. Create a new application (any name/platform)
4. Note your **API ID** (numbers) and **API Hash** (letters+numbers)

### 2. Deploy with Docker

```bash
# Clone the repository
git clone https://github.com/GeiserX/Telegram-Archive
cd Telegram-Archive

# Create data directories
mkdir -p data/session data/backups
chmod -R 755 data/

# Configure environment
cp .env.example .env
```

**Edit `.env`** with your credentials:
```bash
TELEGRAM_API_ID=12345678          # Your API ID
TELEGRAM_API_HASH=abcdef123456    # Your API Hash  
TELEGRAM_PHONE=+1234567890        # Your phone (with country code)
```

### 3. Authenticate with Telegram

```bash
# Make script executable (Linux/Mac)
chmod +x init_auth.sh

# Run authentication
./init_auth.sh    # Linux/Mac
# init_auth.bat   # Windows
```

You'll receive a code on Telegram - enter it when prompted.

### 4. Start Services

```bash
docker-compose up -d
```

**View your backup** at http://localhost:8000

### Common Issues

| Problem | Solution |
|---------|----------|
| `Permission denied` | Run `chmod -R 755 data/` |
| `init_auth.sh: command not found` | Run `chmod +x init_auth.sh` first |
| Viewer shows no data | Both containers need same database path - see [Database Configuration](#database-configuration-v30) |
| `Failed to authorize` | Re-run `./init_auth.sh` |

## Web Viewer

The standalone viewer image (`drumsergio/telegram-archive-viewer`) lets you browse backups without running the backup scheduler.

```yaml
# Example: Viewer-only deployment
services:
  telegram-viewer:
    image: drumsergio/telegram-archive-viewer:v4.1.0
    ports:
      - "8000:8000"
    environment:
      BACKUP_PATH: /data/backups
      DATABASE_DIR: /data/db
      VIEWER_USERNAME: admin
      VIEWER_PASSWORD: your-secure-password
      VIEWER_TIMEZONE: Europe/Madrid
    volumes:
      - /path/to/backups:/data/backups:ro
      - /path/to/db:/data/db:ro
```

Browse your backups at **http://localhost:8000**

Features:
- Telegram-like dark UI
- Photo/video viewer
- Voice note player
- Chat search
- Export to JSON
- Mobile-friendly layout

## Configuration

### Required

| Variable | Description |
|----------|-------------|
| `TELEGRAM_API_ID` | API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | API Hash from my.telegram.org |
| `TELEGRAM_PHONE` | Phone with country code (+1234567890) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULE` | `0 */6 * * *` | Cron schedule (every 6 hours) |
| `BACKUP_PATH` | `/data/backups` | Backup storage path |
| `DATABASE_DIR` | Same as backup | Database location |
| `DOWNLOAD_MEDIA` | `true` | Download media files |
| `MAX_MEDIA_SIZE_MB` | `100` | Max media file size |
| `CHAT_TYPES` | `private,groups,channels` | Types to backup |
| `LOG_LEVEL` | `INFO` | Logging level |
| `VIEWER_USERNAME` | - | Web viewer username |
| `VIEWER_PASSWORD` | - | Web viewer password |
| `DISPLAY_CHAT_IDS` | - | Restrict viewer to specific chats |
| `ENABLE_LISTENER` | `false` | Real-time listener for edits/deletions |
| `LISTEN_EDITS` | `true` | Apply text edits when listener is on (safe) |
| `LISTEN_DELETIONS` | `true` | ⚠️ Delete from backup when listener is on (protected by zero-footprint) |
| `MASS_OPERATION_THRESHOLD` | `10` | 🛡️ Ops count that triggers protection |
| `MASS_OPERATION_WINDOW_SECONDS` | `30` | Detection window in seconds |
| `MASS_OPERATION_BUFFER_DELAY` | `2.0` | Seconds to buffer before applying |
| `ENABLE_NOTIFICATIONS` | `false` | Enable browser push notifications in viewer |
| `SYNC_DELETIONS_EDITS` | `false` | Batch-check ALL messages for edits/deletions (expensive!) |
| `VERIFY_MEDIA` | `false` | Re-download missing/corrupted media files |
| `GLOBAL_INCLUDE_CHAT_IDS` | - | Whitelist chats globally |
| `GLOBAL_EXCLUDE_CHAT_IDS` | - | Blacklist chats globally |
| `PRIVATE_INCLUDE_CHAT_IDS` | - | Whitelist private chats |
| `PRIVATE_EXCLUDE_CHAT_IDS` | - | Blacklist private chats |
| `GROUPS_INCLUDE_CHAT_IDS` | - | Whitelist group chats |
| `GROUPS_EXCLUDE_CHAT_IDS` | - | Blacklist group chats |
| `CHANNELS_INCLUDE_CHAT_IDS` | - | Whitelist channels |
| `CHANNELS_EXCLUDE_CHAT_IDS` | - | Blacklist channels |

#### Chat ID Format

Chat IDs use Telegram's "marked" format:
- **Users**: Positive numbers (e.g., `123456789`)
- **Basic groups**: Negative numbers (e.g., `-123456789`)
- **Supergroups/Channels**: Negative with `-100` prefix (e.g., `-1001234567890`)

**Finding Chat IDs**: Forward a message from the chat to [@userinfobot](https://t.me/userinfobot) on Telegram.

**Whitelist-only mode**: Set `CHAT_TYPES=` (empty) to backup ONLY explicitly included chat IDs:
```yaml
- CHAT_TYPES=                           # Empty = no types by default
- GLOBAL_INCLUDE_CHAT_IDS=-1001234567   # Only backup this specific chat
```

### Real-time Edit/Deletion Tracking

By default, the backup runs on a schedule and only captures new messages. Edits and deletions made between backups are not tracked. You have two options:

#### Option 1: Real-time Listener ⭐

Enable `ENABLE_LISTENER=true` to run a background listener that catches edits as they happen:

```yaml
- ENABLE_LISTENER=true      # Enable real-time listener
- LISTEN_EDITS=true         # Apply text edits (default: true, safe)
- LISTEN_DELETIONS=true     # Delete from backup (protected by zero-footprint mass operation detection)
```

**How it works:**
- Stays connected to Telegram between scheduled backups
- Instantly captures message edits (and optionally deletions)
- Very efficient - only processes actual changes
- Automatically restarts if disconnected

**⚠️ IMPORTANT: Backup Protection**

By default, `LISTEN_DELETIONS=true` - deletions are synced but protected by **zero-footprint mass operation detection**. If a mass deletion is detected (>10 deletions in 30s), all pending deletions are discarded - your backup stays safe. Set to `false` if you want to keep ALL messages even when deleted on Telegram.

Only set `LISTEN_DELETIONS=true` if you explicitly want to track deletions (see protection features below).

### 🛡️ Zero-Footprint Mass Operation Protection

Even if you enable `LISTEN_DELETIONS=true`, the system has **military-grade protection** against mass deletions and edits:

```yaml
- MASS_OPERATION_THRESHOLD=10       # Block if >10 operations (aggressive default)
- MASS_OPERATION_WINDOW_SECONDS=30  # Detection window in seconds
- MASS_OPERATION_BUFFER_DELAY=2.0   # Seconds to buffer before applying
```

All three values are configurable. Tune them based on your needs:
- Lower threshold = more aggressive protection
- Shorter window = faster detection but may catch normal activity
- Longer buffer = more time to detect bursts but slightly delayed updates

#### How It Works

1. **Operations are BUFFERED** - Nothing is written to your database immediately
2. **2-second delay** - All operations wait in a buffer before being applied
3. **Burst detection** - If >10 operations arrive for a chat within the buffer window, it triggers protection
4. **ZERO footprint** - When triggered, the ENTIRE buffer is discarded. No changes written. Your backup stays intact.

#### Example Attack Scenario

Someone clears their chat history (100 messages deleted at once):
```
🛡️ ZERO-FOOTPRINT PROTECTION ACTIVATED
   Chat: -1001234567890
   Attack type: Mass deletion
   Operations intercepted: 100
   Data preserved: 100% (ZERO changes written to database)
   Chat blocked until: 2026-01-14 12:35:00
```

**Result**: All 100 messages are still safely in your backup. The attacker's deletions had no effect.

#### Why This Matters

| Without Protection | With Protection |
|-------------------|-----------------|
| 10 deletions = 10 messages lost | 10 deletions = NOTHING lost |
| Burst detected too late | Burst detected BEFORE any writes |
| Partial data loss | Zero data loss |
| Manual recovery needed | Automatic protection |

This is the core philosophy: **your backup is sacred**. Even if you opt-in to deletion tracking, you're protected against mass operations that could wipe your data.

#### Option 2: Batch Sync (One-time catch-up)

Enable `SYNC_DELETIONS_EDITS=true` to re-check ALL backed-up messages on each backup run:

```yaml
- SYNC_DELETIONS_EDITS=true
```

**⚠️ Warning:** This fetches every message in every chat to check for changes. Only use for:
- One-time initial catch-up on edits/deletions
- If you can't use the real-time listener

After running once, switch back to `ENABLE_LISTENER=true` for ongoing sync.

### Database Configuration (v3.0+)

Telegram Archive supports both SQLite and PostgreSQL.

> ⚠️ **Viewer shows no data?** Both backup and viewer containers **must access the same database**. If using SQLite, add `DB_PATH` to BOTH services in docker-compose.yml:
> ```yaml
> environment:
>   DB_TYPE: sqlite
>   DB_PATH: /data/backups/telegram_backup.db
> ```

**SQLite Path Resolution (in priority order):**

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Full database URL (highest priority) |
| `DATABASE_PATH` | Full path to SQLite file (v2 compatible) |
| `DATABASE_DIR` | Directory for `telegram_backup.db` (v2 compatible) |
| `DB_PATH` | Full path to SQLite file (v3 style) |
| Default | `$BACKUP_PATH/telegram_backup.db` |

**PostgreSQL Configuration:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | - | Full PostgreSQL URL (takes priority) |
| `DB_TYPE` | `sqlite` | Set to `postgresql` to use PostgreSQL |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_USER` | `telegram` | PostgreSQL username |
| `POSTGRES_PASSWORD` | - | PostgreSQL password (required) |
| `POSTGRES_DB` | `telegram_backup` | PostgreSQL database name |

**Using PostgreSQL:**

1. Uncomment the `postgres` service in `docker-compose.yml`
2. Set `POSTGRES_PASSWORD` in your `.env`
3. Set `DB_TYPE=postgresql` in your `.env`
4. Uncomment `depends_on` in backup and viewer services
5. Run `docker-compose up -d`

## ⚠️ Upgrading

For detailed upgrade instructions, breaking changes, and migration scripts, see **[docs/CHANGELOG.md](docs/CHANGELOG.md)**.

### Quick Reference

| From | To | Action Required |
|------|-----|-----------------|
| v4.0.5 | v4.0.6+ | **Migration required** - see CHANGELOG |
| v3.x | v4.0 | Update image names in docker-compose |
| v2.x | v3.0 | Just pull and restart |
| Fresh install | Any | No migration needed |

## CLI Commands

```bash
# View statistics
docker-compose exec telegram-backup python -m src.export_backup stats

# List chats
docker-compose exec telegram-backup python -m src.export_backup list-chats

# Export to JSON
docker-compose exec telegram-backup python -m src.export_backup export -o backup.json

# Export date range
docker-compose exec telegram-backup python -m src.export_backup export -o backup.json -s 2024-01-01 -e 2024-12-31

# Manual backup run
docker-compose exec telegram-backup python -m src.telegram_backup
```

## Data Storage

```
data/
├── session/
│   └── telegram_backup.session
└── backups/
    ├── telegram_backup.db
    └── media/
        └── {chat_id}/
            └── {files}
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Failed to authorize" | Run `./init_auth.sh` again |
| "Permission denied" | `chmod -R 755 data/` |
| Media files missing/corrupted | Set `VERIFY_MEDIA=true` to re-download them |
| Backup interrupted | Set `VERIFY_MEDIA=true` once to recover missing files |
| "duplicate key value violates unique constraint reactions_pkey" | See [Reactions Sequence Fix](#reactions-sequence-fix-postgresql) below |

### Reactions Sequence Fix (PostgreSQL)

If you see this error during backup:
```
duplicate key value violates unique constraint "reactions_pkey"
DETAIL: Key (id)=(XXXX) already exists
```

**Cause:** The PostgreSQL sequence for `reactions.id` got out of sync with the actual data. This commonly occurs after database restores or migrations.

**Solutions:**

1. **Upgrade to v4.1.2+** (recommended) - The code automatically detects and recovers from this issue.

2. **Manual fix** - Run this SQL command:
   ```bash
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup -c \
     "SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false);"
   ```

   Or use the provided script:
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/fix_reactions_sequence.sql
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < fix_reactions_sequence.sql
   ```

## Limitations

- Secret chats not supported (API limitation)
- Edit history not tracked (only latest version stored; enable `ENABLE_LISTENER=true` to track edits in real-time)
- Deleted messages before first backup cannot be recovered

## License

GPL-3.0. See [LICENSE](LICENSE) for details.

Built with [Telethon](https://github.com/LonamiWebs/Telethon).
