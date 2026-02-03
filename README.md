<div align="center">
  <img src="assets/Telegram-Archive.png" alt="Telegram Archive Logo" width="200"/>
</div>

# Telegram Archive

Automated Telegram backup with Docker. Performs incremental backups of messages and media on a configurable schedule.

## Features

### 📦 Backup Engine
- **Incremental backups** — Only downloads new messages since last backup
- **Scheduled execution** — Configurable cron schedule (default: every 6 hours)
- **Real-time listener** — Catch edits, deletions, and new messages instantly between backups
- **Album support** — Groups photos/videos sent together as albums
- **Service messages** — Tracks group photo changes, title changes, user joins/leaves
- **Forwarded message info** — Shows original sender name for forwarded messages
- **Channel signatures** — Displays post author when channels have signatures enabled
- **Media deduplication** — Symlinks identical files to save disk space
- **Avatars always fresh** — Profile photos updated on every backup run

### 🎬 Media Support
- Photos, videos, documents, stickers, GIFs
- Voice messages and audio files with in-browser player
- Polls with vote counts and results
- Configurable size limits and selective download

### 🌐 Web Viewer
- **Telegram-like dark UI** — Feels like the real app
- **Mobile-friendly** — Responsive design with iOS/Android optimizations
- **Integrated lightbox** — View photos and videos without leaving the page
- **Keyboard navigation** — Arrow keys to browse media, Esc to close
- **Real-time updates** — WebSocket sync shows new messages instantly
- **Push notifications** — Get notified even when browser is closed
- **Chat search** — Find messages by text content
- **JSON export** — Download chat history with date range filters

### 🔒 Security & Privacy
- **Optional authentication** — Password-protect your viewer
- **Restricted sharing** — Share specific chats via `DISPLAY_CHAT_IDS`
- **Mass deletion protection** — Rate limiting prevents accidental data loss
- **Runs as non-root** — Docker best practices

### 🗄️ Database
- **SQLite** (default) — Zero config, single file
- **PostgreSQL** — For larger deployments with real-time LISTEN/NOTIFY

## 🗺️ Roadmap

See **[docs/CHANGELOG.md](docs/CHANGELOG.md)** for complete version history.

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

**Option A: Using the provided scripts (recommended for fresh installs)**

```bash
# Run authentication
./init_auth.sh    # Linux/Mac
# init_auth.bat   # Windows
```

**Option B: Direct Docker command (for existing deployments or re-authentication)**

If your session expires or you need to re-authenticate an existing container:

```bash
# Generic command - adjust volume paths and credentials
docker run -it --rm \
  -e TELEGRAM_API_ID=YOUR_API_ID \
  -e TELEGRAM_API_HASH=YOUR_API_HASH \
  -e TELEGRAM_PHONE=+YOUR_PHONE_NUMBER \
  -e SESSION_NAME=telegram_backup \
  -v /path/to/your/session:/data/session \
  drumsergio/telegram-archive:latest \
  python -m src auth
```

**Example for docker compose deployment:**

```bash
# If using docker compose with a session volume
docker run -it --rm \
  --env-file .env \
  -v telegram-archive_session:/data/session \
  drumsergio/telegram-archive:latest \
  python -m src auth

# Then restart the backup container
docker compose restart telegram-backup
```

**What happens during authentication:**
1. The script connects to Telegram's servers
2. Telegram sends a verification code to your Telegram app (check "Telegram" chat)
3. Enter the code when prompted
4. If you have 2FA enabled, enter your password when prompted
5. Session is saved to the mounted volume for future use

### 4. Start Services

```bash
docker compose up -d
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
    image: drumsergio/telegram-archive-viewer:latest
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
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`/`WARN`, `ERROR`) |
| `BATCH_SIZE` | `100` | Messages per batch during backup |
| `DATABASE_TIMEOUT` | `60.0` | Database operation timeout (seconds) |
| `SESSION_NAME` | `telegram_backup` | Telethon session file name |

#### Viewer Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VIEWER_USERNAME` | - | Web viewer username |
| `VIEWER_PASSWORD` | - | Web viewer password |
| `AUTH_SESSION_DAYS` | `30` | Days before re-authentication required |
| `DISPLAY_CHAT_IDS` | - | Restrict viewer to specific chats |
| `VIEWER_TIMEZONE` | `Europe/Madrid` | Timezone for displayed timestamps |
| `SHOW_STATS` | `true` | Show backup stats dropdown in header |

