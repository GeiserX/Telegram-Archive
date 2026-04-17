<p align="center">
  <img src="docs/images/banner.svg" alt="Telegram Archive banner" width="900"/>
</p>

<p align="center">
  <img src="assets/Telegram-Archive.png" alt="Telegram Archive Logo" width="150"/>
</p>

<h1 align="center">Telegram Archive</h1>

<p align="center">
  <a href="https://hub.docker.com/r/drumsergio/telegram-archive"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-archive?style=flat-square&logo=docker" alt="Docker Pulls"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/stargazers"><img src="https://img.shields.io/github/stars/GeiserX/Telegram-Archive?style=flat-square&logo=github" alt="GitHub Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/Telegram-Archive?style=flat-square" alt="License"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/releases"><img src="https://img.shields.io/github/v/release/GeiserX/Telegram-Archive?style=flat-square" alt="Release"></a>
  <a href="https://codecov.io/gh/GeiserX/Telegram-Archive"><img src="https://codecov.io/gh/GeiserX/Telegram-Archive/graph/badge.svg" alt="codecov"></a>
</p>

<p align="center">
  <strong>Automated Telegram backup with Docker. Performs incremental backups of messages and media on a configurable schedule.</strong>
</p>

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
- **Multi-user access control** — Master account + DB-backed viewer accounts with per-user chat whitelists
- **Admin panel** — Create, edit, delete viewer accounts with fine-grained chat permissions
- **Audit logging** — Track all login attempts, admin actions, and API access
- **Authenticated media** — Media files require login and respect per-user permissions
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

