# Telegram Backup Automation

Automated Telegram data backup system with Docker support. Performs incremental backups of your Telegram messages and media on a configurable schedule.

## Features

✨ **Incremental Backups** - Only downloads new messages since last backup  
📅 **Scheduled Execution** - Configurable cron schedule (hourly, daily, etc.)  
🐳 **Docker Ready** - Easy deployment with Docker and Docker Compose  
🔒 **Secure** - Uses official Telegram API, runs as non-root user  
📊 **Flexible Filtering** - Choose private chats, groups, and/or channels  
💾 **Point-in-time Recovery** - Export data from any specific date range  
📁 **Media Support** - Download photos, videos, documents with size limits  
🗄️ **SQLite Storage** - Efficient database with full-text search capability

## Prerequisites

### 1. Telegram API Credentials

You need to obtain API credentials from Telegram:

1. Go to https://my.telegram.org/apps
2. Log in with your phone number
3. Create a new application
4. Note down your `API_ID` and `API_HASH`

### 2. System Requirements

- **Docker**: For containerized deployment (recommended)
- **Python 3.11+**: For local development/testing
- **Disk Space**: Depends on your data (messages are small, media can be large)

## Quick Start

### Option 1: Docker Deployment (Recommended)

1. **Clone or download this repository**

2. **Create environment file**
   ```bash
   cp .env.example .env
   ```

3. **Edit `.env` with your credentials**
   ```env
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
   TELEGRAM_PHONE=+1234567890
   SCHEDULE=0 */6 * * *
   ```

4. **Run authentication setup** (one-time only)
   
   This step is **required** to generate the session file. It runs interactively to ask for your Telegram verification code (and 2FA password if enabled).

   **Windows:**
   ```batch
   init_auth.bat
   ```

   **Linux/Mac:**
   ```bash
   chmod +x init_auth.sh
   ./init_auth.sh
   ```

   **Manual Docker Command (if not using scripts):**
   ```bash
   docker-compose run --rm telegram-backup python -m src.setup_auth
   ```

5. **Start the backup service**
   ```bash
   docker-compose up -d
   ```

6. **Check logs**
   ```bash
   docker-compose logs -f
   ```

### Option 2: Local Python Installation

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create and configure `.env`**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Run authentication setup**
   ```bash
   python -m src.setup_auth
   ```

4. **Start scheduler**
   ```bash
   python -m src.scheduler
   ```

## Docker Image Only (No Repository)
   
If you don't want to clone the repository and just want to run the container:

1. **Create a directory** for your data (e.g., `telegram-backup`)
2. **Create a `.env` file** inside it (see Configuration section)
3. **Run authentication setup**:
   ```bash
   docker run --rm -it \
     --env-file .env \
     -v $(pwd)/data:/data \
     drumsergio/telegram-backup-automation:latest \
     python -m src.setup_auth
   ```
4. **Run the backup service**:
   ```bash
   docker run -d \
     --name telegram-backup \
     --restart unless-stopped \
     --env-file .env \
     -v $(pwd)/data:/data \
     drumsergio/telegram-backup-automation:latest
   ```

## Configuration

All configuration is done via environment variables in the `.env` file:

### Required Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `TELEGRAM_API_ID` | API ID from my.telegram.org | `12345678` |
| `TELEGRAM_API_HASH` | API Hash from my.telegram.org | `abcdef1234567890` |
| `TELEGRAM_PHONE` | Your phone number with country code | `+1234567890` |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULE` | `0 */6 * * *` | Cron schedule (every 6 hours) |
| `BACKUP_PATH` | `/data/backups` | Backup storage path |
| `DOWNLOAD_MEDIA` | `true` | Download media files |
| `MAX_MEDIA_SIZE_MB` | `100` | Max media file size to download |
| `CHAT_TYPES` | `private,groups,channels` | Chat types to backup |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `SESSION_NAME` | `telegram_backup` | Session file name |

### Schedule Format

The `SCHEDULE` variable uses cron format: `minute hour day month day_of_week`

Examples:
- `0 */6 * * *` - Every 6 hours
- `0 0 * * *` - Daily at midnight
- `0 */1 * * *` - Every hour
- `0 2 * * *` - Daily at 2 AM
- `0 0 * * 0` - Weekly on Sunday at midnight

### Chat Type Filtering

Set `CHAT_TYPES` to control what gets backed up:

- `private` - One-on-one conversations
- `groups` - Group chats
- `channels` - Channels you're subscribed to

Examples:
- `CHAT_TYPES=private` - Only private chats
- `CHAT_TYPES=private,groups` - Private chats and groups
- `CHAT_TYPES=private,groups,channels` - Everything

## Usage

### View Backup Statistics

```bash
# Docker
docker-compose exec telegram-backup python -m src.export_backup stats

# Local
python -m src.export_backup stats
```

### List Backed Up Chats

```bash
# Docker
docker-compose exec telegram-backup python -m src.export_backup list-chats

# Local
python -m src.export_backup list-chats
```

### Export Messages to JSON

Export all messages:
```bash
python -m src.export_backup export -o backup.json
```

Export specific chat:
```bash
python -m src.export_backup export -o chat_backup.json -c 123456789
```

Export date range (point-in-time recovery):
```bash
python -m src.export_backup export -o recovery.json \
  -s 2024-01-01 \
  -e 2024-12-31
