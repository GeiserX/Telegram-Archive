# Upgrading

Most upgrades are a pin change and a restart. A few releases need an extra step, listed below.

## The routine upgrade

1. Read the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md) for every release between yours and the new one. Check the table of [releases that need action](#releases-that-need-action) below.
2. Take a backup of the archive. See [Backing up the archive](backup-and-restore.md).
3. Move the pin of both images to the same new version. If you run from a clone of the repository, `git pull` brings the new pins in the stock `docker-compose.yml`. Otherwise edit both `image:` lines yourself:

    ```yaml
    services:
      telegram-backup:
        image: drumsergio/telegram-archive:9.2.1
      telegram-viewer:
        image: drumsergio/telegram-archive-viewer:9.2.1
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
| `9.2.1`, `v9.2.1` | A release. The two names point at the same image. The backup and viewer images publish release tags for `linux/amd64` and `linux/arm64`. |
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
| 9.2.1 | Nothing. |
| 9.2.0 | An install that sets neither `ENABLE_LISTENER` nor `SCHEDULE` now runs the real-time listener and one full pass a day. Set `ENABLE_LISTENER=false` to keep the old behaviour. A `.env` or compose file copied from an older release keeps its values. See [Upgrading to 9.2.0](#upgrading-to-920). Run `telegram-archive check-media` once. A dry run now exits 1 when it finds a video or audio file whose download stopped early, and `--repair` marks such files to download again. See the [Files cut short](#cut-short-92) section. Migration 039 runs on start and files old video stickers and animated stickers as stickers. If you set `DOWNLOAD_MEDIA_TYPES`, `sticker` now covers video stickers. See the [Stickers](#stickers-92) section. |
| 9.1.0 | Nothing. To use an MTProxy, set `TELEGRAM_PROXY_TYPE=mtproxy` and `TELEGRAM_PROXY_SECRET`. See [Proxy](../reference/environment-variables.md#proxy). |
| 9.0.0 | Scripts that read a chat export must change, and so must readers of `raw_data.poll`, `raw_data.webpage` and `raw_data.entities`, `/ws/updates` clients, and scripts that press the transcript button many times as a viewer login. The first start runs migrations 034 to 038; on a large archive it takes longer. After it, run `telegram-archive check-media` once, and `telegram-archive backfill-details --apply` once with the backup stopped. See [Upgrading to 9.0](#upgrading-to-90). |
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

## Upgrading to 9.3.0 { #upgrading-to-930 }

### Custom emoji { #custom-emoji-93 }

The first start runs migration 040. It adds the `custom_emoji` table and, for every custom emoji already stored as a reaction, a record marked as not fetched yet, dated when the archive first saw that emoji. It reads the reactions and their history once and changes no other table.

The next backup run then fetches the files of those emoji into `media/_emoji`, up to 500 per account, with one request per 100 emoji and a second between requests. A large archive with more custom emoji than that gets the rest over the next runs, or at once with `telegram-archive backfill-details --apply`. The fetch does not follow `DOWNLOAD_MEDIA`: like profile photos, custom emoji are always fetched. See [Custom emoji](../configuration/media.md#custom-emoji).

Custom emoji in message text are noted as messages are stored from this release on. For messages archived before, run `telegram-archive backfill-details --apply` once with the backup stopped: it finds the custom emoji in old texts and their earlier versions without asking Telegram for the messages, and fetches their files.

Logins whose downloads are off see custom emoji, as they see profile photos.

### API clients { #api-clients-93 }

In the viewer's API and live frames, a custom emoji entity's `document_id` is now a string of digits, because a JSON number rounds it in a browser. A script or a bridge that reads `raw_data.entities` or message versions and compares `document_id` as a number must read it as a string. The chat exports keep the number. See [Custom emoji](../reference/api.md#custom-emoji).

## Upgrading to 9.2.0 { #upgrading-to-920 }

The real-time listener is on by default, and the full pass runs once a day. The listener saves new messages, their media, edits, chat actions and reactions as they happen. `LISTEN_NEW_MESSAGES_MEDIA` and `LISTEN_REACTIONS` now default to `true` as well. `LISTEN_DELETIONS` stays `false`, so the archive still keeps deleted messages untouched.

`SCHEDULE` now defaults to `0 3 * * *` when the listener is on, and stays `0 */6 * * *` when it is off. The time is the container's local time, UTC unless you set `TZ`.

Who sees a change:

- An install that sets nothing: a pip install, a `docker run` without these variables, or a compose file without these keys. It starts a listener per account at the next start, downloads new media at once, and runs one full pass a day at 03:00 instead of every 6 hours.
- An install that copied `.env.example` or `docker-compose.yml` from an older release. Those files set `ENABLE_LISTENER=false` and `SCHEDULE=0 */6 * * *` explicitly, so nothing changes. To move to the new setup, set `ENABLE_LISTENER=true` and remove the `SCHEDULE` line, or take the new `.env.example` and compose file.
- An install that keeps an hourly or other frequent `SCHEDULE` with the listener on. It still works. Frequent full passes are deprecated with the listener on, and startup logs a note. With the listener off they stay a normal choice.

To keep the old behaviour, set:

```bash
ENABLE_LISTENER=false
```

`SCHEDULE` then falls back to every 6 hours on its own.

On SQLite, the listener sends live updates to the viewer over HTTP. A backup that runs without a viewer, such as a pip install that never starts `viewer`, now logs one warning when the first update cannot be delivered, and stays quiet after that until one gets through.

`telegram-archive status` now exits 1 when `ENABLE_LISTENER` is on and an account has no running listener. A setup that runs the one-shot `backup` command from a host cron runs no listener, so set `ENABLE_LISTENER=false` there.

A running listener now stamps a heartbeat in the database every 30 seconds, and the viewer counts a listener as running only while that stamp is fresh. Upgrade both images together: a 9.2.0 viewer beside an older backup reads every listener as not running, because the older backup writes no heartbeat.

The listener settings lines (`ENABLE_LISTENER enabled`, the `LISTEN_*` values, the `EVENT_WEBHOOK` lines) now appear only in the log of the backup service that runs `schedule`. The viewer and the one-shot commands no longer print them.

### Files cut short { #cut-short-92 }

`check-media` now finds video and audio files that were cut short. A release from late 2025 stored some downloads that stopped early as complete files, and nothing in the archive knew. A dry run counts them as `Cut short` and exits 1, so a script or a health check that runs `check-media` reports them until they are repaired. Run `check-media --repair` once to mark them to download again. The next backup run downloads each one and replaces the short file only when its bytes are the start of the new download. Otherwise it keeps the short file and stores the new download beside it. See [A file cut short](../configuration/media.md#a-file-cut-short).

The backup and the real-time listener now refuse a download shorter than the size Telegram declares for the file. The backup tries it again and then records it not downloaded, so the pending downloads retry it. Before, the short file was stored as complete.

### Map pictures { #maps-92 }

New backups make one request to Telegram per location, venue and live location, once (a live location the backup reads again at a new position makes one more), and keep the map picture Telegram renders for it, a few tens of kilobytes, in the chat's media folder. `DOWNLOAD_MEDIA=false` and `SKIP_MEDIA_CHAT_IDS` turn it off as they do for every file. For locations archived before, run `telegram-archive backfill-details --apply` with the backup stopped. It fetches up to 500 pictures per run, one request each with a second between them, and says how many it deferred; run it again until none are deferred. See [Fill old locations, contacts, polls and edit flags](maintenance.md#fill-old-locations-contacts-polls-and-edit-flags).

### Stickers { #stickers-92 }

Before 9.2.0 the backup archived every video sticker as a `video`, and some older rows hold animated stickers as a `document`. The first start runs migration 039. It changes only the type of those rows to `sticker`: no file, no file name and no id changes, and nothing is deleted. A row is changed only when its file carries the name Telegram gives a sticker: a `video` ending in `_sticker.webm` within Telegram's limits for a video sticker (512 px on each side and 3 seconds at most), or a `document` ending in `_AnimatedSticker.tgs`. The earlier media of edited messages get the same fix, so the edit history calls them Sticker too. It reads the media table and the earlier-media table once each.

What changes for those rows:

- They play in the chat as stickers, and leave **Photos & Videos** and **Files** in the shared media gallery.
- Replies, the pinned bar and the chat list call them Sticker, not Video or File.
- Both chat exports give them the type `sticker`.
- Transcription no longer picks them up. A video sticker has no sound.
- `DOWNLOAD_MEDIA_TYPES` governs them as `sticker`. With a list that names `video` but not `sticker`, new video stickers are no longer downloaded. Files already on disk stay.

A file that had stickers drawn on it was archived as a `sticker` before 9.2.0. New ones are archived as the video, GIF or file they are. The migration leaves the old rows as they are: the row does not say whether the file was a video or a GIF. The chat shows them as the text "Sticker", a link that downloads the file when your login may download. A backup that reads such a message again corrects its type.

## Upgrading to 9.0 { #upgrading-to-90 }

### Migrations { #migrations-90 }

The first start runs migrations 034 to 038. 034, 035 and 036 add nullable columns (`messages.edit_hide`; `source`, `entities` and `rich_message` on `message_versions`; `media.telegram_file_id`) and one table, `media_versions`, and copy no data. 037 adds `reaction_history` and copies one baseline row per emoji from `reactions`: the last count each emoji had, and for an emoji taken back a second row with count 0. It changes no existing row. It reads `reactions` twice, so on a large archive the first start takes longer. Before 9.0, a count that dropped without reaching zero and an earlier removal of an emoji that came back were overwritten, so a message's history starts at its baseline. 038 adds `message_snapshots` and copies no data.

### One-time steps { #one-time-90 }

- Run `telegram-archive check-media`, then `telegram-archive check-media --repair` if it reports anything. See [Media files behind broken links](#media-check-90).
- With the backup stopped, run `telegram-archive backfill-details --apply`. A message archived before 9.0 keeps a pencil from a reaction-only edit until a backup reads it again, and an old location, contact or poll shows `Details not archived`. The command fills both from Telegram, at one request per 100 of those messages. See [Fill old locations, contacts, polls and edit flags](maintenance.md#fill-old-locations-contacts-polls-and-edit-flags).

### Chat exports { #exports-90 }

Both chat exports, the viewer's **Export chat** and `telegram-archive export`, change shape. A script that reads them must change:

- **`message_versions` is gone.** Read the `versions` list on each message instead. An entry no longer carries `chat_id` and `message_id`: it belongs to the message it sits under. In the command's file that message has `chat_id` and `account_id`.
- **The window applies to the message, not to the version.** The flat list picked versions by their own date. `versions` holds every version of each exported message, whatever its date. A version of a message sent outside the window is no longer in the file.
- **Each version is complete.** It has `text` and `date`, as the flat list had, and, new in 9.0, `captured_at`, `source`, `entities`, `rich_message` and `media`.
- **Messages and versions list media.** Each message has `media` and each version has `media`, as lists of `media_id`, `type`, `file_name`, `file_size`, `mime_type`, `width`, `height` and `duration`. A version that holds only earlier media has `text` null and `media_only` true. A script that rejects unknown keys must accept these.
- **`total_message_versions` counts what is under the messages.** In the command's `statistics` it is the number of entries in every message's `versions`, media-only entries included.

Every transcript in a file now names a media listed in the same file. The fields are in [Export](../reference/api.md#export) and [export](../reference/cli.md#export).

### Scripts and API clients { #clients-90 }

- **Poll and preview payloads.** `raw_data.poll` and `raw_data.webpage` keep the first capture. Read the newest poll results or preview from `snapshots` in `/api/chats/{ref}/messages` or in either export. See [Poll and link preview snapshots](../reference/api.md#poll-and-link-preview-snapshots).
- **Empty formatting.** Messages archived from now on store `entities: []` in `raw_data` when they have no formatting, and an edit that removes all formatting leaves that list. `[]` means the message has no formatting. Messages archived before stay as they are, and a missing key on one of them still means the formatting was not recorded.
- **Live frames.** An `edit` frame carries `edit_hide`, and `media` when the edit replaced the photo or file. The nested `media` of a `new_message` frame has the messages route's shape: the `{message_id}_{type}` key as `id`, a `url`, and no URL or path for a login whose downloads are off. See [Live updates over WebSocket](../reference/api.md#live-updates-over-websocket).
- **Transcript presses.** For every login but the master, the transcript POST routes answer 429 with `Retry-After` past `TRANSCRIPTION_ASK_RATE_LIMIT` presses in 10 minutes (30 by default), or while `TRANSCRIPTION_ASK_MAX_OPEN` pressed files (50) wait for the backup. `0` turns either limit off. See [Transcripts](../reference/api.md#transcripts).

### Operators { #operators-90 }

- `MASS_OPERATION_THRESHOLD` counts deletions only. Edits are no longer limited. See [Mass-operation protection](../configuration/listener.md#mass-operation-protection).
- The listener's stop statistics count an edit of a message it had not stored as **Stored as new messages**, no longer as **Skipped**.
- `DELETION_MODE=hard` and deleting a chat also remove a message's snapshots, reaction history and earlier media. `SKIP_MEDIA_DELETE_EXISTING` also removes the earlier media an edit replaced.
- The first transcription drains may send files that failed before only because their file was missing or the server failed. See [Retries](../configuration/transcription.md#retries).

### Media files behind broken links { #media-check-90 }

Run [`telegram-archive check-media`](../reference/cli.md#check-media) once after upgrading, and `telegram-archive check-media --repair` if it reports a broken link or a missing file. With the media folder missing, unreadable or empty, `VERIFY_MEDIA` skips its run with a warning instead of fetching every file again, and `check-media` exits 1 without checking. Archives that stored channel media before 4.0.5 can hold rows that say downloaded with no file behind them, and only `check-media`, `VERIFY_MEDIA` and the transcription drain look for them. A backup run without `VERIFY_MEDIA` now warns when it reads such a row, with a count only. See [A missing shared file](../configuration/media.md#a-missing-shared-file).

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