**Optional: enable a SOCKS5 proxy for all Telegram connections** (useful in regions where Telegram is blocked or behind corporate firewalls)
```bash
TELEGRAM_PROXY_TYPE=socks5
TELEGRAM_PROXY_ADDR=127.0.0.1
TELEGRAM_PROXY_PORT=1080
TELEGRAM_PROXY_USERNAME=
TELEGRAM_PROXY_PASSWORD=
TELEGRAM_PROXY_RDNS=false
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
| Viewer shows no data | Both containers need same database path - see [Database Configuration](#database-configuration) |
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

All settings are configured via environment variables. Set them in your `.env` file or as `environment:` entries in `docker-compose.yml`. See [`.env.example`](.env.example) for a ready-to-use template.

> **`ENABLE_LISTENER` is a master switch.** When set to `false` (the default), all `LISTEN_*` and `MASS_OPERATION_*` variables have no effect. You only need to configure those when you set `ENABLE_LISTENER=true`.

### Environment Variables

The **Scope** column shows whether each variable applies to the backup scheduler (**B**), the web viewer (**V**), or both (**B/V**).

| Variable | Default | Scope | Description |
|----------|---------|:-----:|-------------|
| **Telegram Credentials** | | | |
| `TELEGRAM_API_ID` | *required* | B | API ID from [my.telegram.org](https://my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | *required* | B | API Hash from [my.telegram.org](https://my.telegram.org/apps) |
| `TELEGRAM_PHONE` | *required* | B | Phone number with country code (e.g., `+1234567890`) |
| `TELEGRAM_PROXY_TYPE` | - | B | Optional proxy type for all Telegram clients. Currently supports `socks5` |
| `TELEGRAM_PROXY_ADDR` | - | B | SOCKS5 proxy host or IP address |
| `TELEGRAM_PROXY_PORT` | - | B | SOCKS5 proxy port |
| `TELEGRAM_PROXY_USERNAME` | - | B | Optional SOCKS5 username |
| `TELEGRAM_PROXY_PASSWORD` | - | B | Optional SOCKS5 password |
| `TELEGRAM_PROXY_RDNS` | `false` | B | Use remote DNS resolution through the SOCKS5 proxy |
| **Backup Schedule & Storage** | | | |
| `SCHEDULE` | `0 */6 * * *` | B | Cron expression for backup frequency |
| `BACKUP_PATH` | `/data/backups` | B/V | Base path for backup data and media |
| `DOWNLOAD_MEDIA` | `true` | B | Download media files (photos, videos, documents) |
| `MAX_MEDIA_SIZE_MB` | `100` | B | Skip media files larger than this (MB) |
| `BATCH_SIZE` | `100` | B | Messages processed per database batch |
| `CHECKPOINT_INTERVAL` | `1` | B | Save backup progress every N batch inserts (lower = safer resume after crash) |
| `DATABASE_TIMEOUT` | `60.0` | B/V | Database operation timeout in seconds |
| `SESSION_NAME` | `telegram_backup` | B | Telethon session file name |
| `DEDUPLICATE_MEDIA` | `true` | B | Symlink identical media files across chats to save disk space |
| `SYNC_DELETIONS_EDITS` | `false` | B | Batch-check ALL messages for edits/deletions each run (expensive!) |
| `VERIFY_MEDIA` | `false` | B | Re-download missing or corrupted media files |
| `STATS_CALCULATION_HOUR` | `3` | B | Hour (0-23) to recalculate backup statistics daily |
| `PRIORITY_CHAT_IDS` | - | B | Comma-separated chat IDs to process first in all operations |
| `SKIP_MEDIA_CHAT_IDS` | - | B | Skip media downloads for specific chats (messages still backed up with text) |
| `SKIP_MEDIA_DELETE_EXISTING` | `true` | B | Delete existing media files and DB records for chats in skip list to reclaim storage |
| `SKIP_TOPIC_IDS` | - | B | Skip specific topics in forum supergroups (format: `chat_id:topic_id,...`) |
| `LOG_LEVEL` | `INFO` | B/V | Logging verbosity: `DEBUG`, `INFO`, `WARNING`/`WARN`, `ERROR` |
| **Chat Filtering** | | | See [Chat Filtering](#chat-filtering) below |
| `CHAT_IDS` | - | B | **Whitelist mode**: backup ONLY these chats (ignores all other filters) |
| `CHAT_TYPES` | `private,groups,channels` | B | **Type-based mode**: comma-separated chat types to backup |
| `GLOBAL_EXCLUDE_CHAT_IDS` | - | B | Exclude specific chats (any type) |
| `GLOBAL_INCLUDE_CHAT_IDS` | - | B | Force-include specific chats (any type) |
| `PRIVATE_EXCLUDE_CHAT_IDS` | - | B | Exclude specific private chats |
| `PRIVATE_INCLUDE_CHAT_IDS` | - | B | Force-include specific private chats |
| `GROUPS_EXCLUDE_CHAT_IDS` | - | B | Exclude specific groups |
| `GROUPS_INCLUDE_CHAT_IDS` | - | B | Force-include specific groups |
| `CHANNELS_EXCLUDE_CHAT_IDS` | - | B | Exclude specific channels |
| `CHANNELS_INCLUDE_CHAT_IDS` | - | B | Force-include specific channels |
| **Real-time Listener** | | | See [Real-time Listener](#real-time-listener) below |
| `ENABLE_LISTENER` | `false` | B | **Master switch** — enables all `LISTEN_*` features below |
| `LISTEN_EDITS` | `true` | B | Apply text edits in real-time |
| `LISTEN_DELETIONS` | `true` | B | Mirror deletions (protected by [mass operation rate limiting](#mass-operation-protection)) |
| `LISTEN_NEW_MESSAGES` | `true` | B | Save new messages in real-time between scheduled backups |
| `LISTEN_NEW_MESSAGES_MEDIA` | `false` | B | Also download media immediately (vs. next scheduled backup) |
| `LISTEN_CHAT_ACTIONS` | `true` | B | Track chat photo, title, and member changes |
| `MASS_OPERATION_THRESHOLD` | `10` | B | Max operations per chat before rate limiting triggers |
| `MASS_OPERATION_WINDOW_SECONDS` | `30` | B | Sliding window for counting operations (seconds) |
| `MASS_OPERATION_BUFFER_DELAY` | `2.0` | B | Seconds to buffer operations before applying |
| **Database** | | | See [Database Configuration](#database-configuration) below |
| `DATABASE_URL` | - | B/V | Full database URL (highest priority, overrides all below) |
| `DB_TYPE` | `sqlite` | B/V | Database engine: `sqlite` or `postgresql` |
| `DB_PATH` | `$BACKUP_PATH/telegram_backup.db` | B/V | Path to SQLite database file |
| `DATABASE_PATH` | - | B/V | Full path to SQLite file (v2 compatible alias for `DB_PATH`) |
| `DATABASE_DIR` | - | B/V | Directory containing `telegram_backup.db` (v2 compatible) |
| `POSTGRES_HOST` | `localhost` | B/V | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | B/V | PostgreSQL port |
| `POSTGRES_USER` | `telegram` | B/V | PostgreSQL username |
| `POSTGRES_PASSWORD` | - | B/V | PostgreSQL password (required when using PostgreSQL) |
| `POSTGRES_DB` | `telegram_backup` | B/V | PostgreSQL database name |
| **Viewer & Authentication** | | | |
| `VIEWER_USERNAME` | - | V | Web viewer username (both username and password required to enable auth) |
| `VIEWER_PASSWORD` | - | V | Web viewer password |
| `AUTH_SESSION_DAYS` | `30` | V | Days before re-authentication is required |
| `DISPLAY_CHAT_IDS` | - | V | Restrict viewer to specific chats (comma-separated IDs) |
| `VIEWER_TIMEZONE` | `Europe/Madrid` | V | Timezone for displayed timestamps ([tz database names](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)) |
| `SHOW_STATS` | `true` | V | Show backup statistics dropdown in viewer header |
| **Security** | | | |
| `CORS_ORIGINS` | `*` | V | Allowed CORS origins, comma-separated (e.g., `https://my.domain.com`). Credentials auto-disabled when `*` |
| `SECURE_COOKIES` | `auto` | V | `Secure` flag on auth cookies. Auto-detects from request protocol (`X-Forwarded-Proto` / scheme). Override with `true` or `false` |
| **Notifications** | | | |
| `PUSH_NOTIFICATIONS` | `basic` | V | `off` = disabled, `basic` = in-browser only, `full` = Web Push (works with browser closed) |
| `VAPID_PRIVATE_KEY` | *auto-generated* | V | Custom VAPID private key for Web Push |
| `VAPID_PUBLIC_KEY` | *auto-generated* | V | Custom VAPID public key for Web Push |
| `VAPID_CONTACT` | `mailto:admin@example.com` | V | Contact email included in Web Push requests |

