# Command line and Python API

This page lists every `telegram-archive` command with its flags, output and exit codes. It also covers the other entry points, the scripts shipped in the backup image and the supported Python API.

## Ways to run it

`telegram-archive` and `python -m telegram_archive` are the same command line. Every command reads its configuration from environment variables, listed in [Environment variables](environment-variables.md).

=== "pip"

    ```bash
    telegram-archive --data-dir ./data list-chats
    python -m telegram_archive --data-dir ./data list-chats
    ```

=== "Docker"

    The images do not install the `telegram-archive` console script. Use `python -m telegram_archive` in the backup image. The viewer image carries no command line.

    ```bash
    # Commands that talk to Telegram or write the database
    docker compose run --rm telegram-backup python -m telegram_archive <command>

    # Read-only commands, inside the running container
    docker compose exec telegram-backup python -m telegram_archive stats
    ```

    The container's root filesystem is read-only. Write any output file under `/data`, which is the `./data` folder on the host.

### Global option

| Option | Argument | Meaning |
|--------|----------|---------|
| `--data-dir` | `PATH` | Sets `BACKUP_PATH` to `PATH/backups` and `SESSION_DIR` to `PATH/session`, and creates both directories. |

`--data-dir` must come before the subcommand. It does not override `DATABASE_URL`, `DATABASE_PATH`, `DATABASE_DIR` or `DB_PATH` when one of them is set. Without it, `BACKUP_PATH` defaults to `/data/backups` and the session directory to `/data/session`.

Running `telegram-archive` with no arguments prints help and exits 0. Running it with `--data-dir` and no subcommand creates the directories, prints help and exits 0. A missing required flag or an unknown option prints the usage and exits 2.

### Which commands need Telegram

| Needs an authorized Telegram session | Database only, no Telegram credentials |
|--------------------------------------|----------------------------------------|
| `auth`, `backup`, `schedule`, `fill-gaps`, `backfill-topics`, `reclassify-round-videos` | `migrate`, `export`, `stats`, `status`, `list-chats`, `import`, `merge` |

