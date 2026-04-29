# Expanding Storage with mergerfs

mergerfs pools multiple disks without RAID — just add a drive, mount it in, and you instantly get more space. Because Telegram Archive uses **relative symlinks** (`../_shared/file.mp4`) for media deduplication, mergerfs handles symlink resolution across all pooled drives transparently.

## Step 1: Install mergerfs

Follow the official guide: <https://trapexit.github.io/mergerfs/latest/setup/installation/>

Ubuntu / Debian:
```bash
sudo apt install -y mergerfs
```

For the latest version (recommended), download the `.deb` from [GitHub releases](https://github.com/trapexit/mergerfs/releases).

## Step 2: Understand the paths

The project stores everything under `/data/backups`:

```
/data/backups/
├── telegram_backup.db       # SQLite database (default)
├── media/                   # ← this is what we want to pool
│   ├── _shared/             #    actual media files
│   └── 1234567890/          #    symlinks → ../_shared/file.mp4
├── session/                 #    mounted separately (NOT pooled)
└── ...
```

**Critical:** The default SQLite database lives at `/data/backups/telegram_backup.db`. Running SQLite over mergerfs (FUSE) causes locking issues and severe slowdowns. So we pool only `/data/backups/media`, NOT `/data/backups`.

## Step 3: Create the merged mount point

Nothing to move. Docker's volume layering handles it — mount the entire `./data` directory, then overlay only `media/` with mergerfs:

```bash
cd /path/to/your/telegram-archive
mkdir -p ./data/backups/media_merged
```

## Step 4: Mount mergerfs

### 4a. Example `/etc/fstab` entry

```
# <sources>                                     <mount point>                                  <type>    <options>
/home/user/telegram-archive/data/backups/media:/media/nas/telegram-archive/data/backups/media  /home/user/telegram-archive/data/backups/media_merged  mergerfs  defaults,allow_other,cache.files=off,category.create=mfs,func.getattr=newest  0  0
```

Drive config:

- Drive 1 path: `/home/user/telegram-archive/data/backups/media`
- Drive 2 path: `/media/nas/telegram-archive/data/backups/media`
- Target path: `/home/user/telegram-archive/data/backups/media_merged`

Key options:
- `category.create=mfs` — new files go to the drive with the **most free space**
- `cache.files=off` — prevents stale metadata when files move between branches
- `func.getattr=newest` — picks newest file attributes when a file exists on multiple branches
- `allow_other` — allow mount as user instead of root

### 4b. Mount and verify

```bash
sudo mount /home/user/telegram-archive/data/backups/media_merged
ls /home/user/telegram-archive/data/backups/media_merged
# Should show _shared/ and your chat directories
```

## Step 5: Update docker-compose.yml

For **both** `telegram-backup` and `telegram-viewer` services, replace:

```yaml
volumes:
  - ./data:/data
```

With:

```yaml
volumes:
  - ./data:/data
  - ./data/backups/media_merged:/data/backups/media
```

Docker mounts the whole data tree, then overlays `media/` with the mergerfs pool. Everything else (`session/`, `telegram_backup.db`) stays on direct storage.

Complete diff (apply to both services):

```diff
  services:
    telegram-backup:
      volumes:
-       - ./data:/data
+       - ./data/backups/media_merged:/data/backups/media

    telegram-viewer:
      volumes:
-       - ./data:/data
+       - ./data/backups/media_merged:/data/backups/media
```

## Step 6: Test with viewer first

Before starting the backup service, verify media loads correctly:

```bash
# Start ONLY the viewer
docker compose up -d telegram-viewer

# Open http://localhost:8000 and check:
# - Chat list loads
# - Media thumbnails and files display correctly
# - No "file not found" errors
```

**Why viewer first?** If the mount is wrong, the backup service could write new media to the wrong disk. The viewer is read-only — safe to test with.

## Step 7: Start full stack

Once the viewer works:

```bash
docker compose up -d
```

Verify the backup is writing to the correct branch:

```bash
# Check which branch new files land on
ls -lt /media/nas/telegram-archive/data/backups/media/_shared/ | head -5
```

## Step 8: Migrate existing media

If you started without mergerfs and accumulated media on a single disk, use `migrate_chat.sh` to redistribute:

```bash
# Preview what would move
./scripts/migrate_chat.sh -d \
  -s /home/user/telegram-archive/data/backups \
  -t /media/nas/telegram-archive/data/backups \
  <chat_id>

# Move a chat (copies symlinks + _shared files, verifies, then deletes source)
./scripts/migrate_chat.sh -m \
  -s /home/user/telegram-archive/data/backups \
  -t /media/nas/telegram-archive/data/backups \
  <chat_id>
```

This copies:
1. The chat's symlinks to the target branch
2. The referenced `_shared` files (skipping duplicates already on target)
3. Removes source copies after verifying sizes match

Since symlinks are relative (`../_shared/...`), they resolve correctly regardless of which branch holds the actual file.

---

## Reference

Full config reference: <https://trapexit.github.io/mergerfs/latest/config/options/>
