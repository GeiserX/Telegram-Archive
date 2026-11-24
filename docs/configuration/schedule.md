# Schedule and backup tuning

The backup service saves new messages as they arrive through the [real-time listener](listener.md), and runs a full pass over every account on a cron schedule. Since 9.2.0 the listener is on by default and the full pass runs once a day.

## Two ways to run the backup

| Command | Runs | Use it for |
|---------|------|------------|
| `schedule` | Until stopped | Normal operation. The stock compose file uses it. |
| `backup` | One run over every account, then exits | A manual run, or an external scheduler. |

### The schedule command

The `schedule` command opens one shared Telegram connection per account and keeps it open. It starts the [real-time listener](listener.md), which is on by default (`ENABLE_LISTENER`). It runs one backup straight away, then waits for the next [`SCHEDULE`](#when-backups-run) tick. Every 30 seconds it writes the heartbeat file the Docker health check reads. If a listener dies, the scheduler restarts it after 5 seconds. SIGTERM and SIGINT cancel the running backup, stop the listeners and close the Telegram connections.

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

[`SCHEDULE`](../reference/environment-variables.md#schedule) is a cron expression with five fields: minute, hour, day, month and day of week. Any other number of fields stops the scheduler at startup. An empty value uses the default.

The default depends on the listener:

- With the listener on (the default), `0 3 * * *`: one full pass a day, at 03:00.
- With `ENABLE_LISTENER=false`, `0 */6 * * *`: every 6 hours, because the pass is then the only capture.

At startup the scheduler logs one line that says which mode it runs, for example `Capture mode: real time; full pass on SCHEDULE (0 3 * * *)`.

| `SCHEDULE` | Runs |
|------------|------|
| `0 3 * * *` | Every day at 03:00 (default with the listener on) |
| `0 */6 * * *` | Every 6 hours, on the hour (default with the listener off) |
| `30 3 * * *` | Every day at 03:30 |
| `0 2 * * sun` | Every Sunday at 02:00 |
| `0 * * * *` | Every hour (deprecated with the listener on) |

!!! warning "Deprecated: frequent full passes with the listener on"
    An hourly or other frequent `SCHEDULE` made sense when the scheduled pass was the only capture. With the listener on, each pass walks every chat, finds almost nothing new and spends Telegram's rate limits, so FloodWaits slow the listener's own requests too. Such schedules still work. When the listener is on and `SCHEDULE` runs more than 4 passes a day, startup logs a note. With the listener off, a frequent schedule is still a normal choice. Transcription does not need a frequent pass either. Its drain runs on its own timer, [`TRANSCRIPTION_DRAIN_INTERVAL_MINUTES`](../reference/environment-variables.md#transcription_drain_interval_minutes).

### Why the daily pass stays

The listener does not see everything, so the full pass is still needed:

- Telegram replays only a limited window of missed updates after a disconnect. After a long outage, the pass fetches what the listener could not.
- Reactions you add from another device are not always pushed to the listener.
- A chat you join while the backup is down is picked up by the next pass.
- Gap-fill (`FILL_GAPS`) and the [reaction re-sweep](#reaction-re-sweep) run as part of the pass.
- Media retries, avatar checks and folder refreshes run as part of each pass. So does a transcription drain, besides the one `schedule` runs every `TRANSCRIPTION_DRAIN_INTERVAL_MINUTES`. A file the drain sends back to download waits for the pass.

!!! tip "Use day names"
    In the day-of-week field, `0` means Monday, not Sunday as in crontab. Names such as `mon`, `sun` or `mon-fri` avoid the confusion.

The schedule uses the local time of the backup process. The images run in UTC unless you set `TZ` on the backup service. [`VIEWER_TIMEZONE`](../reference/environment-variables.md#viewer_timezone) does not change it. The stock compose file passes the whole `.env` to the backup service, so one line there is enough:

```bash
TZ=Europe/Berlin
```

A run that starts late, for example after the machine was asleep, still runs if it is less than 3600 seconds late. Several missed runs collapse into one. With the daily default, a pass missed by more than an hour waits for the next day. When a tick arrives while a backup is still running, the scheduler skips that tick and logs a warning.

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
8. Re-check a slice of older messages, if [`SYNC_DELETIONS_EDITS_OLDER_PER_RUN`](#sync-rotation) is set.
9. Reconcile group-to-supergroup migrations.
10. Refresh [forum topics and folders](#forum-topics-and-folders).
11. Recalculate statistics.
12. Retry failed media downloads. See [Media downloads](media.md).
13. Verify media files, if `VERIFY_MEDIA` is on.
14. Send pending media for [transcription](transcription.md).

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
| [`SYNC_DELETIONS_EDITS`](#sync_deletions_edits) | Edits and deletions of every archived message, or of the last N days with [`SYNC_DELETIONS_EDITS_DAYS`](#sync-window), plus a slice of older messages per run with [`SYNC_DELETIONS_EDITS_OLDER_PER_RUN`](#sync-rotation) | Re-reads the whole archive, or the window and the slice, on every run. Telegram answers about 300 ids per 30 seconds, so a million messages take more than a day. |
| [`REACTION_RESWEEP_DAYS`](#reaction-re-sweep) | Reaction changes on recent messages | Up to 500 messages per chat per run, spaced out. |
| [`FILL_GAPS`](#gap-fill) | Messages missing between two archived messages | One scan of the stored ids per chat after each backup run. |

### SYNC_DELETIONS_EDITS

With [`SYNC_DELETIONS_EDITS=true`](../reference/environment-variables.md#sync_deletions_edits), every run re-reads every archived message of every chat, or only the recent ones (see [Checking only recent messages](#sync-window)), 100 ids per request. The sync applies an edit when Telegram reports a different edit date. It also reconciles reactions.

A message counts as deleted only when Telegram's answer matches the requested ids one for one. If the answer does not match, no message from that request is marked deleted in this run. A deletion follows [`DELETION_MODE`](../reference/environment-variables.md#deletion_mode). The default, `soft`, marks the message deleted and keeps it. `hard` removes the row.

This sync fires no [event webhook](event-webhook.md).

!!! danger "No mass-operation limit"
    The [mass-operation protection](listener.md#mass-operation-protection) only covers the real-time listener. This sync has no such limit. With `DELETION_MODE=hard`, it removes every message that Telegram confirms as gone, however many there are. Keep `DELETION_MODE=soft` unless you want deleted messages gone from the archive too.

#### Checking only recent messages {#sync-window}

Telegram answers about 300 message ids per 30 seconds when they are asked for by id. A full sweep of one million messages takes more than a day, so on a large archive one run never ends before the next one is due.

[`SYNC_DELETIONS_EDITS_DAYS`](../reference/environment-variables.md#sync_deletions_edits_days) limits the sync to messages posted in the last N days, highest message id first. The date is the one Telegram gives the message when it is posted, not when it was archived. `0`, the default, re-reads every archived message.

```bash
SYNC_DELETIONS_EDITS=true
SYNC_DELETIONS_EDITS_DAYS=14
```

The window is a deliberate blind spot. Edits, deletions, poll, preview and reaction changes on older posts are not seen, including changes made while the archive was down. There are two ways to catch them:

- Re-check them a slice per run with [`SYNC_DELETIONS_EDITS_OLDER_PER_RUN`](#sync-rotation). No run takes much longer than the others.
- Run one full check: set `SYNC_DELETIONS_EDITS_DAYS=0`, or unset it, and recreate the container. With Docker Compose that is `docker compose up -d`: `docker compose restart` keeps the old environment. The [`schedule`](#the-schedule-command) command runs one backup straight away on start. Set the window back once that run ends. On a large archive this one run takes many hours.

#### Re-checking older messages over several runs {#sync-rotation}

[`SYNC_DELETIONS_EDITS_OLDER_PER_RUN`](../reference/environment-variables.md#sync_deletions_edits_older_per_run) spreads the check of older messages over many runs. It works only with `SYNC_DELETIONS_EDITS=true` and `SYNC_DELETIONS_EDITS_DAYS` above 0.

```bash
SYNC_DELETIONS_EDITS=true
SYNC_DELETIONS_EDITS_DAYS=14
SYNC_DELETIONS_EDITS_OLDER_PER_RUN=20000
```

Each run still checks the window of every chat. After all chats are backed up, the run re-checks up to `SYNC_DELETIONS_EDITS_OLDER_PER_RUN` messages posted before the window. The number applies to each account on its own, and the accounts run one after another. Within an account, all its chats share it:

1. The chats that never finished a full check go first. Then the chat whose last full check is oldest. In each of these two groups, a chat in the middle of a check goes before one that has not started. Otherwise the order is fixed by chat id, so a large chat can take many runs, and the next chat starts when it finishes. The whole archive takes the same number of runs in any order.
2. In each chat, the check goes from the newest older message down, 100 ids per request. Each request counts as 100 messages of the budget, even when a small chat has fewer, because Telegram limits requests, not messages. So a run makes at most one request per 100 of the budget, rounded up, however many small chats it reaches.
3. A chat that has more older messages than the run has left continues on the next run from the same message.
4. When a chat's check reaches its oldest message, the time is recorded. The chat goes to the back of the line, and its next check starts again from the newest older message.
5. A run visits each chat once at most. When every chat has had its turn, the rotation ends, even with budget left.

Only the chats that this run backed up take part. A chat that leaves `CHAT_IDS` or is excluded keeps its place, and continues from there if it comes back. A chat added to the selection has never finished a check, so it goes first. Each account keeps its own progress per chat.

The progress is saved after every request. If the run stops, for example after a crash, at most 100 messages are checked again. If Telegram asks for a longer wait than the [retry](#rate-limits-and-retries) allows, the rotation stops for this run, and the next run continues from the same message. Any other error skips that request's 100 messages, the same way the window pass does: the check carries on below them, the run log counts them, and the next pass of that chat tries them again. A database error for one chat keeps its progress, and the other chats still get the rest of the budget.

The run log ends the step with one line, for example:

```text
Older-message rotation: re-checked 20000 older messages in 3 chat(s), 2 deleted, 5 updated; 1 chat(s) finished a pass, 40 of 120 have not finished one yet
```

When a request failed, the line adds `N skipped after errors, re-checked next pass`. The count "have not finished one yet" covers only the chats this run backs up. When it reaches 0, each of those chats has finished one check. Messages of a skipped request, and ids that a misaligned answer from Telegram left out, wait for that chat's next check. A message that leaves the window while its chat's check is under way, above the point the check has reached, waits for the next check of that chat. The window pass checked it until then.

**How to size it.** Telegram takes about 100 seconds per 1000 messages, so 20,000 messages add about 35 minutes to a run for each account. Size it per account: to re-check an account's history once per period, divide its messages older than the window by the number of runs in that period, and use the largest result. `Total messages` at the end of a run counts every account together, so split it first. For example, an account with 1,500,000 older messages and one run a day re-checks every message once a month with about 50,000 per run, which adds about 85 minutes. With a run every 6 hours, 12,500 per run is enough, about 20 minutes each. Each account adds its own time, so two accounts that each use the whole budget take twice as long. An account with fewer older messages than the budget gets a full check on every run. Because a request counts as 100 (step 2), an archive with many small chats takes more runs per cycle than this division gives. The window's messages come on top of this.

Deletions follow `DELETION_MODE`, the same as on a run with `SYNC_DELETIONS_EDITS_DAYS=0`. With `hard`, a message Telegram no longer returns is removed from the archive, and the rotation now reaches older messages on routine runs. A chat whose old history the account can no longer read, while the chat itself is still backed up, has those older messages marked deleted, or removed with `hard`. A group that hides its history from new members, left and joined again, is one example.

Turning it off (`0`) stops the rotation and keeps the saved progress; turning it on again continues from there. With `SYNC_DELETIONS_EDITS_DAYS=0` every run already checks every message, so the rotation does not run.

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