### Chat Filtering

There are **two modes** for selecting which chats to backup:

**Mode 1 — Whitelist** (simple): set `CHAT_IDS` to backup **only** those specific chats. All other filtering variables are ignored.

```bash
CHAT_IDS=-1001234567890,-1009876543210    # Only these 2 chats, nothing else
```

**Mode 2 — Type-based** (default): use `CHAT_TYPES` to backup all chats of certain types, then fine-tune with include/exclude lists:

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

> `*_INCLUDE_*` variables are **additive** — they add chats to what `CHAT_TYPES` already selects. For exclusive selection, use `CHAT_IDS` instead.

**Chat ID format** — Telegram uses "marked" IDs:
- **Users**: positive numbers (`123456789`)
- **Basic groups**: negative (`-123456789`)
- **Supergroups/Channels**: negative with `-100` prefix (`-1001234567890`)

Find a chat's ID by forwarding a message to [@userinfobot](https://t.me/userinfobot).

**Topic filtering** — For forum-enabled supergroups, you can exclude specific topics without excluding the entire chat using `SKIP_TOPIC_IDS`:

```bash
# Skip topics 42 and 1337 in one chat, and topic 7 in another
SKIP_TOPIC_IDS=-1001234567890:42,-1001234567890:1337,-1009876543210:7
```

> Note: The topic-creating service message (1 per topic) may still be backed up since it lacks `reply_to` metadata. This does not affect user-generated content.

