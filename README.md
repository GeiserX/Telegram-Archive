# Telegram Backup Automation

Automated Telegram backup with Docker. Performs incremental backups of messages and media on a configurable schedule.

## Features

✨ **Incremental Backups** - Only downloads new messages since last backup  
📅 **Scheduled Execution** - Configurable cron schedule  
🐳 **Docker Ready** - Easy deployment with Docker Compose  
🌐 **Web Viewer** - Browse chats with Telegram-like UI  
🎵 **Voice/Audio Player** - Play audio messages in browser  
📤 **Chat Export** - Export chat history to JSON  
🎬 **GIF Autoplay** - Animated GIFs play when visible  
📁 **Media Support** - Photos, videos, documents, stickers  
🔒 **Secure** - Optional authentication, runs as non-root  

## Quick Start

### 1. Get Telegram API Credentials

1. Go to https://my.telegram.org/apps
2. Create a new application
3. Note your `API_ID` and `API_HASH`

### 2. Deploy with Docker

```bash
# Clone and configure
git clone https://github.com/GeiserX/telegram-backup-automation
cd telegram-backup-automation
cp .env.example .env
# Edit .env with your credentials

# Authenticate (one-time)
./init_auth.sh  # or init_auth.bat on Windows

# Start services
docker-compose up -d
```

## Web Viewer

Browse your backups at **http://localhost:8000**

Features:
- Telegram-like dark UI
- Photo/video viewer
- Voice note player
- Chat search
- Export to JSON

### Restricted Viewer Mode

Share specific chats publicly using `DISPLAY_CHAT_IDS`:

```yaml
# docker-compose.yml
telegram-channel-viewer:
  image: drumsergio/telegram-backup-automation:latest
  command: uvicorn src.web.main:app --host 0.0.0.0 --port 8000
  environment:
    BACKUP_PATH: /data/backups
    DISPLAY_CHAT_IDS: 224091347,123456789  # Only show these chats
    VIEWER_USERNAME: viewer
    VIEWER_PASSWORD: secure_password
  volumes:
    - ./data:/data
  ports:
    - "8001:8000"
```

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
| `DATABASE_DIR` | Same as backup | Database location (e.g., SSD) |
| `DOWNLOAD_MEDIA` | `true` | Download media files |
| `MAX_MEDIA_SIZE_MB` | `100` | Max media file size |
| `CHAT_TYPES` | `private,groups,channels` | Types to backup |
| `VIEWER_USERNAME` | - | Web viewer username |
| `VIEWER_PASSWORD` | - | Web viewer password |
| `DISPLAY_CHAT_IDS` | - | Restrict viewer to specific chats |
| `SYNC_DELETIONS_EDITS` | `false` | Sync deletions/edits (expensive) |

### Chat Filtering

Filter specific chats using include/exclude lists:

```env
# Exclude specific chats globally
GLOBAL_EXCLUDE_CHAT_IDS=123456789,987654321

# Include only specific channels
CHANNELS_INCLUDE_CHAT_IDS=100123456789
```

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
| Container restarting | Check `docker-compose logs` |
| No new messages | Normal if already synced |

## Limitations

- Secret chats not supported (API limitation)
- Edit history not tracked (only latest version)
- Deleted messages before first backup cannot be recovered
- Large files over `MAX_MEDIA_SIZE_MB` are skipped

## License

MIT. Built with [Telethon](https://github.com/LonamiWebs/Telethon).
