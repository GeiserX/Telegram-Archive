# Backing up the archive

## No built-in backup command

Telegram Archive has no backup command for the archive itself. The `export` command writes messages to a JSON file. It leaves out media files, the Telegram session and everything the viewer stores, so it is not a backup. A real backup is a copy of the data directory, plus a database dump when you run PostgreSQL.

## Where everything lives

The stock `docker-compose.yml` mounts `./data` into both containers at `/data`. Every file the backup container and the viewer write is under that directory:

```text
data/
├── session/                                  # Telegram logins, one set per account
│   ├── telegram_backup.session               # account 1, or the single account
│   ├── telegram_backup_account2.session      # accounts 2 and up: telegram_backup_account<N>
│   ├── telegram_backup.session.authenticated # copy written after each successful login
│   ├── telegram_backup.session.bak           # copy written before each connect
│   └── telegram_backup.phone_code_hash       # transient, from the non-interactive login script
└── backups/
    ├── telegram_backup.db                    # SQLite database
    ├── telegram_backup.db-wal                # recent commits not yet folded into the .db
    ├── telegram_backup.db-shm                # WAL index
    ├── .push-secret                          # secret shared by the backup and the viewer on SQLite
    └── media/
        ├── <chat_id>/                        # one folder per chat: files, or relative symlinks into _shared/
        ├── _shared/
        │   ├── .sharded                      # marks the hash-bucket layout as done
        │   ├── .repaired-175-v2              # marks the one-time file-extension repair as done
        │   └── <two hex characters>/         # deduplicated files, bucketed by content hash
        ├── avatars/
        │   ├── users/<user id>_<photo id>.jpg
        │   └── chats/<marked chat id>_<photo id>.jpg
        └── .thumbs/200/<chat_id>/            # thumbnail cache
```

Two file suffixes are temporary. A download in progress ends in `.part`. While media verification replaces a file, the backup container renames the old copy with a `.verify-bak` suffix. If the new download fails, it puts the old copy back. Neither needs to be in a backup.

With PostgreSQL there is no `.db`, `-wal`, `-shm` or `.push-secret` file. The database lives in the `postgres_data` volume instead.

The database holds more than messages. Viewer accounts, login sessions, share tokens, the audit log, the Web Push VAPID keys and settings are all stored there. Restoring the database restores all of them.

## What to copy

| Item | Why |
|------|-----|
| `data/session/` | Without it you have to log in to Telegram again. Treat it like a password. |
| The database | On SQLite, `telegram_backup.db` with its `-wal` and `-shm` files. On PostgreSQL, a `pg_dump`. |
| `data/backups/.push-secret` | Keeps real-time updates between the two containers working on SQLite. The containers create a new one if it is missing. |
| `data/backups/media/` | Every downloaded file, including `_shared/`. Keep its `.sharded` and `.repaired-175-v2` marker files. |
| `.env` and `docker-compose.yml` | Your credentials, filters and database settings. |

You can leave out `media/.thumbs/`. The viewer rebuilds thumbnails on demand.

Deduplication is on by default. With it, media folders hold relative symlinks into `_shared/`. Use a tool that copies symlinks as symlinks: `rsync -a`, `cp -a` or `tar`. A tool that follows symlinks stores every shared file once per chat, and a tool that skips them leaves the chat folders empty.

!!! warning "The copy is as sensitive as your Telegram account"
    The session files give full access to the Telegram account. The database dump carries viewer password hashes and the VAPID private key. Keep backups on storage only you can read, and encrypt them when they leave the machine.

## Take a backup

=== "SQLite"

    A running SQLite database keeps recent commits in the `-wal` file. A copy taken while the containers write can miss them or catch the database half-written. Stop both containers first:

    ```bash
    docker compose stop telegram-backup telegram-viewer
    rsync -a data/ /path/to/backup/data/
    cp .env docker-compose.yml /path/to/backup/
    docker compose start telegram-backup telegram-viewer
    ```

    Each container gets up to 90 seconds to shut down cleanly, so `stop` can take that long during a large download. `cp -a data /path/to/backup/` or `tar -czf telegram-archive-data.tar.gz data` works in place of `rsync`.

    When the backup container starts again, it runs one backup straight away. Each chat resumes from its stored position. Then the container returns to its schedule.

=== "PostgreSQL"

    Stop the two app containers so the dump and the media folder describe the same moment. Leave `telegram-postgres` running, then dump the database and copy the rest of `data/`:

    ```bash
    docker compose stop telegram-backup telegram-viewer
    docker exec telegram-postgres pg_dump -U telegram telegram_backup > telegram_backup.sql
    rsync -a data/ /path/to/backup/data/
    cp telegram_backup.sql .env docker-compose.yml /path/to/backup/
    docker compose start telegram-backup telegram-viewer
    ```

    Use your own values if you changed `POSTGRES_USER` or `POSTGRES_DB`. The `rsync` step covers the session files and media.

## Restore

Restore the database and the media from the same backup. A database newer than its media points at files that are not there. Media newer than the database holds files no row points at.

1. Stop both containers:

    ```bash
    docker compose stop telegram-backup telegram-viewer
    ```

2. Put `.env`, `docker-compose.yml` and `data/` back at the same paths. Keep the same database settings in both containers.

3. If the target already has `telegram_backup.db-wal` or `telegram_backup.db-shm` files that are not from the backup, delete them. SQLite applies a leftover WAL file to the restored database on the next open.

4. Give the files to the user the containers run as, uid 1000:

    ```bash
    sudo chown -R 1000:1000 data
    ```

5. On PostgreSQL, load the dump into an empty database. These commands drop the current one and create it again:

    ```bash
    docker compose up -d postgres
    docker exec telegram-postgres dropdb -U telegram telegram_backup
    docker exec telegram-postgres createdb -U telegram telegram_backup
    docker exec -i telegram-postgres psql -U telegram -d telegram_backup < telegram_backup.sql
    ```

6. Start the stack:

    ```bash
    docker compose up -d
    ```

When the backup container starts, it migrates a restored copy from an older release to the current one. With a pip install, run `telegram-archive migrate` before you start anything else. See [Upgrading](upgrading.md) for how migrations run.

To move a SQLite archive to PostgreSQL, see [SQLite and PostgreSQL](../configuration/database.md). Merging two archives is not supported on a current release. See [Multiple accounts](../configuration/multiple-accounts.md) for the supported way to hold a second account in one archive.

## Before an upgrade

Take a backup before every upgrade. Migrations only go forward, so that backup is the only way back. See [Upgrading](upgrading.md).

## The session file

Stop the old install before you start a restored copy on another machine. See [One client per session](../getting-started/telegram-login.md#one-client-per-session).

If a copy of the session file leaks, end that session in Telegram under **Settings > Devices**, then log in again as described in [Log in to Telegram](../getting-started/telegram-login.md). See [Session files](../getting-started/telegram-login.md#session-files) for what the file grants.

## Putting messages back into Telegram

A backup of the archive is not the same as restoring a chat inside Telegram. The backup image carries a separate script, `scripts/restore_chat.py`, that re-sends archived messages into a chat. It sends them as the Telegram account of the session, writes the original sender and time into each message's text, and uploads media again as new files. It cannot recreate the original senders or timestamps. Stop the backup service while it runs. See [One client per session](../getting-started/telegram-login.md#one-client-per-session). Its flags, including `--dry-run`, are listed under [repository scripts](../reference/cli.md#repository-scripts).