#### Real-time Listener (v5.0+)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LISTENER` | `false` | Real-time listener for edits/deletions |
| `LISTEN_EDITS` | `true` | Apply text edits when listener is on |
| `LISTEN_DELETIONS` | `true` | ⚠️ Mirror deletions (protected by rate limiting) |
| `LISTEN_NEW_MESSAGES` | `true` | Save new messages in real-time |
| `LISTEN_NEW_MESSAGES_MEDIA` | `false` | Download media immediately (vs scheduled backup) |
| `LISTEN_CHAT_ACTIONS` | `true` | Track chat photo/title changes |
| `PRIORITY_CHAT_IDS` | - | Process these chats FIRST in all operations |

#### Mass Operation Protection

| Variable | Default | Description |
|----------|---------|-------------|
| `MASS_OPERATION_THRESHOLD` | `10` | 🛡️ Operations before rate limiting triggers |
| `MASS_OPERATION_WINDOW_SECONDS` | `30` | Time window for counting operations |

#### Notifications (v5.0+)

| Variable | Default | Description |
|----------|---------|-------------|
| `PUSH_NOTIFICATIONS` | `basic` | Notification mode: `off`, `basic`, `full` |
| `VAPID_PRIVATE_KEY` | - | Custom VAPID private key (auto-generated if empty) |
| `VAPID_PUBLIC_KEY` | - | Custom VAPID public key (auto-generated if empty) |
| `VAPID_CONTACT` | `mailto:admin@example.com` | VAPID contact email |

Push notification modes:
- `off` - No notifications
- `basic` - In-browser only (tab must be open)
- `full` - Web Push (works even when browser closed)

#### Backup Features

| Variable | Default | Description |
|----------|---------|-------------|
| `DEDUPLICATE_MEDIA` | `true` | Use symlinks to deduplicate identical media |
| `SYNC_DELETIONS_EDITS` | `false` | Batch-check ALL messages for edits/deletions (expensive!) |
| `VERIFY_MEDIA` | `false` | Re-download missing/corrupted media files |
| `STATS_CALCULATION_HOUR` | `3` | Hour (0-23) to recalculate backup stats |

#### Chat Filtering

There are **two modes** for selecting which chats to backup:

##### Mode 1: Whitelist Mode (Simple)

Set `CHAT_IDS` to backup **ONLY specific chats**. This is the easiest way to backup a few specific chats:

```bash
# In .env file:
CHAT_IDS=-1001234567890,-1009876543210    # Only these 2 chats, nothing else
```

When `CHAT_IDS` is set, all other filtering options (`CHAT_TYPES`, `*_INCLUDE_*`, `*_EXCLUDE_*`) are **ignored**.

##### Mode 2: Type-based Mode (Default)

If `CHAT_IDS` is not set, use `CHAT_TYPES` to backup all chats of certain types:

| Variable | Default | Description |
|----------|---------|-------------|
| `CHAT_TYPES` | `private,groups,channels` | Types of chats to backup |
| `GLOBAL_EXCLUDE_CHAT_IDS` | - | Blacklist specific chats (any type) |
| `PRIVATE_EXCLUDE_CHAT_IDS` | - | Blacklist specific private chats |
| `GROUPS_EXCLUDE_CHAT_IDS` | - | Blacklist specific groups |
| `CHANNELS_EXCLUDE_CHAT_IDS` | - | Blacklist specific channels |
| `GLOBAL_INCLUDE_CHAT_IDS` | - | Force-include specific chats (any type) |
| `PRIVATE_INCLUDE_CHAT_IDS` | - | Force-include specific private chats |
| `GROUPS_INCLUDE_CHAT_IDS` | - | Force-include specific groups |
| `CHANNELS_INCLUDE_CHAT_IDS` | - | Force-include specific channels |

**Common examples:**

```bash
# Backup all private chats and groups (no channels)
CHAT_TYPES=private,groups

# Backup all channels except one
CHAT_TYPES=channels
CHANNELS_EXCLUDE_CHAT_IDS=-1001234567890

# Backup all groups plus one specific channel
CHAT_TYPES=groups
CHANNELS_INCLUDE_CHAT_IDS=-1001234567890
```

> **Note**: `*_INCLUDE_*` variables are **additive** - they add to what `CHAT_TYPES` already includes. They don't restrict to only those chats. For exclusive selection, use `CHAT_IDS` instead.

#### Chat ID Format

Chat IDs use Telegram's "marked" format:
- **Users**: Positive numbers (e.g., `123456789`)
- **Basic groups**: Negative numbers (e.g., `-123456789`)
- **Supergroups/Channels**: Negative with `-100` prefix (e.g., `-1001234567890`)

