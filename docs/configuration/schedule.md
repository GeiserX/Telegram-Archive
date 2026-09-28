# Schedule and backup tuning

The backup service runs on a cron schedule and backs up every account in turn.

## Two ways to run the backup

| Command | Runs | Use it for |
|---------|------|------------|
| `schedule` | Until stopped | Normal operation. The stock compose file uses it. |
| `backup` | One run over every account, then exits | A manual run, or an external scheduler. |

### The schedule command

The `schedule` command opens one shared Telegram connection per account and keeps it open. It starts the [real-time listener](listener.md) when `ENABLE_LISTENER=true`. It runs one backup straight away, then waits for the next [`SCHEDULE`](#when-backups-run) tick. Every 30 seconds it writes the heartbeat file the Docker health check reads. If a listener dies, the scheduler restarts it after 5 seconds. SIGTERM and SIGINT cancel the running backup, stop the listeners and close the Telegram connections.

### The backup command

The `backup` command runs one backup over every account and exits. It fails only when every account failed. It runs no gap-fill, starts no listener and writes no heartbeat.

!!! warning "Stop the scheduler before a one-shot backup"
    Stop the `schedule` process first and start it again afterwards. See [One client per session](../getting-started/telegram-login.md#one-client-per-session).

=== "Docker"

    ```bash
    docker compose stop telegram-backup
    docker compose run --rm telegram-backup python -m telegram_archive backup
    docker compose start telegram-backup
    ```

=== "PyPI"

    ```bash
    telegram-archive --data-dir ./data backup
    ```

A container that runs `backup` instead of `schedule` writes no heartbeat. Docker checks the heartbeat every 60 seconds once the 300-second start period ends. After three failed checks, about three minutes, it marks the container unhealthy. See [Monitoring and troubleshooting](../operations/troubleshooting.md#backup-container).

## When backups run

[`SCHEDULE`](../reference/environment-variables.md#schedule) is a cron expression with five fields: minute, hour, day, month and day of week. The default is `0 */6 * * *`, which runs at minute 0 of every sixth hour. Any other number of fields stops the scheduler at startup.

| `SCHEDULE` | Runs |
|------------|------|
| `0 */6 * * *` | Every 6 hours, on the hour |
| `0 * * * *` | Every hour |
| `30 3 * * *` | Every day at 03:30 |
| `0 2 * * sun` | Every Sunday at 02:00 |

!!! tip "Use day names"
    In the day-of-week field, `0` means Monday, not Sunday as in crontab. Names such as `mon`, `sun` or `mon-fri` avoid the confusion.

The schedule uses the local time of the backup process. The images run in UTC unless you set `TZ` on the backup service. [`VIEWER_TIMEZONE`](../reference/environment-variables.md#viewer_timezone) does not change it. The stock compose file passes the whole `.env` to the backup service, so one line there is enough:

```bash
TZ=Europe/Berlin
```

A run that starts late, for example after the machine was asleep, still runs if it is less than 3600 seconds late. Several missed runs collapse into one. When a tick arrives while a backup is still running, the scheduler skips that tick and logs a warning.

Accounts are backed up one after another, in configuration order. With several accounts, one run takes as long as all the accounts' runs added together. See [Multiple accounts](multiple-accounts.md).

## What one run does

For each account, in order:

1. Log in and store the account owner's id.
2. Write the last backup time and set the "backup in progress" flag.
3. Delete previously downloaded YouTube preview videos and their rows, when `YOUTUBE_VIDEOS_DELETE_EXISTING` is on and `DOWNLOAD_YOUTUBE_VIDEOS` is off. See [Media downloads](media.md#youtube-preview-videos).
4. Correct configured chat ids that lack the `-100` prefix, and refresh folder membership for folder-based filters.
5. Select the chats to back up, priority chats first. [Choosing chats](choosing-chats.md) explains the rules.
6. For each chat:
    1. Update the chat row. With [`DOWNLOAD_CHAT_DESCRIPTION=true`](../reference/environment-variables.md#download_chat_description), this includes the description and member count, at the cost of one extra request per chat. If that request hits a FloodWait, descriptions are skipped for the rest of the run and fetched again on the next one; the chat keeps the values it already has. The member count is filled in for channels and supergroups.
    2. Check the chat's avatar and download it when it changed.
    3. Fetch new messages, oldest first.
    4. Sync edits and deletions, if [`SYNC_DELETIONS_EDITS`](#sync_deletions_edits) is on.
    5. Refresh pinned messages, up to 100 per chat.
    6. Re-check recent reactions, if the [reaction re-sweep](#reaction-re-sweep) is on.
7. Back up archived chats that the main loop did not already cover.
8. Reconcile group-to-supergroup migrations.
9. Refresh [forum topics and folders](#forum-topics-and-folders).
10. Recalculate statistics.
11. Retry failed media downloads. See [Media downloads](media.md).
12. Verify media files, if `VERIFY_MEDIA` is on.
13. Send pending media for [transcription](transcription.md).

The run clears the "backup in progress" flag at the end, even when it fails.

When a chat is private or forbidden, or you are banned from it, the run logs it as skipped. Any other error in one chat is logged, and the run moves on to the next chat.

The last backup time is taken at the start of the run. The archive stores one value for all accounts, so with several accounts the last account to start wins.

## Where a run picks up

Each chat has a stored position per account: the id of the newest message already archived. A run fetches only messages with a higher id. A chat with no position starts from its first message, so the first run fetches the whole history. The position never moves back.

Messages are written in batches of [`BATCH_SIZE`](../reference/environment-variables.md#batch_size) messages, 100 by default. The position is saved every [`CHECKPOINT_INTERVAL`](../reference/environment-variables.md#checkpoint_interval) batches. The default is 1, which is also the minimum. It is saved once more at the end of each chat. After a crash, only the messages since the last saved position are fetched again.

A message that fails to process holds the position just before it. The rest of the chat is still stored, and the failed message is tried again on the next run. Once the same message has failed on 2 separate runs, the position moves past it. The backup records its id in the metadata.

Older messages are never read again by a normal run. Edits and deletions of messages below the position reach the archive only through `SYNC_DELETIONS_EDITS` or the real-time listener. Two things are refreshed on every run anyway: pinned flags, and reactions on every message fetched in that run.

## Catching changes to older messages

| Option | What it catches | Cost |
|--------|-----------------|------|
| [Real-time listener](listener.md) | Edits, deletions, new messages and reactions as they happen | A process that stays connected. |
| [`SYNC_DELETIONS_EDITS`](#sync_deletions_edits) | Edits and deletions of every archived message | Re-reads the whole archive on every run. |
| [`REACTION_RESWEEP_DAYS`](#reaction-re-sweep) | Reaction changes on recent messages | Up to 500 messages per chat per run, spaced out. |
| [`FILL_GAPS`](#gap-fill) | Messages missing between two archived messages | One scan of the stored ids per chat after each backup run. |

### SYNC_DELETIONS_EDITS

With [`SYNC_DELETIONS_EDITS=true`](../reference/environment-variables.md#sync_deletions_edits), every run re-reads every archived message of every chat, 100 ids per request. The sync applies an edit when Telegram reports a different edit date. It also reconciles reactions.

A message counts as deleted only when Telegram's answer matches the requested ids one for one. If the answer does not match, no message from that request is marked deleted in this run. A deletion follows [`DELETION_MODE`](../reference/environment-variables.md#deletion_mode). The default, `soft`, marks the message deleted and keeps it. `hard` removes the row.

This sync fires no [event webhook](event-webhook.md).

!!! danger "No mass-operation limit"
    The [mass-operation protection](listener.md#mass-operation-protection) only covers the real-time listener. This sync has no such limit. With `DELETION_MODE=hard`, it removes every message that Telegram confirms as gone, however many there are. Keep `DELETION_MODE=soft` unless you want deleted messages gone from the archive too.

### Reaction re-sweep

[`REACTION_RESWEEP_DAYS`](../reference/environment-variables.md#reaction_resweep_days) re-checks reactions on messages from the last N days, in every chat, on every run. It is off by default and works without the listener.

| Variable | Default | Effect |
|----------|---------|--------|
| [`REACTION_RESWEEP_DAYS`](../reference/environment-variables.md#reaction_resweep_days) | `0` | Days to look back. `0` turns the re-sweep off. |
| [`REACTION_RESWEEP_MAX_PER_CHAT`](../reference/environment-variables.md#reaction_resweep_max_per_chat) | `500` | Most messages re-checked per chat per run. Requests hold 100 ids. |
| [`REACTION_RESWEEP_BATCH_DELAY_SECONDS`](../reference/environment-variables.md#reaction_resweep_batch_delay_seconds) | `2.0` | Minimum gap between re-sweep requests, across all chats. |

After a [FloodWait](#rate-limits-and-retries), the re-sweep waits the required time plus 2 seconds, then continues in the same run. After 3 FloodWaits in one run, it stops until the next run. Progress is saved, so the next run continues where this one stopped. Saved progress older than 48 hours, or from a different day window, is discarded.

### Gap-fill

With [`FILL_GAPS=true`](../reference/environment-variables.md#fill_gaps), the scheduler looks for holes after the startup backup and after each scheduled backup. It never runs after a one-shot `backup`.

A gap is two consecutive stored message ids further apart than [`GAP_THRESHOLD`](../reference/environment-variables.md#gap_threshold), 50 by default. The missing range is fetched and stored. Gap-fill never moves the chat's position. When it recovers messages, it recalculates [statistics](#statistics).

A hole before a chat's earliest archived message is reported in the log but never fetched. To run gap-fill by hand, for one chat or with another threshold, use the `fill-gaps` command described in [Import and maintenance tasks](../operations/maintenance.md).

## Rate limits and retries

Telegram answers too many requests with a FloodWait: an order to wait a number of seconds. The client never sleeps through a FloodWait on its own. There are two exceptions, and both absorb waits of up to 60 seconds by default:

| Variable | Default | Applies to |
|----------|---------|------------|
| [`MEDIA_FLOOD_SLEEP_THRESHOLD`](../reference/environment-variables.md#media_flood_sleep_threshold) | `60` | Media transfers. The transfer resumes at its current offset. |
| [`DIALOG_FLOOD_SLEEP_THRESHOLD`](../reference/environment-variables.md#dialog_flood_sleep_threshold) | `60` | Listing your chats. The listing resumes on the same page. |

For how this interacts with the download timeout, see [Media downloads](media.md).

Message history, chat lookups, the deletion and edit sync, pinned messages and forum topic pages go through a bounded retry:

| Variable | Default | Effect |
|----------|---------|--------|
| [`MAX_FLOOD_RETRIES`](../reference/environment-variables.md#max_flood_retries) | `5` | Retries for one call. |
| [`MAX_FLOOD_WAIT_SECONDS`](../reference/environment-variables.md#max_flood_wait_seconds) | `3600` | A longer FloodWait fails at once. The chat or file is tried again on the next run. |
| [`BACKOFF_MIN_SECONDS`](../reference/environment-variables.md#backoff_min_seconds) | `2.0` | Base of the exponential backoff. |
| [`BACKOFF_MAX_SECONDS`](../reference/environment-variables.md#backoff_max_seconds) | `300.0` | Ceiling of the backoff. |
| [`FLOOD_WAIT_LOG_THRESHOLD`](../reference/environment-variables.md#flood_wait_log_threshold) | `10` | While fetching message history, a first FloodWait shorter than this is not logged. `0` logs them all. |

The folder fetch near the end of the run, the avatar download and the chat description fetch are not retried; a failure there is logged and the step waits for the next run.

The sleep before retry number *n* is:

```text
max(required wait, min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS × 2^(n-1))) + 0.5 to 2 s of jitter
```

Transient errors, such as timeouts and dropped connections, use the same exponential backoff with 0.5 to 1.5 s of jitter, and reconnect the client when it is down. These errors are never retried:

- an expired file reference
- a private or forbidden chat
- an invalid chat or peer id
- an unauthorized session
- an auth key error
- a ban from the channel

While fetching a chat's history, a FloodWait or a dropped connection resumes from the last message it already received instead of starting the chat over. The retry counter resets each time a message arrives, so a long history is not cut short by scattered waits.

An invalid value in one of these retry variables logs a warning and falls back to its default. An invalid value in most other settings stops startup with the variable's name.

## Statistics

Statistics are recalculated at the end of every backup run, and after a gap-fill that recovered messages. [`STATS_CALCULATION_HOUR`](../reference/environment-variables.md#stats_calculation_hour) does not affect the backup. It sets the hour of the viewer's own daily recalculation. See [Using the viewer](../viewer/using-the-viewer.md).

## Forum topics and folders

Forum topics are fetched early in each forum chat's backup and again at the end of the run. Telegram returns them 100 per page, and the backup reads up to 50 pages. Deleted topics and topics listed in `SKIP_TOPIC_IDS` are not stored. Custom emoji icons are resolved to their emoji. If the topic request fails before any page arrives, topics are inferred from the topic ids on the stored messages.

Near the end of each run, your Telegram folders are stored so the viewer can show them as tabs. Membership is resolved against the archived chats and includes category flags such as "all groups". Mute and read state are not archived, so the "exclude muted" and "exclude read" flags are not applied. Folders you deleted in Telegram are removed from the archive.