### Real-time Listener

The scheduled backup only captures new messages. To also track edits and deletions between backups, enable the real-time listener:

```yaml
ENABLE_LISTENER: "true"        # Master switch — required
LISTEN_EDITS: "true"           # Track text edits (safe, default: true)
LISTEN_DELETIONS: "true"       # Mirror deletions (default: true, protected by rate limiting)
LISTEN_NEW_MESSAGES: "true"    # Save new messages instantly (default: true)
```

**How it works:** stays connected to Telegram between scheduled backups, captures changes as they happen, and automatically reconnects if disconnected.

**Backup protection:** when `LISTEN_DELETIONS=true`, deletions are protected by the [mass operation rate limiter](#mass-operation-protection). Set `LISTEN_DELETIONS=false` to never delete anything from your backup.

**Alternative — batch sync:** set `SYNC_DELETIONS_EDITS=true` to check ALL backed-up messages on each scheduled run. This is expensive and slow — only use for a one-time catch-up, then switch to the real-time listener.

### Mass Operation Protection

When the listener is enabled and `LISTEN_DELETIONS=true`, a sliding-window rate limiter protects against mass deletion attacks:

1. Operations are **buffered** for `MASS_OPERATION_BUFFER_DELAY` seconds before being applied
2. A sliding window tracks operations per chat over `MASS_OPERATION_WINDOW_SECONDS`
3. When `MASS_OPERATION_THRESHOLD` is exceeded, the **entire buffer is discarded** — zero changes written

**Example:** someone deletes 50 messages in 10 seconds with default settings (threshold=10, window=30s) — the first 10 are applied, remaining 40 are blocked. For **zero** deletions from your backup, set `LISTEN_DELETIONS=false`.

### Database Configuration

Telegram Archive supports **SQLite** (default, zero-config) and **PostgreSQL** (better for large deployments with real-time LISTEN/NOTIFY).

> **Viewer shows no data?** Both backup and viewer containers must access the **same database**. Ensure `DB_TYPE` and `DB_PATH` (or `DATABASE_URL`) match in both services.

**SQLite path resolution** (highest priority first): `DATABASE_URL` → `DATABASE_PATH` → `DATABASE_DIR` → `DB_PATH` → `$BACKUP_PATH/telegram_backup.db`

**Using PostgreSQL:**

1. Uncomment the `postgres` service in `docker-compose.yml`
2. Set `DB_TYPE=postgresql` and `POSTGRES_PASSWORD` in your `.env`
3. Uncomment `depends_on` in both backup and viewer services
4. Run `docker compose up -d`

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
    image: drumsergio/telegram-archive:v7.1.7  # Pin to specific version
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

## Ecosystem

| Project | Type | Description |
|---------|------|-------------|
| [telegram-archive-mcp](https://github.com/GeiserX/telegram-archive-mcp) | MCP Server | Query archived messages from AI assistants |
| [n8n-nodes-telegram-archive](https://github.com/GeiserX/n8n-nodes-telegram-archive) | n8n Node | Workflow automation for Telegram Archive |

## Other Telegram Projects by GeiserX

- [paperless-telegram-bot](https://github.com/GeiserX/paperless-telegram-bot) — Manage Paperless-NGX documents through Telegram
- [AskePub](https://github.com/GeiserX/AskePub) — Telegram bot for ePub annotation with GPT-4
- [telegram-delay-channel-cloner](https://github.com/GeiserX/telegram-delay-channel-cloner) — Relay messages between channels with configurable delay
- [jellyfin-telegram-channel-sync](https://github.com/GeiserX/jellyfin-telegram-channel-sync) — Sync Jellyfin access with Telegram channel membership
- [telegram-slskd-local-bot](https://github.com/GeiserX/telegram-slskd-local-bot) — Automated music discovery and download via Telegram

## Supporters

> This project is made possible by generous supporters:
> **Calvin**

## License

GPL-3.0. See [LICENSE](LICENSE) for details.

Built with [Telethon](https://github.com/LonamiWebs/Telethon).