**Finding Chat IDs**: Forward a message from the chat to [@userinfobot](https://t.me/userinfobot) on Telegram.

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

By default, `LISTEN_DELETIONS=true` - deletions are synced but protected by **rate limiting**. If a mass deletion is detected (>10 deletions in 30s), the first 10 are applied but the remaining are blocked. Set to `false` if you want to keep ALL messages even when deleted on Telegram.

### 🛡️ Mass Operation Rate Limiting

When `LISTEN_DELETIONS=true`, a **sliding-window rate limiter** protects against mass deletion attacks:

```yaml
- MASS_OPERATION_THRESHOLD=10       # Max ops before rate limiting (default: 10)
- MASS_OPERATION_WINDOW_SECONDS=30  # Time window for counting (default: 30s)
```

#### How It Works

1. **Operations apply immediately** - Normal usage (deleting a few messages) works instantly
2. **Sliding window** - System tracks operations per chat in a time window
3. **Rate limiting** - When threshold exceeded, chat is blocked for remainder of window
4. **First N applied** - The first 10 operations ARE applied, remaining are blocked

#### Example: Mass Deletion Attack

Someone deletes 50 messages in 10 seconds:
```
🛡️ RATE LIMIT TRIGGERED
   Chat: -1001234567890
   Operations in 30s: 11 (max: 10)
   First 10 were applied, remaining blocked
   Chat blocked until: 2026-01-18 12:35:00
```

**Result**: First 10 deletions were applied, but the remaining 40 were blocked. Most of your backup is preserved.

#### Complete Protection

For **zero deletions** from your backup, disable deletion sync entirely:
```yaml
- LISTEN_DELETIONS=false  # Deletions never affect your backup
```

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
5. Run `docker compose up -d`

## Updating to Latest Version

### Using Pre-built Images (Recommended)

If you're using the default `docker-compose.yml` with images from Docker Hub:

```bash
# Pull latest images and recreate containers
docker compose pull
docker compose up -d
```

Or in one command:
```bash
docker compose up -d --pull always
```

> **Note:** Running `git pull` only updates source code, not Docker images. You must use `docker compose pull` to get new container versions.

### Building from Source

If you've modified the code or prefer building locally:

```bash
git pull
docker build -t drumsergio/telegram-archive:latest .
docker build -t drumsergio/telegram-archive-viewer:latest -f Dockerfile.viewer .
docker compose up -d
```

### Pinning Versions

For production stability, pin to specific versions instead of `latest`:

```yaml
services:
  telegram-backup:
    image: drumsergio/telegram-archive:v5.3.7  # Pin to specific version
```

Check [Releases](https://github.com/GeiserX/Telegram-Archive/releases) for available versions.

## ⚠️ Upgrading (Breaking Changes)

For major version upgrades with breaking changes and migration scripts, see **[docs/CHANGELOG.md](docs/CHANGELOG.md)**.

## CLI Commands

### Local Development

#### Option 1: Install with pip (Recommended)

Install the package in editable mode to get the `telegram-archive` command:

```bash
# Install in editable mode
pip install -e .

# Now telegram-archive is available system-wide
telegram-archive --help
telegram-archive --data-dir ./data list-chats
telegram-archive --data-dir ./data stats
telegram-archive --data-dir ./data backup

# Export to JSON
telegram-archive --data-dir ./data export -o backup.json -s 2024-01-01 -e 2024-12-31
```

#### Option 2: Run directly without installation

For development without installing, use the `telegram-archive` executable script:

```bash
# Show all available commands
./telegram-archive --help

# Use custom data directory (instead of /data)
./telegram-archive --data-dir ./data list-chats
./telegram-archive --data-dir ./data stats
./telegram-archive --data-dir ./data backup

# Or symlink to PATH for easier access
sudo ln -s $(pwd)/telegram-archive /usr/local/bin/telegram-archive
telegram-archive --data-dir ./data list-chats
```

### Docker Usage

All commands use the unified `python -m src` interface inside containers:

```bash
# Show all available commands
docker compose exec telegram-backup python -m src --help

# View statistics
docker compose exec telegram-backup python -m src stats

# List chats
docker compose exec telegram-backup python -m src list-chats

# Export to JSON
docker compose exec telegram-backup python -m src export -o backup.json

# Export date range
docker compose exec telegram-backup python -m src export -o backup.json -s 2024-01-01 -e 2024-12-31

# Manual backup run (one-time)
docker compose exec telegram-backup python -m src backup

# Re-authenticate (if session expires)
docker compose exec -it telegram-backup python -m src auth
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
