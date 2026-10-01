# Upgrading

Most upgrades are a pin change and a restart. A few releases need an extra step, listed below.

## The routine upgrade

1. Read the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md) for every release between yours and the new one. Check the table of [releases that need action](#releases-that-need-action) below.
2. Take a backup of the archive. See [Backing up the archive](backup-and-restore.md).
3. Move the pin of both images to the same new version. If you run from a clone of the repository, `git pull` brings the new pins in the stock `docker-compose.yml`. Otherwise edit both `image:` lines yourself:

    ```yaml
    services:
      telegram-backup:
        image: drumsergio/telegram-archive:8.19.0
      telegram-viewer:
        image: drumsergio/telegram-archive-viewer:8.19.0
    ```

4. Pull and recreate the containers:

    ```bash
    docker compose pull && docker compose up -d
    ```

5. Reload any viewer tab that was open during the upgrade.

`docker compose pull` on its own only pulls the tag you already pinned. Moving the pin is the upgrade.

The backup container migrates the database each time it starts. The viewer never migrates, so it returns errors for a few seconds while the backup migrates. [SQLite and PostgreSQL](../configuration/database.md) covers the exceptions for the login command. Check the schema revision after the start:

```bash
docker compose exec telegram-backup alembic current
```

Migrations only go forward. You cannot downgrade to an older release. To go back, restore the backup you took in step 2 and pin the old version again.

## Image tags

| Tag | What it is |
|-----|------------|
| `8.19.0`, `v8.19.0` | A release. The two names point at the same image. The backup and viewer images publish release tags for `linux/amd64` and `linux/arm64`. |
| `latest` | Rebuilt from pushes to `main` that touch the image's code. It can carry code that is not released yet. Do not use it. |
| `dev` | Built from pull requests opened from the repository itself, `linux/amd64` only. It is a test image. |

## Upgrading a pip install

A pip install does not migrate on its own. Upgrade the package, then run the migrations before you start the backup, the scheduler or the viewer:

```bash
pip install -U telegram-archive
telegram-archive --data-dir ./data migrate
```

Use the same `--data-dir` or database variables you run the archive with.

Run `migrate` before any other command on a new database. If another command created the database first, `migrate` fails on the first table it tries to create, with `table chats already exists` on SQLite or `relation "chats" already exists` on PostgreSQL. To fix it, delete the new database and run `migrate` first. You can also start the Docker backup image once. Its entrypoint detects this schema and marks it as current.

Packages on PyPI start with 8.17.0. See [Install from PyPI](../getting-started/pip.md).

## The module rename { #module-rename }

In 8.17.0 the code moved from the `src` package to `telegram_archive`.

- Both images keep a `src` alias. `python -m src ...` and `uvicorn src.web.main:app` keep working and run the same code. They print a one-line deprecation note on stderr.
- Images up to and including 8.16.1 only know `src`. On those, use `python -m src`.
- The PyPI package has no `src`. Use `telegram-archive` or `python -m telegram_archive`.
- The wallpaper mount path is now `/app/telegram_archive/web/static/<file>`. The old path, `/app/src/web/static/<file>`, still works.
- From a checkout of the repository, run Alembic with `alembic -c telegram_archive/alembic.ini`. Inside the backup container a bare `alembic` still works.

The stock `docker-compose.yml`, `init_auth.sh` and `init_auth.bat` use the new name.

## Releases that need action { #releases-that-need-action }

Every release needs the routine upgrade. This table lists the extra steps, newest release first.

| Release | What to do |
|---------|------------|
| 8.19.0 | Nothing to set. The first start runs migrations 034, 035 and 036, which add nullable columns and one new table, `media_versions`, and copy no data. `MASS_OPERATION_THRESHOLD` now counts deletions only, since edits are no longer limited. With `DELETION_MODE=hard`, deleting a chat, or `SKIP_MEDIA_DELETE_EXISTING` for the chats in `SKIP_MEDIA_CHAT_IDS`, the earlier media an edit replaced is removed with the message's current media. A message archived before this release keeps a pencil from a reaction-only edit until a backup reads it again. |
| 8.18.0 | Nothing. A browser that never picked a theme opens in Match system instead of Slate, unless `VIEWER_DEFAULT_THEME` pins a theme. A saved choice keeps working. See [Themes and wallpaper](../viewer/themes.md). |
| 8.17.0 | Migration 033 runs on start. The module is now `telegram_archive`, and `python -m src` keeps working. See [Upgrading to 8.17.0](#upgrading-to-8170). |
| 8.16.1 | Nothing. |
| 8.16.0 | Migration 032 runs on start. Voice transcription is on but stays idle until `TRANSCRIPTION_URL` is set. Set `TRANSCRIPTION_ENABLED=false` to hide its banner. See [Voice transcription](../configuration/transcription.md). |
| 8.15.1 | Nothing. |
| 8.15.0 | `DELETION_MODE` now defaults to `soft`, and excluded chats are kept unless `EXCLUDE_DELETE_EXISTING=true`. If you relied on deletion, set `DELETION_MODE=hard` or `EXCLUDE_DELETE_EXISTING=true` before you upgrade. Migration 031 runs on start. Upgrade both images together. |
| 8.14.0 | `SKIP_MEDIA_DELETE_EXISTING` now defaults to `false`. Set it to `true` to keep deleting the media of skipped chats. Migrations 029 and 030 run on start. |
| 8.13.0 | A restricted viewer account sees zero statistics until the next daily calculation or a refresh by the master login. If you set `DOWNLOAD_MEDIA_TYPES` and want `video_note` to match, and the archive was captured before 8.5.0, run `reclassify-round-videos` once, with the backup stopped and the viewer idle. See [Import and maintenance tasks](maintenance.md). |
| 8.12.1, 8.12.0, 8.11.3, 8.11.2, 8.11.1, 8.11.0 | Nothing. |
| 8.10.1 | Reload any viewer tab left open across the upgrade. |
| 8.10.0 | `DOWNLOAD_YOUTUBE_VIDEOS` defaults to `false`, so the video file behind a YouTube link is no longer downloaded. Set it to `true` to keep the old behaviour. See [Media downloads](../configuration/media.md#youtube-preview-videos). |
| 8.9.1 | If the viewer mounts the archive read-only, set `THUMBNAIL_CACHE_DIR` to a writable volume. Without it the thumbnail cache is lost each time the container is recreated. See [Thumbnails](../configuration/media.md#thumbnails). |
| 8.5.0 | Round video messages captured before this release stay typed as ordinary videos. Run `reclassify-round-videos` once to correct them, with the backup stopped and the viewer idle. See [Import and maintenance tasks](maintenance.md). |
| 8.3.0 | Migration 028 runs on start and indexes every existing message for full-text search. |
| Other releases from 8.0.1 to 8.9.2 | Nothing. Their migrations run on start. |

## Upgrading to 8.17.0 { #upgrading-to-8170 }

The package is renamed `telegram_archive`. See [the module rename](#module-rename). Releases are also published on PyPI, and voice transcription supports more providers. Migration 033 runs on start. Nothing is needed beyond the routine upgrade.

## Upgrading from 7.x

The jump from 7.x to 8.x has one irreversible step: migration 022. It rewrites the nine tables that hold captured data and records the account that captured each row. The tables hold chats, messages, media, reactions, earlier message versions, sync state, forum topics, folders and folder membership. After that one archive can hold several Telegram accounts. Media files do not move. Only the database changes.

Use 8.0.1 or later for this jump, never 8.0.0. 8.0.0 fails on some older archives that still hold messages from deleted chats. 8.0.1 fixes this. Going straight to the current release runs the same migrations, then every later one. One of them builds the full-text search index over your existing messages.

On PostgreSQL these migrations also create the `pg_trgm` extension. Give the database user permission to run `CREATE EXTENSION`, or have an administrator create the extension first.

1. Stop both containers. On SQLite, migration 022 refuses to run while another process holds the database file open, and that includes the viewer.

    ```bash
    docker compose stop telegram-backup telegram-viewer
    ```

2. Back up the database. This is the only way back. [Backing up the archive](backup-and-restore.md) has the commands for SQLite and PostgreSQL.

3. On SQLite, check that free space on the same filesystem is at least three times the size of the database file plus its `-wal` file, if one exists. Migration 022 checks this before it writes anything.

4. Move both image pins to the new release, as in [the routine upgrade](#the-routine-upgrade).

5. Start the backup first and wait for its migrations to finish. Then start the viewer.

    ```bash
    docker compose up -d telegram-backup
    docker compose logs -f telegram-backup
    ```

    When the log shows that the migrations are done, start the viewer:

    ```bash
    docker compose up -d telegram-viewer
    ```

If there is not enough free space, or another process holds the file, migration 022 stops with a message that ends in `Nothing has been changed.` Fix the cause and start the backup container again.

During migration 022:

- On SQLite the database peaks at a little under 2.4 times its old size.
- PostgreSQL barely grows.
- Everything you already have becomes account 1. A single-account install keeps running with no new settings.

From 8.0 on, the viewer addresses each chat by its [chat ref](../viewer/using-the-viewer.md#links-to-a-message) instead of the Telegram chat id. Old bookmarks to chats stop working. Open the chat from the sidebar to get its new address. Share links keep working: the link carries only the token, and migration 022 converts each token's chat grant to the new form. Scripts that create viewer accounts or share tokens through the admin API must send `allowed_chat_refs` instead of `allowed_chat_ids`. The API rejects the old field with HTTP 400.

## Older upgrades

Jumps from older major versions, such as v3 to v4 or v4 to v5, are described at the end of the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md#upgrading-to-v500-from-v4x).
