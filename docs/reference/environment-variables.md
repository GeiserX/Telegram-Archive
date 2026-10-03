# Environment variables

This page lists every setting Telegram Archive reads. Each row gives the default, which process reads it, how the value is checked, and what a bad value does. The feature pages explain what the settings are for. This page is the only one that states every default.

## How settings are read {#how-settings-are-read}

All configuration comes from environment variables. There is no configuration file.

At startup the program also loads a `.env` file. It starts in the directory of the installed `telegram_archive` package and walks up through its parents. It never looks in the working directory. In a source checkout this finds the checkout's `.env`. In a pip install it only finds a `.env` in a parent of `site-packages`, such as a project folder that holds the virtualenv. Otherwise, export the variables in the shell. A variable that is already set in the environment always wins over the same name in `.env`.

Under the stock `docker-compose.yml`:

- The backup container receives the whole `.env` through `env_file`. Its `environment:` block wins when both set the same name.
- The viewer container has no `env_file`. It receives only the variables listed in its own `environment:` block. [Run with Docker](../getting-started/docker.md) has that list. To give the viewer anything else, add the variable to its block.

The viewer builds the same settings object as the backup. A shared check that fails stops the viewer too. A viewer that receives a bad `DELETION_MODE` or a bad `MASS_OPERATION_*` value refuses to start, even though it never uses them.

### Parsing rules

- Booleans accept `1`, `true`, `yes`, `on` and `0`, `false`, `no`, `off`, in any case, with surrounding spaces ignored. Empty or unset means the default. Anything else stops startup with `Invalid boolean value for <NAME>`.
- Three booleans are stricter. `ALLOW_ANONYMOUS_VIEWER`, `TRUST_PROXY_HEADERS` and `DB_ECHO` treat only the word `true`, in any case, as true. `1` or `yes` silently mean false.
- For integers and decimals, empty or unset means the default. A value that is not a number stops startup with an error that names the variable. `nan` and `inf` are rejected, except by `BACKOFF_MIN_SECONDS` and `BACKOFF_MAX_SECONDS`, which accept them.
- Chat id lists are comma-separated integers. Use the marked form that Telegram uses for groups and channels, such as `-1001234567890`. A non-integer entry stops startup with Python's generic `invalid literal for int()` error, which does not say which variable is wrong. When a backup starts, it looks at filter ids written without the `-100` prefix. If the marked chat is already archived, it uses the marked id instead.
- A FloodWait is Telegram telling the client to pause before its next request.
- Empty id lists count as unset. A chat id list set to an empty string behaves as if it were not set. `CHAT_TYPES` is the exception: an explicitly empty value means no types. To clear a filter for one account in a multi-account setup, set its per-account override to `none`.

### What happens on a bad value

| Outcome | Settings |
|---|---|
| Startup stops, and the error names the variable | Most booleans, integers and decimals<br>`SKIP_TOPIC_IDS`, the proxy settings, the `TG_ACCOUNT_<N>` credentials and `TG_ACCOUNT_<N>_CHAT_TYPES`<br>`DELETION_MODE` and `MASS_OPERATION_*`, in the viewer too and even with the listener off<br>`TRANSCRIPTION_MAX_SECONDS`, `TRANSCRIPTION_MAX_UPLOAD_MB`, `TRANSCRIPTION_BACKFILL_PER_RUN`, `TRANSCRIPTION_ASK_RATE_LIMIT` and `TRANSCRIPTION_ASK_MAX_OPEN`, while transcription is on |
| Startup stops, and the error shows the bad value but not the variable | `CHAT_TYPES`, `DOWNLOAD_MEDIA_TYPES`, `DOWNLOAD_DOCUMENT_MIME_TYPES`. |
| Startup stops with a generic error | A non-integer entry in any chat or folder id list, including the `TG_ACCOUNT_<N>_*` id overrides and `TRANSCRIPTION_PRIORITY_CHAT_IDS`. |
| The viewer crashes as it starts | `AUTH_SESSION_DAYS`, `MAX_WS_CONNECTIONS`, `MAX_WS_SUBSCRIPTIONS_PER_CONNECTION`. |
| A warning is logged and the default is used | `MAX_FLOOD_RETRIES`, `MAX_FLOOD_WAIT_SECONDS`, `BACKOFF_MIN_SECONDS`, `BACKOFF_MAX_SECONDS`, `FLOOD_WAIT_LOG_THRESHOLD`, `MEDIA_REFRESH_MAX_ATTEMPTS`, `MEDIA_REFRESH_TIMEOUT_SECONDS`. `VIEWER_TIMEZONE` becomes `UTC`. `STATS_CALCULATION_HOUR` becomes `3`. |
| The value silently falls back | `LOG_LEVEL` becomes `INFO`. `PUSH_NOTIFICATIONS` becomes `basic`, and it is not trimmed, so a stray space counts as a bad value. `PARALLEL_DOWNLOAD_PART_SIZE_KB` snaps to a valid size. `DATABASE_TIMEOUT` becomes 60 seconds. |
| A warning is logged and the feature turns off or falls back to a default | Every `EVENT_WEBHOOK_*` setting except `EVENT_WEBHOOK_ENABLED` and `EVENT_WEBHOOK_BODY_TEMPLATE`. `TRANSCRIPTION_URL`, `TRANSCRIPTION_PRESET`, `TRANSCRIPTION_PROVIDER`, `TRANSCRIPTION_TYPES`, `TRANSCRIPTION_WEBHOOK_SECRET` and `TRANSCRIPTION_CALLBACK_URL`. `VIEWER_CHAT_BACKGROUND`. |

### Applying a change

Settings are read once, when a process starts. Under compose, edit `.env` and run:

```bash
docker compose up -d
```

Compose recreates every container whose configuration changed. `docker compose restart` does not read `.env` again. For a pip install, stop and start the process.

## Telegram credentials {#telegram-credentials}