!!! warning "One client per session"
    Stop the backup service before any command that connects to Telegram. See [One client per session](../getting-started/telegram-login.md#one-client-per-session).

!!! note "With pip, run migrate by hand"
    The backup image migrates the database each time it starts. A pip install does not. Run [`migrate`](#migrate) before any other command, and again after every upgrade. See [Install from PyPI](../getting-started/pip.md).

## auth { #auth }

```text
telegram-archive [--data-dir PATH] auth
```

Takes no flags.

Logs in to Telegram interactively and saves the session file. It walks every configured account in turn and skips accounts whose session is already authorized. For each login it prompts `Enter verification code: `. When two-step verification is on, it also prompts `Enter your 2FA password: `. It then checks that the logged-in phone number matches the configured one and fails if they differ. A failure stops the walk, so the accounts after it are not tried. Fix that account and run `auth` again. It does not touch the database. See [Log in to Telegram](../getting-started/telegram-login.md).

It prints progress lines, then a blank line and one of two endings. On success it prints `✓ Setup completed successfully!` followed by a short "Next steps" block. On failure it prints `✗ Setup failed. Please check the errors above.` On a permission error it prints how to give the container's user, uid 1000, write access to the data directory.

It exits 0 when every account is authorized and 1 on any failure.

## backup { #backup }

```text
telegram-archive [--data-dir PATH] backup
```

Takes no flags.

Backs up every configured account once, then exits. It first moves the files in `media/_shared` into subfolders named after the first two characters of each file's SHA-256 hash. This happens once and does nothing on later runs. It runs no gap-fill and writes no heartbeat. See [Your first backup](../getting-started/first-backup.md) and [Schedule and backup tuning](../configuration/schedule.md).

It prints log lines only.

It exits 0 on success. A configuration error, a session that is not authorized or any other error ends the command with a traceback and a non-zero exit code. With several accounts, the command logs a failing account and continues with the others. It fails only when every account failed.

## schedule { #schedule }

```text
telegram-archive [--data-dir PATH] schedule
```

Takes no flags.

Runs the scheduler until stopped. The stock compose file runs this command. It does the same one-time media move as `backup`, starts the real-time listeners when `ENABLE_LISTENER=true`, and runs one backup straight away. After that it backs up on the `SCHEDULE` cron expression, `0 */6 * * *` by default. With `FILL_GAPS=true` it runs gap-fill after each backup. It writes a heartbeat file every 30 seconds for the container health check. See [Schedule and backup tuning](../configuration/schedule.md).

It prints log lines only. It runs until stopped. A configuration error or a fatal error exits 1.

## migrate { #migrate }

```text
telegram-archive [--data-dir PATH] migrate
```

Takes no flags.

Runs `alembic upgrade head` with the Alembic configuration bundled in the package. It finds the database through the same variables as every other command, and creates the SQLite directory when it is missing. Unlike the image's entrypoint, it does not stamp a database that has tables but no migration history. See [SQLite and PostgreSQL](../configuration/database.md#migrations).

On success it prints `Database schema is up to date.` and exits 0. On failure it prints `Migration failed: <error>` on stderr and exits 1.

## export { #export }

```text
telegram-archive [--data-dir PATH] export -o FILE [-c CHAT_ID] [-s YYYY-MM-DD] [-e YYYY-MM-DD]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| `-o` | `--output` | `FILE` | yes | Output JSON file. Its parent directory is created. |
| `-c` | `--chat-id` | `CHAT_ID` | no | Export only this chat's messages. |
| `-s` | `--start-date` | `YYYY-MM-DD` | no | Keep messages dated on or after midnight UTC of this day. |
| `-e` | `--end-date` | `YYYY-MM-DD` | no | Keep messages dated on or before midnight UTC of this day. |

Writes messages from the archive to one JSON file. It exports no media rows and no media files. For a full copy of the archive, see [Backing up the archive](../operations/backup-and-restore.md).

!!! warning "The end date is exclusive in practice"
    Both dates are compared as midnight UTC. `-e 2024-12-31` keeps messages up to 00:00 on 31 December and leaves out the rest of that day. To include the whole of 2024, use `-s 2024-01-01 -e 2025-01-01`.

The command writes the file with an indent of 2 and keeps non-ASCII text as UTF-8. It writes dates and other values JSON cannot hold as strings. The top-level keys are:

| Key | Content |
|-----|---------|
| `export_date` | The time of the export, UTC, ISO 8601. |
| `filters` | `chat_id`, `start_date` and `end_date` as given, or `null`. |
| `statistics` | `total_messages`, `total_chats`, `total_message_versions` and `total_transcripts`. |
| `chats` | Every chat in the archive, even with `-c`. |
| `messages` | The selected messages, ordered by date, oldest first. |
| `message_versions` | Earlier texts of edited messages: `chat_id`, `message_id`, `text` and `date`. |

Each message has `id`, `chat_id`, `sender_id`, `sender_name`, `date`, `text`, `reply_to_msg_id`, `reply_to_top_id`, `reply_to_text`, `forward_from_id`, `edit_date`, `edit_hide`, `raw_data`, `created_at`, `is_outgoing`, `is_pinned`, `is_deleted`, `deleted_at` and `account_id`. Messages deleted in soft mode are included, with `is_deleted` set to 1. `edit_hide` is 1 when Telegram says the edit at `edit_date` is not to be shown, as it does when only the reactions changed: such a message was not edited unless `message_versions` holds an earlier text of it. It is null when the source did not report the flag: a message archived before the archive kept it, or one from a Telegram export import. A null flag counts as shown. A message with voice or media transcripts also has a `transcripts` list.

It exits 0 on success. On any failure, a date in the wrong format included, it prints `Export failed: <error>` on stderr and exits 1.

## stats { #stats }

```text
telegram-archive [--data-dir PATH] stats
```

Takes no flags.

Prints the archive's totals. It reads the figures cached in the database. A completed backup, a gap-fill that recovered messages, the viewer's first start when nothing is cached yet, the viewer's daily job or a `POST /api/stats/refresh` call from a master login recalculates them. Until one of those has run, every figure is 0.

It prints:

```text
============================================================
Backup Statistics
============================================================
Total chats:        <n>
Total messages:     <n>
Media files:        <n>
Total storage:      <n> MB
============================================================
```

It exits 0 on success. On failure it prints `Stats failed: <error>` on stderr and exits 1.

## status { #status }

```text
telegram-archive [--data-dir PATH] status [--json]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| | `--json` | | no | Print the status as JSON instead of text. |

Says whether the archive is healthy, for a cron job or a monitoring check. It reads the database directly, so the viewer does not need to run and no viewer login is needed. It reports what the master login's [Archive status](../viewer/using-the-viewer.md#archive-status) page shows, except transcription: the last backup run, the listener of each Telegram account, the media counts, when the statistics were last calculated, and the database backend and size. It prints counts and times only, never chat ids, titles or text.

The archive is unhealthy when one of these holds:

- No backup run has ever started.
- The last run did not finish. It is not running, and the statistics a run writes after its message sweep are older than its start.
- `SCHEDULE` has fired twice since the last run started. One missed tick is allowed, because a tick that arrives while a run is still going is skipped. A run still going after two ticks counts as missed, so the first backup of a large archive reads `UNHEALTHY` until it completes.

The schedule check uses the local time of the command, as the scheduler does. Run it with the same `TZ` as the backup service. Inside the backup container this is already the case.

The schedule check reads `SCHEDULE` even when runs are started another way, for example the one-shot [`backup`](#backup) command from a host cron. Set `SCHEDULE` to the real cadence, or the check reports missed runs.

A database that does not exist yet is created empty and reads as `no backup has run yet`. Before you trust that verdict, check that `DATABASE_URL` or `BACKUP_PATH` points at the archive.

!!! note "What it cannot see"
    A run writes its statistics after the message sweep and before the media retries, media verification, transcription and gap-fill. The command does not report a failure in those later steps. Read the logs for them.

    The viewer also recalculates the statistics: on its first start when nothing is cached yet, in its daily job, and on a `POST /api/stats/refresh` call from the master login. A recalculation after a failed run hides that failure until the next run starts. With several Telegram accounts, the start time and the statistics are shared. An account that failed before another one completed is not reported. The logs name the failed account.

By default it prints:

```text
Archive status: healthy
  Last backup started:  <time>
  Statistics updated:   <time>
  Listener, account <n>: active since <time>
  Media files:          <n> downloaded, <n> pending, <n> exhausted, <n> skipped
  Database:             <sqlite|postgresql>, <size>
```

`(running now)` follows the start time while a run is going. A time that was never recorded reads `never`. A listener that is off reads `not running`. When the archive is unhealthy, the first line reads `Archive status: UNHEALTHY` and a `Problems:` list with one line per reason ends the output.

With `--json` it prints the JSON that [`GET /api/status`](api.md#health-and-status) returns, with two more keys: `healthy`, true or false, and `problems`, the list of reasons. Log lines go to stderr, so stdout holds only the JSON.

It exits 0 when the archive is healthy and 1 when it is unhealthy. When the configuration is invalid, the database cannot be reached or read, or `SCHEDULE` is not a valid cron expression, it prints `Status failed: <error>` on stderr and exits 1.

## list-chats { #list-chats }

```text
telegram-archive [--data-dir PATH] list-chats
```

Takes no flags.

Prints every chat in the database as a table with the columns `ID`, `Type`, `Name` and `Last Updated`, followed by `Total: N chats`. The name is the chat title, or the first and last name for a private chat. `Last Updated` is `N/A` when the chat has no update time.

It exits 0 on success. On failure it prints `List chats failed: <error>` on stderr and exits 1.

## import { #import }

```text
telegram-archive [--data-dir PATH] import -p DIR [-c CHAT_ID] [--dry-run] [--skip-media] [--merge]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| `-p` | `--path` | `DIR` | yes | Telegram Desktop export folder, holding `result.json` or `messages.html`. |
| `-c` | `--chat-id` | `CHAT_ID` | for HTML exports | Chat id in marked format, for example `-1001234567890`. |
| | `--dry-run` | | no | Parse and validate without writing to the database or copying media. |
| | `--skip-media` | | no | Import messages and metadata only. |
| | `--merge` | | no | Allow importing into a chat that already has messages. |

Imports a Telegram Desktop export into the archive. Formats, resuming and the other details are in [Import and maintenance tasks](../operations/maintenance.md).

It prints `Import complete:` with the number of chats, messages and media files, then one line per chat. With `--dry-run` the heading starts with `[DRY RUN]`.

It exits 0 on success. On failure it prints `Import failed: <error>` on stderr and exits 1.

## merge { #merge }

```text
telegram-archive [--data-dir PATH] merge --source SOURCE [--source-media DIR] [--account LABEL_OR_ID] [--add-missing-parents] [--dry-run]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| | `--source` | `SOURCE` | yes | The other archive: a SQLite file path or a database URL. |
| | `--source-media` | `DIR` | no | The other archive's media folder. Defaults to `media` beside a SQLite source file. |
| | `--account` | `LABEL_OR_ID` | no | Merge only this source account, by label or account id. A label wins when a value could be both. Without it, every account. |
| | `--add-missing-parents` | | no | Add an empty placeholder chat, message, folder or user for each source row whose parent row the source lacks. Needed to merge such a SQLite source into PostgreSQL. |
| | `--dry-run` | | no | Run every check and print the counts and the media size without writing. |

Copies every Telegram account of another archive, the source, into this archive under new account ids, with the rows each account owns and their media files. Both archives must be at the same, current schema revision. Stop both installs first.

The source is only read. Nothing already in the target is changed or deleted. Viewer accounts, viewer sessions, share links and push subscriptions are not merged. A SQLite source whose `-wal` file still holds changes needs a writable folder, because SQLite writes a `-shm` file beside it to read them. What it copies, what it refuses and a worked example are in [Merge two archives](../operations/maintenance.md#merge-two-archives).

It prints `Merge complete:`, then one `Source account <n> -> target account <n>` line per account and the rows added per table. When `--add-missing-parents` added placeholder rows, it lists them per table. When `--account` left out the transcript that a copied transcript points at, a `Transcript copy links left empty` line gives the count. Then come the media files copied, `_shared` files copied, links created, files already in the target, files missing in the source folder, avatar files and the size in MB. Without a source media folder, those media lines are replaced by `Media files: not copied (no source media folder; pass --source-media)`. With `--dry-run` the heading is `[DRY RUN] Merge plan, nothing written:`.

It exits 0 on success. When a check fails, it prints `Merge refused: <reason>` on stderr and exits 1. On any other error it prints `Merge failed: <error type>. Nothing was committed to the target database.` on stderr and exits 1.

## fill-gaps { #fill-gaps }

```text
telegram-archive [--data-dir PATH] fill-gaps [-c CHAT_ID] [-t THRESHOLD]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| `-c` | `--chat-id` | `CHAT_ID` | no | Scan only this chat. |
| `-t` | `--threshold` | `THRESHOLD` | no | Minimum gap size to investigate. Overrides `GAP_THRESHOLD`, which defaults to 50. |

Scans archived chats for holes in their message id sequences and fetches the missing messages from Telegram. A hole before a chat's earliest archived message is reported but never filled. When messages were recovered, the cached statistics are recalculated. See [Schedule and backup tuning](../configuration/schedule.md).

It prints:

```text
Gap-fill complete:
  Chats scanned: <n>
  Chats with gaps: <n>
  Total gaps found: <n>
  Messages recovered: <n>
  Chats with history missing before their earliest archived message: <n> (reported only - ...)
  - <chat name> (ID <id>): <n> gaps, <n> recovered[, ~<n> ids missing before id <id>]
```

The line about missing earlier history appears only when there is such a chat. With several accounts it is not printed at all. A chat's line ends with `~<n> ids missing before id <id>` when the archive lacks that chat's earlier history.

It exits 0 on success. On failure it prints `Gap-fill failed: <error>` on stderr and exits 1.

## backfill-topics { #backfill-topics }

```text
telegram-archive [--data-dir PATH] backfill-topics -c CHAT_ID
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| `-c` | `--chat-id` | `CHAT_ID` | yes | The forum chat to sweep again. |

Imported forum messages carry no topic, so they land in the General topic. This command gives them their topics back. It resets the chat's [position](glossary.md#position) to zero for every account. Then it backs up that chat once, on its own. Media downloads, edit and deletion sync, and media verification are off for this run. The sweep rewrites each message's topic in place. See [Import and maintenance tasks](../operations/maintenance.md).

!!! warning "Per-account whitelists win"
    An account with its own `TG_ACCOUNT_<N>_CHAT_IDS` keeps that whitelist. For that account the command sweeps the account's own chats, not the requested one.

When the chat is not in the archive, it prints one line saying the chat is not in the archive yet and to import or back it up first, and exits 1. When the position reset fails, it prints `Topic backfill failed: <error>` on stderr and exits 1. Otherwise it prints log lines and exits as `backup` does.

## reclassify-round-videos { #reclassify-round-videos }

```text
telegram-archive [--data-dir PATH] reclassify-round-videos [-c CHAT_ID] [--dry-run]
```

| Short | Long | Argument | Required | Meaning |
|-------|------|----------|----------|---------|
| `-c` | `--chat-id` | `CHAT_ID` | no | Only this chat. Without it, every chat with videos. |
| | `--dry-run` | | no | Report what would change without writing. |

Archives captured before 8.5.0 stored round video messages as ordinary videos. This command asks Telegram which archived videos are round, with one filtered search per chat, and changes the type of those rows in place. Nothing is downloaded, renamed or deleted.

Run it while nobody has the viewer open. An open tab shows `missing from the archive disk` for a changed video until you reload the tab.

It prints:

```text
Round-video reclassification complete:
  Chats scanned:      <n>
  Round videos found: <n>
  Rows re-typed:      <n>
```

A `Chats with errors:` line follows when some chats failed. With several accounts, an account that failed altogether counts as one error there. With one account, that failure ends the command. With `--dry-run` the heading starts with `[DRY RUN]`.

It exits 0 on success. On failure it prints `Reclassification failed: <error>` on stderr and exits 1.

## Other entry points

| Command | What it does |
|---------|--------------|
| `python -m telegram_archive.setup_auth` | The same login as `auth`. |
| `python -m telegram_archive.export_backup COMMAND` | A separate command line for three read commands: `export`, `list-chats` and `stats`. It takes the same `export` flags. It has no `--data-dir`. On failure it logs `Export failed: <error>` and exits 1. |
| `python -m telegram_archive.listener` | Starts the real-time listener on its own, with its own Telegram client. See [Real-time listener](../configuration/listener.md) and [One client per session](../getting-started/telegram-login.md#one-client-per-session). |
| `python -m telegram_archive.telegram_backup` | The same as `backup`, without `--data-dir`. A failure ends in a traceback. |
| `python -m telegram_archive.scheduler` | The same as `schedule`, without `--data-dir`. |
| `python -m telegram_archive.config` | Builds the configuration and logs a short self-check: the API id, whether a phone number is set, the schedule and the chat types. It never prints the phone number. On an invalid value it prints `Configuration error: <error>` and still exits 0, so read the output. Like every command except `migrate`, it creates the data directories. |
| `uvicorn telegram_archive.web.main:app --host 127.0.0.1 --port 8000` | Runs the viewer without Docker. See [Install from PyPI](../getting-started/pip.md). |
| `alembic -c telegram_archive/alembic.ini upgrade head` | Runs the migrations from a repository checkout. Inside the backup image a bare `alembic`, such as `alembic current`, works because the image sets `ALEMBIC_CONFIG`. |
| `./telegram-archive` | A script at the repository root that runs the command line from a checkout without installing the package. The dependencies must already be installed in the Python it runs with. See [Install from PyPI](../getting-started/pip.md#from-a-git-checkout). |

### Health checks

The images run these as their Docker health checks. Both exit 0 when healthy and 1 when not.

| Script | Checks | Variables |
|--------|--------|-----------|
| `/app/scripts/healthcheck_backup.py` | The heartbeat file that `schedule` rewrites every 30 seconds is younger than the maximum age. A missing file is unhealthy. | `HEARTBEAT_FILE`, default `/tmp/telegram-archive.heartbeat`. `HEARTBEAT_MAX_AGE_SECONDS`, default 180. |
| `/app/scripts/healthcheck_viewer.py` | `GET` on the viewer's health URL, with a 5 second timeout, answers 200 with `status` set to `ok`. | `HEALTHCHECK_URL`, default `http://127.0.0.1:8000/api/health`. |

The backup check does not prove that backups succeed. See [Monitoring and troubleshooting](../operations/troubleshooting.md).

## Repository scripts { #repository-scripts }

These scripts ship in the backup image under `/app/scripts` and in the repository. They are not in the PyPI package. Run them in the backup container:

```bash
docker compose run --rm telegram-backup python scripts/<name>.py [flags]
```

Most of them find the database through the same variables as the application. `migrate_media_paths.py` does not. It tries `DATABASE_URL` first, then `DB_TYPE=postgresql` with the `POSTGRES_*` variables, then `$BACKUP_PATH/telegram_backup.db`. It ignores `DATABASE_PATH`, `DATABASE_DIR` and `DB_PATH`. Pass `--db-url` when the database lives elsewhere. `deduplicate_media.py` and `cleanup_legacy_avatars.py` only touch files.

| Script | Purpose | Flags |
|--------|---------|-------|
| `auth_noninteractive.py` | Logs in without a terminal, in two steps. `send` requests the code and stores its hash beside the session file. `verify` signs in with the code, and the 2FA password when one is set. It covers the single legacy account only, from `TELEGRAM_*` variables. See [Log in to Telegram](../getting-started/telegram-login.md#log-in-without-a-terminal). | `send`, then `verify CODE [2FA_PASSWORD]`. `TELEGRAM_PHONE_CODE_HASH` can replace the stored hash. |
| `migrate-sqlite-to-postgres.py` | Copies a SQLite archive into an empty PostgreSQL database and checks the row counts. Stop the backup container first. See [SQLite and PostgreSQL](../configuration/database.md#move-an-existing-sqlite-archive-to-postgresql). | `-s`/`--sqlite PATH`, `-p`/`--postgres URL`, `-b`/`--batch-size N` (default 1000), `-v`/`--verify-only`, `-n`/`--dry-run` |
| `restore_chat.py` | Re-sends archived messages into a Telegram chat as the Telegram account of the session. Each message carries its original sender and time in its text. Media is uploaded again as new files. | See below. |
| `detect_albums.py` | Groups media sent close together into albums in older archives. | `--dry-run`, `--window SECONDS` (default 2) |
| `deduplicate_media.py` | Moves duplicate media files into `media/_shared` and links them from the chat folders. | `--dry-run`, `-v`/`--verbose` |
| `update_media_sizes.py` | Fills in missing media file sizes from the files on disk. | `--dry-run`, `--force` |
| `normalize_grouped_ids.py` | Stores every album id in the same form. | `--dry-run` |
| `cleanup_legacy_avatars.py` | Deletes old-style avatar files that have a new-style replacement. | `--dry-run`, `--backup-path PATH` (default `/data/backups`) |
| `migrate_media_paths.py` | Renames old media folders of groups and channels to their marked ids and updates the database paths. | `--dry-run`, `--media-path PATH` (default `$MEDIA_PATH` or `/data/backups/media`), `--db-url URL` |
| `healthcheck_backup.py`, `healthcheck_viewer.py` | The container health checks described above. | none |
| `fix_reactions_sequence.sql`, `migrate_to_marked_ids.sql`, `migrate_to_marked_ids_sqlite.sql` | SQL helpers. Run them with `sqlite3` or `psql` outside the image. | none |
| `generate_dummy_db.py` | A development tool that builds a demo archive. | `--data-dir`, `--force` |
| `fix_media_sizes.py` | Broken. It fails with `ImportError`. | none |

[Import and maintenance tasks](../operations/maintenance.md) explains when and how to run the maintenance scripts.

### restore_chat.py

```text
python scripts/restore_chat.py (--chat ID | --source-chat ID --dest-chat ID)
    [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--limit N]
    [--delay SECONDS] [--no-media] [--dry-run]
```

| Flag | Argument | Meaning |
|------|----------|---------|
| `--chat` | `ID` | Restore this chat into itself. |
| `--source-chat` | `ID` | Restore from this chat. Needs `--dest-chat`. |
| `--dest-chat` | `ID` | Send into this chat. |
| `--after` | `YYYY-MM-DD` | Only messages after this date. |
| `--before` | `YYYY-MM-DD` | Only messages before this date. |
| `--limit` | `N` | At most this many messages. |
| `--delay` | `SECONDS` | Pause between messages. Default 2.0. |
| `--no-media` | | Send text only. |
| `--dry-run` | | Show what would be sent without sending. |

The session is `SESSION_PATH` when set. Otherwise it is `SESSION_DIR` joined with `SESSION_NAME`, and `SESSION_DIR` defaults to `/data/session`. It reads `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.

!!! danger "It sends real messages"
    Stop the backup service first. See [One client per session](../getting-started/telegram-login.md#one-client-per-session). Always run it with `--dry-run` first and read what it would send.

## Python API

The package exposes four names:

```python
from telegram_archive import Config, TelegramBackup, run_backup, __version__
```

`Config`, `TelegramBackup` and `run_backup` load on first use, so `import telegram_archive` works where telethon is not installed, as in the viewer image. Every other module is internal.

Before you call the API, you need:

- the environment variables the command line reads
- a database migrated with [`migrate`](#migrate) using those variables
- an authorized session from [`auth`](#auth)

### Config

`Config()` reads every setting from the environment when it is built. It creates `BACKUP_PATH`, the session directory, the database directory and, when `DOWNLOAD_MEDIA` is on, the media directory. An invalid value raises at construction. For example, a non-numeric `TELEGRAM_API_ID` raises `ValueError`.

`config.validate_credentials()` raises `ValueError` when `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` or `TELEGRAM_PHONE` is missing in single-account mode.

### run_backup

```python
await run_backup(config, client=None, *, account_id=None)
```

A coroutine that returns `None`.

- Without `client`, it backs up every configured account in turn, each with its own session file. With one account, an error propagates. With several, it logs a failing account by its index and continues with the others. It raises `RuntimeError("all N configured accounts failed to back up")` only when every account failed.
- With `client`, an already connected and authorized Telethon `TelegramClient`, it backs up only that client's account.

Unlike the `backup` command, it does not run the one-time move of `media/_shared`. It runs no gap-fill and starts no listener.

A complete script:

```python
import asyncio
import os

os.environ.setdefault("BACKUP_PATH", "./data/backups")

from telegram_archive import Config, run_backup

asyncio.run(run_backup(Config()))
```

`TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE` come from the environment. With `BACKUP_PATH=./data/backups`, the session directory defaults to `./data/session` and the SQLite database to `./data/backups/telegram_backup.db`.

### TelegramBackup

`TelegramBackup` is the class `run_backup` drives. It is lower level: prefer `run_backup` unless you need to control the steps.

```python
backup = await TelegramBackup.create(config, client=None, *, account_id=None, account=None, account_resolver=None)
```

`create()` opens its own database connection. It needs either `account_id` or `account_resolver`. `account_id` is the archive's row id for the account. `account_resolver` is an async callable. After the client connects, `create()` calls it with the client and the database, and it returns that id. Without either it raises `ValueError`.

The lifecycle is:

1. `await backup.connect()`
2. `await backup.backup_all()`
3. `await backup.disconnect()`
4. `await backup.db.close()`

`disconnect()` only disconnects a client the instance created. A client passed in stays connected.

### The viewer as an ASGI app

`telegram_archive.web.main:app` is a standard ASGI application. It builds `Config` when it is imported, so an invalid value stops it from starting. Until viewer access is configured, it answers requests with 503. See [Logins, viewer accounts and share links](../viewer/access.md).

The HTTP routes are documented in [HTTP API](api.md). A running viewer also serves the machine-readable route list at `/openapi.json`.
