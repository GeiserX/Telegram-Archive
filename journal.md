# Telegram Archive Installation Journal

**Repo:** https://github.com/drumsergio/telegram-archive (upstream)
**Custom fork:** https://github.com/PhenixStar/telegram-viewer
**What:** Automated Telegram message backup + web viewer. Scheduled backups every 6h, SQLite DB, media download.
**Host:** Spark (user: dgx, IP: 10.10.101.56)
**Installed:** 2026-02-21 (local dev), 2026-02-22 (Docker)
**Stack:** Python 3.14, Telethon, FastAPI, SQLAlchemy async, SQLite WAL

## Access
- **Public:** https://telegram.nulled.ai
- **Local:** http://localhost:8000
- **Containers:** telegram-backup (scheduler), telegram-viewer (web UI, port 8000)

## Infrastructure Stack
```
User → Cloudflare (HTTPS/443) → CF Tunnel → localhost:8000 (telegram-viewer)
```

### Cloudflare Tunnel
- **Tunnel ID:** e8c67e7f-3fa5-428a-abfd-1c4482ea7d42
- **Config:** /etc/cloudflared/config.yml
- **Ingress:** `telegram.nulled.ai` → `http://localhost:8000`
- **DNS CNAME ID:** 5926baf36a82859a74d5a316bc59f53b
- **CNAME target:** e8c67e7f-3fa5-428a-abfd-1c4482ea7d42.cfargotunnel.com
- **Zone:** 6a5446ecfbb65345da5cd84a2339352a (nulled.ai)

### Origin CA Cert (created but unused — tunnel handles TLS)
- **Cert ID:** 506528782212139211207153314500138252297044198461
- **Cert file:** /etc/caddy/certs/telegram-origin.crt
- **Key file:** /etc/caddy/certs/telegram-origin.key
- **Expires:** 2041-02-18

## Credentials & Secrets
- **Telegram API:** credentials in .env (TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE)
- **Viewer auth:** VIEWER_USERNAME/VIEWER_PASSWORD in .env
- **Session:** ./data/session/ (Telethon session file)

## Deploy (docker-compose)
```bash
cd ~/Desktop/tele-private/Telegram-Archive
docker compose up -d
docker compose logs --tail 50
```

## Update
```bash
cd ~/Desktop/tele-private/Telegram-Archive
docker compose pull    # only works if upstream adds arm64 images
# Or rebuild locally:
docker build -t drumsergio/telegram-archive:latest -f Dockerfile .
docker build -t drumsergio/telegram-archive-viewer:latest -f Dockerfile.viewer .
docker compose up -d --force-recreate
```

## Data
- **SQLite DB:** ./data/telegram_backup.db (~40MB)
- **Media:** ./data/backups/media/ (18 chat dirs)
- **Session:** ./data/session/

## Backup Contents
```
~/Desktop/tele-private/Telegram-Archive/
├── journal.md
├── docker-compose.yml
├── .env
├── Dockerfile
├── Dockerfile.viewer
└── data/
    ├── telegram_backup.db
    ├── session/
    └── backups/media/
```

## Pain Points & Resolutions

### 1. Architecture Mismatch (arm64)
**Problem:** Upstream images (drumsergio/telegram-archive) are amd64-only. Host is aarch64 (NVIDIA DGX).
**Resolution:** Built images locally from Dockerfiles: `docker build -t drumsergio/telegram-archive:latest .` and `docker build -t drumsergio/telegram-archive-viewer:latest -f Dockerfile.viewer .`

### 2. DB_PATH Relative Path
**Problem:** .env had `DB_PATH=data/telegram_backup.db` (relative). Container has `read_only: true`, so `os.makedirs("data")` failed with "Read-only file system" at `/app/data`.
**Resolution:** Changed .env to `DB_PATH=/data/telegram_backup.db` (absolute container path matching volume mount).

## Update Log

### 2026-02-22: Initial Docker deployment
- Built images locally for arm64 (upstream amd64-only)
- Fixed DB_PATH from relative to absolute in .env
- Started both telegram-backup and telegram-viewer containers
- Configured remote access via Cloudflare Tunnel (not MikroTik+Caddy)
- Verified backup scheduler connects to Telegram, viewer serves on localhost:8000
- Public access confirmed at https://telegram.nulled.ai