Feature page: [Log in to Telegram](../getting-started/telegram-login.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="telegram_api_id"></span>`TELEGRAM_API_ID` | unset | backup | Integer API id from my.telegram.org. Required in single-account mode. Ignored once any `TG_ACCOUNT_<N>` credential variable is set. A non-numeric value stops startup in single-account and [indexed](#multiple-accounts) mode. |
| <span id="telegram_api_hash"></span>`TELEGRAM_API_HASH` | unset | backup | API hash from my.telegram.org. Required in single-account mode. Ignored in [indexed mode](#multiple-accounts). |
| <span id="telegram_phone"></span>`TELEGRAM_PHONE` | unset | backup | Phone number with country code. Required in single-account mode. Ignored in indexed mode. |
| <span id="session_name"></span>`SESSION_NAME` | `telegram_backup` | backup | Session file name inside `SESSION_DIR` for the single account. Also the fallback name for account 1 in indexed mode. |
| <span id="session_dir"></span>`SESSION_DIR` | `session` beside `BACKUP_PATH`, so `/data/session` | backup | Directory for session files. Made absolute and created at startup. `--data-dir PATH` sets it to `PATH/session`. |
| <span id="telegram_device_model"></span>`TELEGRAM_DEVICE_MODEL` | `Telegram Archive` | backup | Device name this install shows in Telegram under Settings, Devices. One name for every account of the install. A blank value uses the default. Give each install its own name to tell them apart. |

## Multiple accounts {#multiple-accounts}

Feature page: [Multiple accounts](../configuration/multiple-accounts.md).

Any non-empty `TG_ACCOUNT_<N>_API_ID`, `_API_HASH`, `_PHONE_NUMBER`, `_LABEL` or `_SESSION_NAME` switches to indexed mode. `N` starts at 1, has no leading zeros and must be contiguous. A `TG_ACCOUNT_` variable with an unknown suffix stops startup. Errors name the variable. A credential or phone number is never echoed; an invalid `TG_ACCOUNT_<N>_CHAT_TYPES` entry is.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="tg_account_n_api_id"></span>`TG_ACCOUNT_<N>_API_ID` | unset | backup | Integer API id of account N. Required for each declared account. A non-integer stops startup. |
| <span id="tg_account_n_api_hash"></span>`TG_ACCOUNT_<N>_API_HASH` | unset | backup | API hash of account N. Required for each declared account. |
| <span id="tg_account_n_phone_number"></span>`TG_ACCOUNT_<N>_PHONE_NUMBER` | unset | backup | Phone number of account N. Required. Two accounts with the same number stop startup. |
| <span id="tg_account_n_label"></span>`TG_ACCOUNT_<N>_LABEL` | `default` for account 1, `account<N>` for the others | backup | Name shown on the account tags in the viewer. |
| <span id="tg_account_n_session_name"></span>`TG_ACCOUNT_<N>_SESSION_NAME` | account 1: `SESSION_NAME`, then `telegram_backup`<br>account 2 and up: `telegram_backup_account<N>` | backup | Session file name of account N. Two accounts resolving to the same name stop startup. |
| <span id="tg_account_n_filter"></span>`TG_ACCOUNT_<N>_<FILTER>` | unset, inherits the global filter | backup | Overrides one filter for account N. Empty inherits the global value. `none`, in any case, means explicitly empty. `TG_ACCOUNT_<N>_INCLUDE_CHAT_IDS` and `_EXCLUDE_CHAT_IDS` replace the global include and exclude lists for that account. An override never switches to indexed mode by itself, and one for an account that does not exist stops startup. With several accounts, an unprefixed `*_INCLUDE_FOLDER_IDS` that two or more accounts would inherit stops startup, because folder ids are numbered per account. |

`<FILTER>` can be any of these:

- `CHAT_IDS`
- `CHAT_TYPES`
- `INCLUDE_CHAT_IDS` and `EXCLUDE_CHAT_IDS`
- the `PRIVATE_`, `GROUPS_` and `CHANNELS_` include and exclude lists
- `PRIORITY_CHAT_IDS`
- `SKIP_MEDIA_CHAT_IDS`
- `INCLUDE_FOLDER_IDS`
- `PRIVATE_INCLUDE_FOLDER_IDS`, `GROUPS_INCLUDE_FOLDER_IDS` and `CHANNELS_INCLUDE_FOLDER_IDS`

## Proxy {#proxy}

Feature page: [Log in to Telegram](../getting-started/telegram-login.md).

Setting any of `TELEGRAM_PROXY_TYPE`, `_ADDR`, `_PORT`, `_USERNAME`, `_PASSWORD` or `_SECRET` turns the proxy on for every Telegram connection. `TYPE`, `ADDR` and `PORT` are then required.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="telegram_proxy_type"></span>`TELEGRAM_PROXY_TYPE` | unset | backup | `socks5` or `mtproxy`, in any case. |
| <span id="telegram_proxy_addr"></span>`TELEGRAM_PROXY_ADDR` | unset | backup | Proxy host name or IP address. |
| <span id="telegram_proxy_port"></span>`TELEGRAM_PROXY_PORT` | unset | backup | Integer from 1 to 65535. |
| <span id="telegram_proxy_username"></span>`TELEGRAM_PROXY_USERNAME` | unset | backup | SOCKS5 only. Must be set together with `TELEGRAM_PROXY_PASSWORD`; invalid for MTProxy. |
| <span id="telegram_proxy_password"></span>`TELEGRAM_PROXY_PASSWORD` | unset | backup | SOCKS5 only. Must be set together with `TELEGRAM_PROXY_USERNAME`; invalid for MTProxy. |
| <span id="telegram_proxy_secret"></span>`TELEGRAM_PROXY_SECRET` | unset | backup | Required for MTProxy; invalid for SOCKS5. Must be 32 hexadecimal characters, optionally prefixed with `dd`; a FakeTLS `ee` secret stops startup. Keep it in a protected `.env`. |
| <span id="telegram_proxy_rdns"></span>`TELEGRAM_PROXY_RDNS` | `false` | backup | SOCKS5 host name resolution through the proxy. It does not enable a proxy on its own; MTProxy accepts only unset or false. |

## Schedule and paths {#schedule-and-paths}

Feature page: [Schedule and backup tuning](../configuration/schedule.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="schedule"></span>`SCHEDULE` | `0 */6 * * *` | backup | Five cron fields: minute, hour, day, month, day of week. Any other field count stops the scheduler. Evaluated in the process's local time zone, which is UTC in the images unless you set `TZ`. `VIEWER_TIMEZONE` does not apply. The `schedule` command also runs one backup as soon as it starts. |
| <span id="backup_path"></span>`BACKUP_PATH` | `/data/backups`<br>compose: fixed at `/data/backups` | both | Archive root. Media goes to `BACKUP_PATH/media` and the default SQLite file is `BACKUP_PATH/telegram_backup.db`. The stock compose sets it in both services' `environment:` block, so a value in `.env` is ignored there. `--data-dir PATH` sets it to `PATH/backups`. |

## Chat filters {#chat-filters}

Feature page: [Choosing chats](../configuration/choosing-chats.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="chat_ids"></span>`CHAT_IDS` | empty | backup | [Whitelist mode](../configuration/choosing-chats.md). When non-empty, the backup saves only these chats and ignores every other filter, including folder filters. `PRIORITY_CHAT_IDS` and `SKIP_MEDIA_CHAT_IDS` still apply. |
| <span id="whitelist_resolve_dialog_limit"></span>`WHITELIST_RESOLVE_DIALOG_LIMIT` | `1000` | backup | If the backup cannot find a `CHAT_IDS` entry, it scans up to this many chats in your chat list once, for at most 300 seconds, then tries the entry again. `0` turns the scan off. |
| <span id="chat_types"></span>`CHAT_TYPES` | `private,groups,channels` | backup | Types backed up in type-based mode: `private`, `groups`, `channels`, `bots`. Bots are not in the default. An explicitly empty value means no types, so only include lists admit chats. The stock compose turns an empty value into the default. An unknown type stops startup. |
| <span id="global_include_chat_ids"></span>`GLOBAL_INCLUDE_CHAT_IDS` | empty | backup | When set, only these chats of any type, plus the members of `GLOBAL_INCLUDE_FOLDER_IDS`, are backed up. `CHAT_TYPES` and the per-type include lists are then ignored. |
| <span id="global_exclude_chat_ids"></span>`GLOBAL_EXCLUDE_CHAT_IDS` | empty | backup | Never back up these chats. Checked before every other rule. |
| <span id="include_chat_ids"></span>`INCLUDE_CHAT_IDS` | empty | backup | Deprecated. Old name of `GLOBAL_INCLUDE_CHAT_IDS`, read when the new name is unset or empty. |
| <span id="exclude_chat_ids"></span>`EXCLUDE_CHAT_IDS` | empty | backup | Deprecated. Old name of `GLOBAL_EXCLUDE_CHAT_IDS`, read when the new name is unset or empty. |
| <span id="private_include_chat_ids"></span>`PRIVATE_INCLUDE_CHAT_IDS` | empty | backup | Allow-list for private chats and bots. When set, only these private chats are backed up, even if `private` is missing from `CHAT_TYPES`. |
| <span id="private_exclude_chat_ids"></span>`PRIVATE_EXCLUDE_CHAT_IDS` | empty | backup | Exclude list for private chats and bots. |
| <span id="groups_include_chat_ids"></span>`GROUPS_INCLUDE_CHAT_IDS` | empty | backup | Allow-list for groups and supergroups. |
| <span id="groups_exclude_chat_ids"></span>`GROUPS_EXCLUDE_CHAT_IDS` | empty | backup | Exclude list for groups and supergroups. |
| <span id="channels_include_chat_ids"></span>`CHANNELS_INCLUDE_CHAT_IDS` | empty | backup | Allow-list for channels. |
| <span id="channels_exclude_chat_ids"></span>`CHANNELS_EXCLUDE_CHAT_IDS` | empty | backup | Exclude list for channels. |
| <span id="exclude_delete_existing"></span>`EXCLUDE_DELETE_EXISTING` | `false` | backup | Delete what the archive already holds for chats in any exclude list: rows, the chat's media folder and its avatars. The media folder stays while another account still archives the chat. Files in `media/_shared` stay. Cannot be undone. Has no effect in whitelist mode. |
| <span id="global_include_folder_ids"></span>`GLOBAL_INCLUDE_FOLDER_IDS` | empty | backup | Telegram folder ids. The chats listed in those folders are included the same way as `GLOBAL_INCLUDE_CHAT_IDS`. Membership is refreshed every run. Ignored in whitelist mode. |
| <span id="include_folder_ids"></span>`INCLUDE_FOLDER_IDS` | empty | backup | Deprecated. Old name of `GLOBAL_INCLUDE_FOLDER_IDS`, read when the new name is unset or empty. |
| <span id="private_include_folder_ids"></span>`PRIVATE_INCLUDE_FOLDER_IDS` | empty | backup | Folder-based allow-list for private chats and bots. |
| <span id="groups_include_folder_ids"></span>`GROUPS_INCLUDE_FOLDER_IDS` | empty | backup | Folder-based allow-list for groups. |
| <span id="channels_include_folder_ids"></span>`CHANNELS_INCLUDE_FOLDER_IDS` | empty | backup | Folder-based allow-list for channels. |
| <span id="priority_chat_ids"></span>`PRIORITY_CHAT_IDS` | empty | backup | These chats are backed up first. |
| <span id="skip_topic_ids"></span>`SKIP_TOPIC_IDS` | empty | backup | Forum topics to skip, as comma-separated `chat_id:topic_id` pairs. Topic `1` is the General topic. A malformed entry stops startup and names the entry. |
| <span id="follow_chat_migrations"></span>`FOLLOW_CHAT_MIGRATIONS` | `false` | backup | When a tracked basic group becomes a supergroup, back up the new supergroup too. When this is false, the backup only logs a warning. |

## Media {#media}

Feature page: [Media downloads](../configuration/media.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="download_media"></span>`DOWNLOAD_MEDIA` | `true` | backup | Download media files at all. When false, messages are stored with no media rows. |
| <span id="max_media_size_mb"></span>`MAX_MEDIA_SIZE_MB` | `100` | backup | Skip files larger than this. `0` or a negative value means no limit. Skipped files keep a row and are fetched on a later run if you raise the limit. |
| <span id="download_media_types"></span>`DOWNLOAD_MEDIA_TYPES` | empty, every type | backup | Comma-separated allow-list: `photo`, `video`, `video_note`, `animation`, `voice`, `audio`, `sticker`, `document`, `webpage`. An unknown type stops startup. Applies to the scheduled backup and the listener. |
| <span id="download_document_mime_types"></span>`DOWNLOAD_DOCUMENT_MIME_TYPES` | empty, every document | backup | Narrows `document` to full `type/subtype` MIME types. A document passes on an exact MIME match or on a file extension that belongs to one of the listed types. Wildcards and bare extensions stop startup. |
| <span id="download_chat_description"></span>`DOWNLOAD_CHAT_DESCRIPTION` | `false` | backup | Fetch each chat's description or bio and member count every run, at one extra request per chat. |
| <span id="download_timeout_seconds"></span>`DOWNLOAD_TIMEOUT_SECONDS` | `3600` | backup | Time limit for one download attempt. `0` turns the limit off. Time spent waiting out a FloodWait counts toward this limit. |
| <span id="media_flood_sleep_threshold"></span>`MEDIA_FLOOD_SLEEP_THRESHOLD` | `60` | backup | The backup and the listener wait out a FloodWait of up to this many seconds during a media transfer. `0` fails at once. |
| <span id="media_max_filename_bytes"></span>`MEDIA_MAX_FILENAME_BYTES` | `143` | backup | Byte budget for a stored media file name. The file id prefix and the extension are always kept. The `import` command uses it too. |
| <span id="media_max_download_attempts"></span>`MEDIA_MAX_DOWNLOAD_ATTEMPTS` | `5` | both | The retry pass gives up on a file after this many failed attempts. The viewer reads it only to split waiting from given-up files in Archive status. The stock compose does not pass it to the viewer. |
| <span id="media_refresh_max_attempts"></span>`MEDIA_REFRESH_MAX_ATTEMPTS` | `3` | backup | Total attempts per file within one run, the first included. Covers expired file references, location errors and timeouts, and a download that ended short of the size Telegram declares for the file (see [A file cut short](../configuration/media.md#a-file-cut-short)). |
| <span id="media_refresh_timeout_seconds"></span>`MEDIA_REFRESH_TIMEOUT_SECONDS` | `120` | backup | Time limit for re-fetching one message to refresh its file reference. |
| <span id="parallel_download_enabled"></span>`PARALLEL_DOWNLOAD_ENABLED` | `false` | backup | Download large files over several connections. The scheduled backup only, never the listener. |
| <span id="parallel_download_min_size_mb"></span>`PARALLEL_DOWNLOAD_MIN_SIZE_MB` | `20` | backup | Smallest file that takes the parallel path. Floored at 1. |
| <span id="parallel_download_connections"></span>`PARALLEL_DOWNLOAD_CONNECTIONS` | `4` | backup | Connections per file. Clamped to 2 through 8. |
| <span id="parallel_download_part_size_kb"></span>`PARALLEL_DOWNLOAD_PART_SIZE_KB` | `512` | backup | Chunk size: 4, 8, 16, 32, 64, 128, 256 or 512. A non-integer becomes 512. Other integers snap down to the largest valid size below them, and anything under 4 becomes 4. Never stops startup. |
| <span id="deduplicate_media"></span>`DEDUPLICATE_MEDIA` | `true` | backup | Store each file once under `media/_shared` and link to it from every chat folder that holds it. |
| <span id="skip_media_chat_ids"></span>`SKIP_MEDIA_CHAT_IDS` | empty | backup | Chats whose media is not downloaded. Their messages are still archived. Removing a chat from the list later does not fetch media for messages already archived. |
| <span id="skip_media_delete_existing"></span>`SKIP_MEDIA_DELETE_EXISTING` | `false` | backup | Also delete the media already archived for `SKIP_MEDIA_CHAT_IDS` chats, earlier media that edits replaced included. A file another account's row still names stays, and so do the files in `media/_shared`. |
| <span id="download_youtube_videos"></span>`DOWNLOAD_YOUTUBE_VIDEOS` | `false` | backup | Download the video file of a YouTube link preview. The link, its card and the card thumbnail are archived either way. |
| <span id="youtube_videos_delete_existing"></span>`YOUTUBE_VIDEOS_DELETE_EXISTING` | `false` | backup | Delete YouTube preview videos already downloaded. A shared file goes only when no row of any account and no chat folder still refers to it. Ignored, with a warning, while `DOWNLOAD_YOUTUBE_VIDEOS=true`. |
| <span id="verify_media"></span>`VERIFY_MEDIA` | `false` | backup | Check every downloaded file each run and download missing, empty or damaged files again. A missing file, a link whose `_shared` file is gone included, is first restored from a copy on disk. With the media folder missing, unreadable or empty, the check is skipped and nothing changes. When an edit replaced the message's media, the old file is kept as it is and the new media is downloaded beside it. |
| <span id="thumbnail_cache_dir"></span>`THUMBNAIL_CACHE_DIR` | `BACKUP_PATH/media/.thumbs` if writable, else `/tmp/telegram-archive-thumbs` | viewer | Where the viewer caches generated thumbnails. Created if missing. The backup always writes its thumbnails to `media/.thumbs`, so with this set the viewer ignores those and makes its own. The stock compose does not pass it to the viewer. |

## Backup tuning {#backup-tuning}

Feature page: [Schedule and backup tuning](../configuration/schedule.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="batch_size"></span>`BATCH_SIZE` | `100` | backup | Messages written per database batch. Not clamped. |
| <span id="checkpoint_interval"></span>`CHECKPOINT_INTERVAL` | `1` | backup | Save the chat's progress every N batches. Floored at 1. |
| <span id="sync_deletions_edits"></span>`SYNC_DELETIONS_EDITS` | `false` | backup | Re-read every archived message each run to catch edits and deletions. Slow on large archives. Deletions follow `DELETION_MODE`. The mass-operation limit does not apply to it, and it never fires the event webhook. |
| <span id="fill_gaps"></span>`FILL_GAPS` | `false` | backup | Run gap-fill after the startup backup and after each scheduled backup. The one-shot `backup` command never runs it. |
| <span id="gap_threshold"></span>`GAP_THRESHOLD` | `50` | backup | A gap is a jump between stored message ids larger than this. `fill-gaps --threshold` overrides it. |
| <span id="dialog_flood_sleep_threshold"></span>`DIALOG_FLOOD_SLEEP_THRESHOLD` | `60` | backup | While listing dialogs, the backup waits out a FloodWait of up to this many seconds. `0` fails at once. |
| <span id="max_flood_retries"></span>`MAX_FLOOD_RETRIES` | `5` | backup | Retries after a FloodWait or a transient error before a call gives up. |
| <span id="max_flood_wait_seconds"></span>`MAX_FLOOD_WAIT_SECONDS` | `3600` | backup | A FloodWait longer than this is not waited out. The call fails and the work is retried next run. |
| <span id="backoff_min_seconds"></span>`BACKOFF_MIN_SECONDS` | `2.0` | backup | First delay of the exponential backoff. |
| <span id="backoff_max_seconds"></span>`BACKOFF_MAX_SECONDS` | `300.0` | backup | Longest backoff delay. |
| <span id="reaction_resweep_days"></span>`REACTION_RESWEEP_DAYS` | `0` | backup | Each scheduled backup re-checks reactions on messages from the last N days. `0` turns it off. Floored at 0. Works with the listener off. |
| <span id="reaction_resweep_max_per_chat"></span>`REACTION_RESWEEP_MAX_PER_CHAT` | `500` | backup | Most messages re-checked per chat per run. Floored at 1. |
| <span id="reaction_resweep_batch_delay_seconds"></span>`REACTION_RESWEEP_BATCH_DELAY_SECONDS` | `2.0` | backup | Minimum gap between re-sweep requests, across chats. `0` removes the gap. Floored at 0. |

## Real-time listener {#listener}

Feature page: [Real-time listener](../configuration/listener.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="enable_listener"></span>`ENABLE_LISTENER` | `false` | backup | Start one real-time listener per account inside the `schedule` command. The `LISTEN_*` settings do nothing without it. |
| <span id="listen_new_messages"></span>`LISTEN_NEW_MESSAGES` | `true` | backup | Save new messages as they arrive. Viewer notifications depend on it. |
| <span id="listen_new_messages_media"></span>`LISTEN_NEW_MESSAGES_MEDIA` | `false` | backup | Also download the media of new messages at once. Otherwise media waits for the next scheduled backup. |
| <span id="listen_edits"></span>`LISTEN_EDITS` | `true` | backup | Apply text edits as they happen. The previous text is kept as a version. |
| <span id="listen_deletions"></span>`LISTEN_DELETIONS` | `false` | backup | Apply deletions as `DELETION_MODE` says. When false, the listener only counts them. |
| <span id="deletion_mode"></span>`DELETION_MODE` | `soft` | backup | `soft` marks messages deleted and keeps them. `hard` removes them with their versions, media rows, earlier media, transcripts, reactions and reaction history. Any other value stops startup, in the viewer too. Also applies to `SYNC_DELETIONS_EDITS`. |
| <span id="listen_chat_actions"></span>`LISTEN_CHAT_ACTIONS` | `true` | backup | Save service messages such as joins and leaves, and refresh chat titles and photos. |
| <span id="listen_reactions"></span>`LISTEN_REACTIONS` | `false` | backup | Capture per-emoji reaction counts as they change. |
| <span id="reaction_debounce_seconds"></span>`REACTION_DEBOUNCE_SECONDS` | `1.5` | backup | How often buffered reaction changes are written. Floored at 0.1. |
| <span id="mass_operation_threshold"></span>`MASS_OPERATION_THRESHOLD` | `10` | backup | The most deletions the listener applies to one chat in one window. The listener drops any beyond that, and the chat's deletions stay blocked for one window length. Edits are never limited. Below 1 stops startup, even with the listener off. |
| <span id="mass_operation_window_seconds"></span>`MASS_OPERATION_WINDOW_SECONDS` | `30` | backup | Length of that sliding window, and how long a chat stays blocked after it trips. Below 1 stops startup. |
| <span id="mass_operation_buffer_delay"></span>`MASS_OPERATION_BUFFER_DELAY` | `2.0` | backup | Deprecated and unused. It is still parsed, so a non-number stops startup. |

## Event webhook {#event-webhook}

Feature page: [Event webhook](../configuration/event-webhook.md).

The sub-settings other than the body template are checked only when `EVENT_WEBHOOK_ENABLED` is true. A bad one logs one warning that names the variable, never the value, and turns the webhook off.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="event_webhook_enabled"></span>`EVENT_WEBHOOK_ENABLED` | `false` | backup | Send an HTTP request when the listener applies an edit or a deletion. Needs `ENABLE_LISTENER=true`. A bad boolean stops startup. |
| <span id="event_webhook_url"></span>`EVENT_WEBHOOK_URL` | empty | backup | An `http` or `https` URL with a host name. Required when enabled. Never logged. |
| <span id="event_webhook_method"></span>`EVENT_WEBHOOK_METHOD` | `POST` | backup | `POST` or `PUT`, in any case. |
| <span id="event_webhook_headers"></span>`EVENT_WEBHOOK_HEADERS` | empty, and `Content-Type: application/json; charset=utf-8` is added | backup | A JSON object with string values. The `Content-Type` picks how placeholders are escaped. |
| <span id="event_webhook_events"></span>`EVENT_WEBHOOK_EVENTS` | `message_edited,message_deleted` | backup | Which events fire, from those two names. |
| <span id="event_webhook_chat_ids"></span>`EVENT_WEBHOOK_CHAT_IDS` | empty, all chats | backup | Marked chat ids to fire for. No `-100` correction is applied. |
| <span id="event_webhook_body_template"></span>`EVENT_WEBHOOK_BODY_TEMPLATE` | empty, the built-in JSON body | backup | Custom body with `{placeholder}` substitution. The feature page lists the placeholders and filters. Not checked at startup. An unknown placeholder renders as an empty string and an unknown filter uses the automatic escaping, silently. |

## Voice transcription {#transcription}

Feature page: [Voice transcription](../configuration/transcription.md).

The backup checks the transcription settings only while transcription is on. [What happens on a bad value](#what-happens-on-a-bad-value) lists which ones stop startup and which only log a warning.

!!! note "Added in 8.17.0"
    `TRANSCRIPTION_PROVIDER`, `TRANSCRIPTION_MODEL` and `TRANSCRIPTION_HOTWORDS` were added in 8.17.0. Older images ignore them.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="transcription_enabled"></span>`TRANSCRIPTION_ENABLED` | `true` | both | Turns transcription on or off. Off means no transcription runs, no button in the viewer and no callback route. The stock compose passes it to the viewer. |
| <span id="transcription_url"></span>`TRANSCRIPTION_URL` | empty | both | Base URL of the transcription server, without a `/v1` suffix. Empty means no server. A URL that is not `http` or `https` with a host name warns and turns transcription off. The viewer only checks whether it is set and never connects to it. The stock compose passes it to the viewer. |
| <span id="transcription_api_key"></span>`TRANSCRIPTION_API_KEY` | empty | backup | Key sent in the provider's authentication header. Never logged. |
| <span id="transcription_provider"></span>`TRANSCRIPTION_PROVIDER` | `auto` | backup | `auto`, `akou`, `openai`, `deepgram`, `assemblyai` or `elevenlabs`. An unknown name warns and becomes `auto`. |
| <span id="transcription_preset"></span>`TRANSCRIPTION_PRESET` | `auto` | backup | `lite`, `fast`, `best`, `fusion` or `auto`. Only sent to an akou transcription server. An unknown name warns and becomes `auto`. |
| <span id="transcription_model"></span>`TRANSCRIPTION_MODEL` | empty, the provider's default | backup | Model name for every server except akou, which gets `TRANSCRIPTION_PRESET` instead. The defaults are `whisper-1` on the OpenAI endpoint, `nova-3` on Deepgram and `scribe_v2` on ElevenLabs. AssemblyAI picks its own. |
| <span id="transcription_hotwords"></span>`TRANSCRIPTION_HOTWORDS` | empty | backup | Comma-separated words sent as the prompt on the OpenAI endpoint only. |
| <span id="transcription_types"></span>`TRANSCRIPTION_TYPES` | `voice` | backup | Media transcribed without a button press: `voice`, `video_note`, `audio`, `video`, `document`. Documents count only with an `audio/` or `video/` MIME type. Unknown names are dropped with a warning, and if none remain the value is `voice`. |
| <span id="transcription_max_seconds"></span>`TRANSCRIPTION_MAX_SECONDS` | `1800` | backup | Longer media is skipped with a stored reason. There is no value for unlimited: `0` skips every file with a known length. |
| <span id="transcription_max_upload_mb"></span>`TRANSCRIPTION_MAX_UPLOAD_MB` | `500`<br>`25` when unset and the provider is `openai` | backup | Largest upload, measured on what is sent. `0` means no limit. A negative value warns and means no limit. |
| <span id="transcription_language"></span>`TRANSCRIPTION_LANGUAGE` | empty, the server detects it | backup | Language hint, passed as written. |
| <span id="transcription_diarize"></span>`TRANSCRIPTION_DIARIZE` | `false` | backup | Ask for speaker labels. Boolean. The OpenAI endpoint ignores it. |
| <span id="transcription_callback_url"></span>`TRANSCRIPTION_CALLBACK_URL` | empty, poll only | backup | The viewer's public URL plus `/api/transcriptions/callback`, sent to akou with each job. An invalid URL is dropped with a warning. Its host must be allowed by the akou key, or akou refuses every job. |
| <span id="transcription_webhook_secret"></span>`TRANSCRIPTION_WEBHOOK_SECRET` | empty | viewer | The `whsec_` secret the viewer uses to check akou's callbacks. The callback route exists only when this is valid and transcription is on in the viewer. A value without the `whsec_` prefix is ignored with a warning. The stock compose has it commented out for the viewer. |
| <span id="transcription_backfill_per_run"></span>`TRANSCRIPTION_BACKFILL_PER_RUN` | `50` | backup | Most files sent per backup run per account, button presses included. Floored at 1. When akou runs transcriptions as queued jobs, jobs still open count against it. |
| <span id="transcription_priority_chat_ids"></span>`TRANSCRIPTION_PRIORITY_CHAT_IDS` | empty | backup | Chat ids whose media is sent first, in list order, in every account. Repeats are dropped. |
| <span id="transcription_ask_rate_limit"></span>`TRANSCRIPTION_ASK_RATE_LIMIT` | `30` | viewer | Transcript button presses one client may make in 10 minutes. After that the viewer answers 429 with `Retry-After` and the page says when to try again. A client is its login session, the proxy user name, or, with `ALLOW_ANONYMOUS_VIEWER=true`, the client IP, read as for the login rate limit (`TRUST_PROXY_HEADERS`). The master is not limited. `0` or less means no limit. A non-integer stops startup while transcription is on. |
| <span id="transcription_ask_max_open"></span>`TRANSCRIPTION_ASK_MAX_OPEN` | `50` | viewer | Pressed files that may wait for the backup at once, across the archive. Past it, a press from anyone but the master gets 429 until the next backup run picks some up. `Retry-After` is then one hour, a polling hint, since the viewer does not know the backup's `SCHEDULE`. Only asks from the last 24 hours on downloaded files count, so asks no backup run picks up stop counting after a day and stay in the archive. They also stay queued and still run when a backup takes them, so while the backup is stopped the waiting asks can grow by up to one cap a day. The master is never refused by it. `0` or less means no limit. A non-integer stops startup while transcription is on. |

## Database {#database}

Feature page: [SQLite and PostgreSQL](../configuration/database.md).

The backup and the viewer each pick a database in the order given on [SQLite and PostgreSQL](../configuration/database.md). Both must end up at the same database.

The stock compose passes `DATABASE_URL`, `DB_TYPE`, `DB_PATH` and the `POSTGRES_*` settings to the viewer. It does not pass `DATABASE_PATH`, `DATABASE_DIR`, `DATABASE_TIMEOUT` or `DB_ECHO`.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="database_url"></span>`DATABASE_URL` | unset | both | Full URL, highest priority. Accepts `sqlite://`, `sqlite+aiosqlite://`, `postgresql://`, `postgresql+asyncpg://` and `postgres://`. An absolute SQLite path needs four slashes. The backup image refuses to start with any other scheme. |
| <span id="db_type"></span>`DB_TYPE` | `sqlite` | both | `sqlite`, `postgresql` or `postgres`, in any case. The backup image refuses to start with any other non-empty value. The viewer and a pip install treat any other value as `sqlite` without a warning. |
| <span id="database_path"></span>`DATABASE_PATH` | unset | both | Full SQLite file path. Beats `DATABASE_DIR` and `DB_PATH`. Under the stock compose, setting it in `.env` moves only the backup's database. |
| <span id="database_dir"></span>`DATABASE_DIR` | unset | both | Directory holding `telegram_backup.db`. Beats `DB_PATH`. |
| <span id="db_path"></span>`DB_PATH` | `BACKUP_PATH/telegram_backup.db`<br>compose: `/data/backups/telegram_backup.db` | both | SQLite file path, used when `DATABASE_URL`, `DATABASE_PATH` and `DATABASE_DIR` are unset and `DB_TYPE` is not PostgreSQL. |
| <span id="postgres_host"></span>`POSTGRES_HOST` | `localhost`<br>compose: `postgres` | both | PostgreSQL host. |
| <span id="postgres_port"></span>`POSTGRES_PORT` | `5432` | both | PostgreSQL port. |
| <span id="postgres_user"></span>`POSTGRES_USER` | `telegram` | both | PostgreSQL user. It must be allowed to create the `pg_trgm` extension. |
| <span id="postgres_password"></span>`POSTGRES_PASSWORD` | empty | both | PostgreSQL password. The optional compose `postgres` service refuses to start without it. |
| <span id="postgres_db"></span>`POSTGRES_DB` | `telegram_backup` | both | PostgreSQL database name. |
| <span id="database_timeout"></span>`DATABASE_TIMEOUT` | `60.0` | both | SQLite only: how many seconds to wait for a locked database. A bad, zero or negative value silently becomes 60. No effect on PostgreSQL. |
| <span id="db_echo"></span>`DB_ECHO` | `false` | both | Log every SQL statement. Only the word `true` turns it on. |

## Live updates {#realtime}

Feature page: [Live updates and notifications](../viewer/live-updates.md).

With PostgreSQL, the backup reaches the viewer through the database and these settings do nothing. With SQLite, the backup sends each change to the viewer over unencrypted HTTP. When `VIEWER_HOST` points at another machine, keep the two on a trusted network or a secure tunnel.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="internal_push_secret"></span>`INTERNAL_PUSH_SECRET` | unset. With SQLite, the processes create and share a `.push-secret` file beside the database. | both | Secret the viewer requires on pushes from non-loopback addresses. Needed only when the two processes do not share the database directory. The stock compose passes it to the viewer. |
| <span id="viewer_host"></span>`VIEWER_HOST` | `localhost`<br>compose: `telegram-viewer` | backup | Host the backup sends SQLite pushes to. |
| <span id="viewer_port"></span>`VIEWER_PORT` | `8080`<br>compose: `8000` | backup | Port the backup sends SQLite pushes to. It must match the port the viewer listens on: `8000` in the images and in the pip instructions, so a setup outside the stock compose must set it, because the code default is `8080`. |

## Viewer access {#viewer-access}

Feature pages: [Logins, viewer accounts and share links](../viewer/access.md) and [Exposing the viewer safely](../viewer/exposing.md).

The stock compose passes every variable in this table to the viewer except `MAX_WS_CONNECTIONS` and `MAX_WS_SUBSCRIPTIONS_PER_CONNECTION`.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="viewer_username"></span>`VIEWER_USERNAME` | empty | viewer | Master login name. Password login is on only when this and `VIEWER_PASSWORD` are both set. |
| <span id="viewer_password"></span>`VIEWER_PASSWORD` | empty | viewer | Master login password. Spaces at either end are removed. |
| <span id="allow_anonymous_viewer"></span>`ALLOW_ANONYMOUS_VIEWER` | `false` | viewer | With no password and no proxy login configured, let anyone in as a read-only viewer. Only the word `true` turns it on. Without any of the three, every data request answers HTTP 503. |
| <span id="auth_session_days"></span>`AUTH_SESSION_DAYS` | `30` | viewer | Session lifetime in days, counted from login. A non-integer crashes the viewer. |
| <span id="auth_proxy_header"></span>`AUTH_PROXY_HEADER` | empty | viewer | Request header that carries the user name from a trusted reverse proxy. Setting it turns proxy login on. The proxy must strip that header from client requests. |
| <span id="auth_proxy_admin_users"></span>`AUTH_PROXY_ADMIN_USERS` | empty | viewer | Comma-separated proxy user names, matched exactly, that get the master role. |
| <span id="auth_proxy_default_access"></span>`AUTH_PROXY_DEFAULT_ACCESS` | `none` | viewer | `all` gives new proxy users every chat. Any other value gives them none until an admin grants access. |
| <span id="trust_proxy_headers"></span>`TRUST_PROXY_HEADERS` | `false` | viewer | Take the client address for the login rate limit, the audit log, and the transcript ask limit in an open viewer from `X-Forwarded-For`, then `X-Real-IP`. Only the word `true` turns it on. |
| <span id="secure_cookies"></span>`SECURE_COOKIES` | empty, detected | viewer | `true` or `false` forces the cookie's Secure flag. Any other value sets it when the request arrived over https or carries `X-Forwarded-Proto: https`, whatever `TRUST_PROXY_HEADERS` says. |
| <span id="cors_origins"></span>`CORS_ORIGINS` | `*` | viewer | Comma-separated allowed origins. With `*`, credentials are not allowed. Also the list of origins allowed to open a cross-origin WebSocket, where `*` matches nothing. |
| <span id="max_ws_connections"></span>`MAX_WS_CONNECTIONS` | `200` | viewer | Most WebSocket connections the viewer holds. Extra ones are closed. A non-integer crashes the viewer. |
| <span id="max_ws_subscriptions_per_connection"></span>`MAX_WS_SUBSCRIPTIONS_PER_CONNECTION` | `16` | viewer | Most chats one WebSocket may follow. A non-integer crashes the viewer. |
| <span id="display_chat_ids"></span>`DISPLAY_CHAT_IDS` | empty | viewer | Show only these chats, to every login including the master login. Ids without the `-100` prefix are corrected when the marked chat is archived. |

## Viewer display {#viewer-display}

Feature pages: [Using the viewer](../viewer/using-the-viewer.md) and [Themes and wallpaper](../viewer/themes.md).

The stock compose passes `VIEWER_TIMEZONE`, `VIEWER_DEFAULT_THEME`, `VIEWER_CHAT_BACKGROUND` and `SHOW_STATS` to the viewer. It does not pass the other three.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="viewer_timezone"></span>`VIEWER_TIMEZONE` | `Europe/Madrid` | viewer | Time zone name for displayed times, the date picker and the daily statistics job. An unknown name warns and becomes `UTC`. |
| <span id="viewer_default_theme"></span>`VIEWER_DEFAULT_THEME` | empty, Match system | viewer | Theme for browsers with no saved choice: `system`, `telegram`, `night`, `iosnight`, `slate`, `minimal`, `graphite`, `amoled`, `forest`, `aubergine`, `day`, `paper`. `system` shows Telegram Day on a light system and Telegram Night on a dark one. A `?theme=` link and the browser's saved choice win over it. A value that is not 3 to 16 letters is dropped, and an unknown theme falls back to Match system. |
| <span id="viewer_chat_background"></span>`VIEWER_CHAT_BACKGROUND` | empty, the theme's own wallpaper | viewer | Bare file name of an image in the viewer's static directory, used as the chat wallpaper in every theme. It replaces the theme's own pattern and gradient. Anything that is not a plain file name is ignored with a warning. The image is served without a login. |
| <span id="show_stats"></span>`SHOW_STATS` | `true` | viewer | `false` hides the Statistics row in the main menu. The statistics API still answers. |
| <span id="stats_calculation_hour"></span>`STATS_CALCULATION_HOUR` | `3` | viewer | Hour, 0 to 23 in `VIEWER_TIMEZONE`, of the viewer's daily statistics run. A bad value warns and becomes 3. Under the stock compose, setting it in `.env` has no effect, because the viewer does not receive it. Add it to the viewer's `environment:` block. |
| <span id="media_open_cmd"></span>`MEDIA_OPEN_CMD` | empty | viewer | Shell command behind the master-login-only Open button. Placeholders `%PATH%`, `%DIR%` and `%FILENAME%` are quoted and filled in. Opens images, video, audio and PDF only. Runs on the machine serving the viewer, so it suits native installs. |
| <span id="media_open_path_cmd"></span>`MEDIA_OPEN_PATH_CMD` | empty | viewer | Shell command behind the master-login-only Show in folder button. Same placeholders, any file type. |

## Notifications {#notifications}

Feature page: [Live updates and notifications](../viewer/live-updates.md).

The stock compose passes every variable in this table to the viewer except `ENABLE_NOTIFICATIONS`.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="push_notifications"></span>`PUSH_NOTIFICATIONS` | `basic` | viewer | `off`, `basic` or `full`. Only `full` sends Web Push, which works with the browser closed. Any other value becomes `basic` without a warning. |
| <span id="enable_notifications"></span>`ENABLE_NOTIFICATIONS` | `false` | viewer | Legacy. Notifications count as on when this is true or `PUSH_NOTIFICATIONS` is `basic` or `full`. |
| <span id="vapid_private_key"></span>`VAPID_PRIVATE_KEY` | empty, generated and stored in the database | viewer | Web Push signing key. Used only when both VAPID keys are set. |
| <span id="vapid_public_key"></span>`VAPID_PUBLIC_KEY` | empty, generated and stored in the database | viewer | Web Push public key given to browsers. |
| <span id="vapid_contact"></span>`VAPID_CONTACT` | `mailto:admin@example.com` | viewer | Contact address sent with every Web Push request. |

## Logging {#logging}

Feature page: [Monitoring and troubleshooting](../operations/troubleshooting.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="log_level"></span>`LOG_LEVEL` | `INFO` | backup | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case. `WARN` counts as `WARNING`. An unknown name silently becomes `INFO`. Applied by the backup, the scheduler and the CLI. The viewer always logs at `INFO`, even though the stock compose passes it. |
| <span id="log_chat_titles"></span>`LOG_CHAT_TITLES` | `false` | backup | Name the chat on the two per-chat progress lines. Private chats are named by kind only. |
| <span id="flood_wait_log_threshold"></span>`FLOOD_WAIT_LOG_THRESHOLD` | `10` | backup | While fetching message history, a first FloodWait shorter than this many seconds is not logged. `0` logs every one. Other calls log every FloodWait as a warning. |

## Health checks and images {#health-and-image}

Feature page: [Monitoring and troubleshooting](../operations/troubleshooting.md).

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="heartbeat_file"></span>`HEARTBEAT_FILE` | `/tmp/telegram-archive.heartbeat` | backup | File the `schedule` command rewrites every 30 seconds and the backup health check reads. Other commands never write it. |
| <span id="heartbeat_max_age_seconds"></span>`HEARTBEAT_MAX_AGE_SECONDS` | `180` | backup | The backup container reports unhealthy once the heartbeat is older than this. A value that is not an integer makes every health check fail, so the container stays unhealthy. |
| <span id="healthcheck_url"></span>`HEALTHCHECK_URL` | `http://127.0.0.1:8000/api/health` | viewer | URL the viewer health check requests. The check passes only when the JSON response has `status` set to `ok`. |
| <span id="alembic_config"></span>`ALEMBIC_CONFIG` | `/app/telegram_archive/alembic.ini` in the backup image | backup | Set by the backup image so a bare `alembic` command works inside the container. |

## Maintenance scripts only {#scripts-only}

Feature page: [Import and maintenance tasks](../operations/maintenance.md). These scripts ship in the backup image under `/app/scripts`, not in the PyPI package.

| Variable | Default | Read by | Notes |
|---|---|---|---|
| <span id="telegram_phone_code_hash"></span>`TELEGRAM_PHONE_CODE_HASH` | unset | script | `scripts/auth_noninteractive.py verify` uses it first. Only when it is unset or empty does the script read the file written by its `send` step. The script handles the single legacy account only. |
| <span id="session_path"></span>`SESSION_PATH` | unset | script | Full session file path for `scripts/restore_chat.py`. Without it the script uses `SESSION_DIR`, default `/data/session`, plus `SESSION_NAME`. |
| <span id="sqlite_path"></span>`SQLITE_PATH` | unset | script | Source database for `scripts/migrate-sqlite-to-postgres.py`, checked before `DATABASE_PATH`, `DATABASE_DIR`, `DB_PATH` and the usual locations. |
| <span id="media_path"></span>`MEDIA_PATH` | `/data/backups/media` | script | Default media path for `scripts/migrate_media_paths.py`. The script builds its database URL on its own: it ignores `DB_PATH`, `DATABASE_PATH` and `DATABASE_DIR`, treats only `DB_TYPE=postgresql` as PostgreSQL and defaults `POSTGRES_PASSWORD` to `telegram`. Pass `--db-url` or set `DATABASE_URL` when your database is elsewhere. |

## Variables that do nothing {#removed-settings}

- `LISTEN_ALBUMS` is no longer read. Albums are grouped automatically.
- `AVATAR_REFRESH_HOURS` is no longer read. Avatars are checked on every run.
- [`MASS_OPERATION_BUFFER_DELAY`](#mass_operation_buffer_delay) is unused but still parsed.
- No forensic, hashing, timestamping or blockchain settings exist. Names such as `FORENSIC_MODE` or `HASH_ALGORITHM` are ignored.
