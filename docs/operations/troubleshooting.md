# Monitoring and troubleshooting

This page covers health checks, logs, and a table of known symptoms with their fixes.

## Health checks

Both images declare a Docker health check. Read the result with:

```bash
docker compose ps
```

### Backup container

The backup container is healthy while its heartbeat file is younger than `HEARTBEAT_MAX_AGE_SECONDS` (180 seconds). The file is `HEARTBEAT_FILE`, by default `/tmp/telegram-archive.heartbeat`. A missing file counts as unhealthy.

Only the `schedule` command writes the heartbeat. It writes it every 30 seconds and starts before it connects to Telegram. A long first backup therefore stays healthy. Docker runs the check every 60 seconds, with a 300 second start period and 3 retries.

!!! warning "Healthy means alive, not working"
    The heartbeat proves that the scheduler's event loop runs. It does not prove that Telegram is reachable, that the database works or that a backup succeeded. A container can be healthy while every run fails. To see whether backups succeed, read the logs and the [Archive Status panel](#archive-status-panel).

Two setups read as unhealthy even when nothing is wrong:

- A container that runs the one-shot `backup` command instead of `schedule` writes no heartbeat. Docker marks it unhealthy about three minutes after the 300-second start period ends, once three checks in a row have failed.
- A read-only root filesystem without a tmpfs at `/tmp` leaves the scheduler nowhere to write the heartbeat. The container stays unhealthy for good. The backup log then repeats `Could not write heartbeat: PermissionError`, or another OSError name, every 30 seconds. The stock compose file mounts that tmpfs.

### Viewer container

The viewer checks `HEALTHCHECK_URL`, by default `http://127.0.0.1:8000/api/health`, every 30 seconds. The endpoint needs no login. It answers `{"status":"ok","database":"connected"}`, or HTTP 503 with `{"status":"degraded","database":"unreachable"}` when a database query fails.

From the host running the stock compose file:

```bash
curl http://127.0.0.1:8000/api/health
```

### Check from a script

`/api/health` needs no login and only says whether the database answers. The master's Archive Status panel reads `/api/status`, which a script can call with the master's session cookie:

```bash
curl -fsS -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"choose-a-long-password"}' \
  http://127.0.0.1:8000/api/login
curl -fsS -b cookies.txt http://127.0.0.1:8000/api/status
```

The answer holds `backup.last_run` and `backup.in_progress`, `stats_calculated_at`, one entry per account in `listeners` with `active` and `active_since`, the media counts under `media`, and `database.backend` and `database.size_bytes`. It carries no chat ids, titles or text. An alert on `backup.last_run` older than two schedule ticks catches a backup that stopped working while the container stays healthy.

Log in once and keep the cookie. Each login opens a session, a user holds at most 10, and an 11th login ends the oldest, so a script that logs in on every check logs you out. Logins are also limited to 15 per client address in 300 seconds.

## Logs

Follow the backup:

```bash
docker compose logs -f telegram-backup
```

Follow the viewer:

```bash
docker compose logs -f telegram-viewer
```

The stock compose file caps each container's log at 10 MB, in 3 rotated files.

### Log level

`LOG_LEVEL` sets the level for the backup, the scheduler and every CLI command. The default is `INFO`.

| Value | Shows |
|-------|-------|
| `DEBUG` | Everything, including per-step detail |
| `INFO` | Progress, run summaries and warnings |
| `WARNING` | Warnings and errors. `WARN` is accepted too. |
| `ERROR` | Errors only |
| `CRITICAL` | Fatal errors only |

An unknown value silently becomes `INFO`. The viewer ignores `LOG_LEVEL` and always logs at `INFO`. The Telethon and httpx loggers are always set to `WARNING`, so request URLs never reach the log.

`DB_ECHO=true` logs every SQL statement. Only the word `true`, in any case, turns it on; `1`, `yes` and `on` do not.

### What the logs contain

The backup's logs never contain chat ids, message text, account names or phone numbers. Set `LOG_CHAT_TITLES=true` to add a cleaned chat title to the two lines that report progress through each chat:

```text
[3/40] Backing up
  [Archived 1/5]
```

The title is appended to the end of each line. A private chat is then shown as `private chat`, not by name.

`FLOOD_WAIT_LOG_THRESHOLD` hides short Telegram rate-limit pauses, called FloodWaits. The default is 10 seconds. While fetching message history, the backup does not log the first FloodWait shorter than this. Set it to `0` to log every one.

### Specific log lines

Other pages document the lines their features write:

- The transcription drain summary: [Voice transcription](../configuration/transcription.md#troubleshooting).
- The webhook failure line: [Event webhook](../configuration/event-webhook.md#delivery).
- The line the viewer logs when it rejects an update from the backup: [Live updates and notifications](../viewer/live-updates.md#sqlite).

## Other signals to watch

### Archive Status panel

The master login's **Archive Status** panel shows the last backup run, each listener, the media pipeline, stats freshness, transcription and the database. See [Using the viewer](../viewer/using-the-viewer.md#archive-status).

A growing `gave up` count means files failed `MEDIA_MAX_DOWNLOAD_ATTEMPTS` times, 5 by default. The backup also logs this warning at the end of each run:

```text
N media file(s) permanently skipped after 5 failed download attempts (raise MEDIA_MAX_DOWNLOAD_ATTEMPTS to retry them)
```

### Messages that failed

When the backup cannot process a message, it stops the chat's progress marker there and retries the message on the next run. After two failed runs, it moves past the message. It records the id in the `metadata` table under the key `message_failures_<chat_id>`. For accounts other than the first, the key ends in `_account_<id>`. The record keeps the newest 500 ids and an exact total. The ids never go to the logs.

### Statistics

```bash
docker compose exec telegram-backup python -m telegram_archive stats
```

On the 8.16.1 images run it as `python -m src stats`. See the `No module named telegram_archive` row below.

This prints cached figures. They are all zero until the first backup has completed.

## Troubleshooting table

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Permission denied` writing to `/data` | Both containers run as uid 1000 and cannot write a directory another user owns. See [Run with Docker](../getting-started/docker.md). | `sudo chown -R 1000:1000 data`. `chmod 755` does not help. `init_auth.sh` creates `data/backups` as your host user, so run the `chown` after it if your uid is not 1000. |
| The backup container restarts over and over after printing help | The image's default command has no subcommand, so it prints help and exits. | Pass a command, normally `schedule`, as the stock compose file does. |
| `Session not authorized` | Telegram no longer accepts the session. See [Log in to Telegram](../getting-started/telegram-login.md#log-in-again). | Stop the backup service, run `auth` again, start the service. |
| `No module named telegram_archive` | The 8.16.1 images know the module only as `src`. See [Upgrading](../operations/upgrading.md#module-rename). | Run the same command with `python -m src`. |
| The viewer answers 503 `Viewer authentication is not configured` | No login method is set, so the viewer serves no data by design. See [Logins, viewer accounts and share links](../viewer/access.md). | Set `VIEWER_USERNAME` and `VIEWER_PASSWORD`, or `AUTH_PROXY_HEADER`, or `ALLOW_ANONYMOUS_VIEWER=true`, in the viewer's environment. |
| The viewer shows no chats while the backup has data | The two containers resolve different databases, usually because `DATABASE_DIR` or `DATABASE_PATH` is set only in `.env`. See [SQLite and PostgreSQL](../configuration/database.md). | Use `DB_PATH`, or add the same variable to the viewer's `environment:` block. Both containers need identical database settings. |
| The viewer shows errors right after an upgrade, or on an empty PostgreSQL database | The viewer never migrates, and the backup has not finished migrating yet. See [Upgrading](../operations/upgrading.md). | Start the backup container and wait. Check with `docker compose exec telegram-backup alembic current`. |
| A variable set in `.env` has no effect on the viewer | The viewer service has no `env_file` and only receives an allowlist of variables. See [Run with Docker](../getting-started/docker.md#what-the-stock-compose-file-does). | Add the variable to the viewer's `environment:` block in `docker-compose.yml`. |
| `ERROR: unrecognised database configuration` at start | `DB_TYPE` is not `sqlite`, `postgresql` or `postgres`, or `DATABASE_URL` uses an unsupported scheme such as `postgresql+psycopg2://`. See [SQLite and PostgreSQL](../configuration/database.md). | Fix the value. The container refuses to start until you do. |
| `table chats already exists` when running `migrate` after a pip install | Another command created the schema before `migrate` ran, without recording a schema version. See [Install from PyPI](../getting-started/pip.md#first-run). | If the archive holds nothing yet, delete the database file and run `telegram-archive migrate` before any other command. |
| `database is locked` on a slow disk | SQLite waits `DATABASE_TIMEOUT` seconds (60) for a lock, then gives up. See [SQLite and PostgreSQL](../configuration/database.md). | Raise `DATABASE_TIMEOUT` in both containers. It has no effect on PostgreSQL. |
| Startup fails with `invalid literal for int()` | A chat id list, such as `CHAT_IDS` or any `*_CHAT_IDS`, contains something that is not an integer. The error does not name the variable. See [Choosing chats](../configuration/choosing-chats.md#chat-ids). | Check every chat id list for stray text, quotes or spaces between commas. |
| Startup fails with `Invalid boolean value for X` | Variable `X` holds a word the parser does not accept. | Use `true`, `false`, `1`, `0`, `yes`, `no`, `on` or `off`. |
| The viewer crashes at start | `AUTH_SESSION_DAYS`, `MAX_WS_CONNECTIONS` or `MAX_WS_SUBSCRIPTIONS_PER_CONNECTION` is not an integer, or `DELETION_MODE` or a `MASS_OPERATION_*` value is invalid. See [Environment variables](../reference/environment-variables.md). | Read the traceback in the viewer log and fix the value. `DELETION_MODE` must be `soft` or `hard`. `MASS_OPERATION_THRESHOLD` and `MASS_OPERATION_WINDOW_SECONDS` must be integers of at least 1. `MASS_OPERATION_BUFFER_DELAY` must be a number. |
| Bot chats are missing | `bots` is not in the default `CHAT_TYPES`. See [Choosing chats](../configuration/choosing-chats.md#what-is-captured-by-default). | Add `bots` to `CHAT_TYPES`. |
| Chats disappeared from new backups after adding an include list | Include lists are allow-lists: they restrict capture to the listed chats. See [Choosing chats](../configuration/choosing-chats.md#type-based-mode). | List every chat you want, or use per-type include lists together with `CHAT_TYPES`. |
| Media is missing in the viewer | The viewer shows the reason: over the size limit, excluded by the media filter, not available for this login, or not downloaded yet. See [Media downloads](../configuration/media.md#why-media-is-missing-in-the-viewer). | Relax the limit or filter, or wait for the next backup. |
| Media turned back on is not fetched for older messages | With `DOWNLOAD_MEDIA=false` or `SKIP_MEDIA_CHAT_IDS`, archived messages have no media record, and the cursor has moved past them. See [Media downloads](../configuration/media.md#reversible-or-not). | Only new messages get media. To keep the option of fetching later, narrow media with the type, MIME or size filters instead. |
| Video thumbnails are missing on a native install | Video thumbnails need `ffmpeg`. Without it, thumbnails are skipped and no error appears. See [Install from PyPI](../getting-started/pip.md#system-tools). | Install `ffmpeg` so it is on the `PATH`. |
| Times are shown in the wrong zone | The viewer uses `VIEWER_TIMEZONE`, default `Europe/Madrid`. An unknown name falls back to UTC with a warning. See [Using the viewer](../viewer/using-the-viewer.md). | Set `VIEWER_TIMEZONE` to a tz database name, such as `America/New_York`. |
| Backups run at an unexpected hour | `SCHEDULE` runs in the container's time zone, which is UTC unless you set `TZ`. `VIEWER_TIMEZONE` does not affect it. See [Schedule and backup tuning](../configuration/schedule.md). | Write the cron expression in UTC, or set `TZ` on the backup service, for example `TZ=Europe/Berlin`. |
| The container is healthy but nothing is backed up | The heartbeat only proves the process is alive. See [Backup container](#backup-container). | Read `docker compose logs telegram-backup` and the [Archive Status panel](#archive-status-panel). |
| Long `FloodWait` pauses, and some chats are only finished on the next run | Telegram rate limits. A wait longer than `MAX_FLOOD_WAIT_SECONDS` (3600) is not waited out, and that chat or file is retried next run. See [Your first backup](../getting-started/first-backup.md#rate-limits-are-normal). | Nothing to fix. Let the runs catch up. |
| New messages only appear after a backup run, or live updates never arrive | The listener is off. Or, on SQLite, the backup cannot send updates to the viewer, because `VIEWER_PORT` defaults to 8080 outside compose or the containers do not share the `.push-secret` file. When the viewer is unreachable, the backup log shows `HTTP notification failed`. When an update arrives without a valid secret, the viewer log shows `Rejected /internal/push`. See [Live updates and notifications](../viewer/live-updates.md#sqlite). | Set `ENABLE_LISTENER=true`. Outside compose, set `VIEWER_HOST` and `VIEWER_PORT=8000`. Mount the same database directory in both containers, or set `INTERNAL_PUSH_SECRET` on both. |
| No notifications | Notifications need the listener. The default `basic` mode only notifies for the open chat in a hidden tab. `full` needs the viewer served over HTTPS. See [Live updates and notifications](../viewer/live-updates.md#notification-modes). | Turn on the listener, then set `PUSH_NOTIFICATIONS=full` behind HTTPS for notifications with the browser closed. |
| The event webhook never fires | The webhook needs the listener, and each event needs its `LISTEN_*` switch. Startup logs a warning such as `EVENT_WEBHOOK_ENABLED has no effect: ENABLE_LISTENER=false`. See [Event webhook](../configuration/event-webhook.md#prerequisites). | Read the startup warnings and fix what they name. A bad `EVENT_WEBHOOK_*` value also turns the webhook off with one warning. |
| Transcripts stay queued | Pressing Transcribe in the viewer only queues the file. The backup sends queued files at the end of each successful run. It stops when the server is unreachable or rejects a request. A 404 often means `TRANSCRIPTION_URL` ends in `/v1`. See [Voice transcription](../configuration/transcription.md#troubleshooting). | Wait for the next backup, check the drain summary line, and remove `/v1` from the URL. |
| A share link still works after its expiry | Expiry only blocks new logins with the token. Sessions it already created stay valid for `AUTH_SESSION_DAYS`. See [Logins, viewer accounts and share links](../viewer/access.md). | Revoke or delete the token. That ends every session it created. |
| The login rate limit locks out everyone behind a reverse proxy | Without `TRUST_PROXY_HEADERS=true`, every client shares the proxy's address and its 15 attempts per 5 minutes. See [Exposing the viewer safely](../viewer/exposing.md). | Set `TRUST_PROXY_HEADERS=true`, but only if the proxy overwrites `X-Forwarded-For`. |
| The sidebar message search finds nothing and says it needs the full-text index | The backup has not started once on 8.5 or later, so migration 028 has not built the index. Without the index, the sidebar search answers nothing, and the search inside a chat falls back to plain text matching on message text only, without transcripts. | Start the backup container once and wait for it to migrate. |
| Search stays without the full-text index after the backup has migrated | The SQLite build has no FTS5. The sidebar search answers nothing, and the search inside a chat falls back to plain text matching on message text only, without transcripts. See [SQLite and PostgreSQL](../configuration/database.md). | Use a SQLite build with FTS5, or PostgreSQL. |
| `Media not found` in an open tab after `reclassify-round-videos` or an upgrade | The tab holds old media addresses. See [Media downloads](../configuration/media.md#round-videos-in-older-archives). | Reload the page. |
| An excluded chat still shows in the viewer | Excluded chats keep their archived data by default. See [Choosing chats](../configuration/choosing-chats.md#changing-filters-later). | Set `EXCLUDE_DELETE_EXISTING=true` to delete it on the next run. This cannot be undone. |
| Telegram logged the account out after a one-off command | A second process used the same session file while `schedule` ran. See [Log in to Telegram](../getting-started/telegram-login.md#one-client-per-session). | Log in again. From then on, stop the backup service before running commands that connect to Telegram. |
| `duplicate key value violates unique constraint "reactions_pkey"` on PostgreSQL | The reactions id sequence is out of step with the table, often after a restore. | The backup resets the sequence and retries on its own. If the error persists, run the manual fix below. |

### Manual reactions sequence fix

Run this against the PostgreSQL container from the stock compose file:

```bash
docker exec -i telegram-postgres psql -U telegram -d telegram_backup \
  -c "SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false);"
```

## Reporting a bug

Open a bug report at [github.com/GeiserX/Telegram-Archive/issues](https://github.com/GeiserX/Telegram-Archive/issues). Blank issues are off. The bug form asks for the version, how you run it, the database type, what happened, what you expected, the steps to reproduce and the logs. Questions and feature requests have their own forms. Have these ready:

- The version tag of both images, as pinned in `docker-compose.yml` or shown by `docker compose images`. For a pip install, the output of `pip show telegram-archive`.
- The log lines around the failure, from `docker compose logs telegram-backup` or `telegram-viewer`.
- Whether you run SQLite or PostgreSQL.

The logs carry no chat content by design, so you can paste them as they are. If you turned on `LOG_CHAT_TITLES`, remove the titles first.

An issue with no activity for 14 days is marked stale and closed 14 days later. Reply to it to keep it open.

A security problem is not a bug report. Do not open an issue for it. See [Reporting a vulnerability](../viewer/exposing.md#reporting-a-vulnerability).