```

### Manual Backup Run

```bash
# Docker
docker-compose exec telegram-backup python -m src.telegram_backup

# Local
python -m src.telegram_backup
```

## Data Storage

### Directory Structure

```
data/
├── backups/
│   ├── telegram_backup.session      # Authentication session
│   ├── telegram_backup.db           # SQLite database
│   └── media/                       # Downloaded media files
│       ├── 123456/                  # Chat ID
│       │   ├── 20240101_120000_1.jpg
│       │   └── 20240101_120100_2.mp4
│       └── 789012/
│           └── ...
```

### Database Schema

The SQLite database contains:
- **chats** - Chat metadata (users, groups, channels)
- **messages** - Message content and metadata
- **users** - User information
- **media** - Media file metadata and paths
- **sync_status** - Incremental sync tracking

### Backup Size Estimates

- **Text messages**: ~1-2 KB per message
- **Photos**: 100 KB - 5 MB each
- **Videos**: 1 MB - 100+ MB each
- **Documents**: Varies widely

Example: 10,000 messages with 1,000 photos ≈ 20 MB text + 1-2 GB media

## Recovery

### Restore from Specific Date

1. Export messages from desired date range:
   ```bash
   python -m src.export_backup export -o recovery.json \
     -s 2024-06-01 -e 2024-06-30
   ```

2. The JSON file contains all messages and metadata from that period

3. Media files are referenced by path in the JSON

### Full Database Access

The SQLite database can be queried directly:

```bash
sqlite3 data/backups/telegram_backup.db

# Example queries
SELECT COUNT(*) FROM messages;
SELECT * FROM chats;
SELECT * FROM messages WHERE date >= '2024-01-01' LIMIT 10;
```

## Troubleshooting

### Authentication Issues

**Problem**: "Failed to authorize"
- **Solution**: Run `setup_auth.py` again to re-authenticate
- Make sure your phone number includes country code (e.g., `+1234567890`)

**Problem**: "Two-factor authentication required"
- **Solution**: Enter your 2FA password when prompted during setup

### Backup Issues

**Problem**: "No new messages"
- This is normal if you've already backed up recent messages
- Check logs to see which chats were scanned

**Problem**: "Media download failed"
- Check `MAX_MEDIA_SIZE_MB` setting
- Ensure sufficient disk space
- Some media may be expired or deleted from Telegram

### Docker Issues

**Problem**: "Permission denied" errors
- **Solution**: Ensure data directory has correct permissions:
  ```bash
  chmod -R 755 data/
  ```

**Problem**: Container keeps restarting
- **Solution**: Check logs with `docker-compose logs`
- Verify `.env` file has correct credentials
- Ensure session file exists (run `setup_auth.py` first)

### Schedule Not Running

**Problem**: Backups don't run on schedule
- Check cron format is correct (5 fields)
- View logs: `docker-compose logs -f`
- Verify container is running: `docker-compose ps`

## Security Considerations

- **API Credentials**: Keep your `.env` file secure and never commit it to version control
- **Session Files**: The `.session` file contains authentication tokens - protect it like a password
- **Non-root User**: Docker container runs as non-root user (UID 1000) for security
- **Network**: Only connects to official Telegram servers
- **Data Privacy**: All data stays on your machine - nothing is sent to third parties

## Advanced Usage

### Multiple Accounts

To backup multiple Telegram accounts:

1. Create separate `.env` files:
   ```bash
   cp .env .env.account1
   cp .env .env.account2
   ```

2. Use different session names:
   ```env
   # .env.account1
   SESSION_NAME=account1
   
   # .env.account2
   SESSION_NAME=account2
   ```

3. Run separate containers or processes for each account

### Custom Backup Scripts

You can import the modules in your own scripts:

```python
import asyncio
from src.config import Config, setup_logging
from src.telegram_backup import run_backup

async def custom_backup():
    config = Config()
    setup_logging(config)
    await run_backup(config)

asyncio.run(custom_backup())
```

### Database Queries

Access backup data programmatically:

```python
from src.database import Database
from datetime import datetime

db = Database('data/backups/telegram_backup.db')

# Get all chats
chats = db.get_all_chats()

# Get messages in date range
messages = db.get_messages_by_date_range(
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 12, 31)
)

# Get statistics
stats = db.get_statistics()
print(f"Total messages: {stats['messages']}")

db.close()
```

## Limitations

- **Edited Messages**: Only the latest version is stored (edits are not tracked)
- **Deleted Messages**: Cannot backup messages deleted before first backup
- **Secret Chats**: Not supported (Telegram API limitation)
- **Large Media**: Files over `MAX_MEDIA_SIZE_MB` are skipped
- **Rate Limits**: Telegram may throttle if backing up very large amounts of data

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

## License

This project is provided as-is for personal use. Make sure to comply with Telegram's Terms of Service when using the API.

## Acknowledgments

- Built with [Telethon](https://github.com/LonamiWebs/Telethon) - Python Telegram client library
- Scheduled with [APScheduler](https://github.com/agronholm/apscheduler) - Advanced Python Scheduler

## Support

For issues and questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review logs for error messages
3. Open an issue on GitHub with details

---

**Note**: This tool uses the official Telegram API and operates as a regular Telegram client. It does not violate Telegram's Terms of Service when used responsibly for personal backups.
