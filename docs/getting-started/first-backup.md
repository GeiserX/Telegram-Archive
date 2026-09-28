# Your first backup

This page shows how to follow the first run and how to check that it worked.

## What the first run does

The compose file runs the `schedule` command. It starts one backup as soon as it connects to Telegram. After that it waits for the cron schedule.

The first run reads each chat from the start of its history, oldest message first. Chats listed in `PRIORITY_CHAT_IDS` go first. The rest follow in order of most recent activity.

The backup writes messages in batches of 100 and saves its progress after each batch. If the container stops or restarts, the next run continues from the last saved batch instead of starting the chat again.

The backup downloads media as it reads the messages. Each file is stored once and shared between the chats that contain it.

When every chat is done, the run fetches forum topics and Telegram folders. It then recalculates the statistics and retries failed downloads. If a transcription server is configured, it sends queued voice messages to it.

The full ordered list of steps in one run is in [Schedule and backup tuning](../configuration/schedule.md).

## Rate limits are normal

Telegram slows down large accounts with FloodWait replies, which ask the client to pause for a number of seconds. The archive waits out the pause and retries. On a large account you will see many of these during the first run. They are expected and need no action.

The archive does not wait out a FloodWait longer than 3600 seconds. It retries that chat or file on the next run. To change the limit, see `MAX_FLOOD_WAIT_SECONDS` in [Schedule and backup tuning](../configuration/schedule.md).

## Watch the logs

```bash
docker compose logs -f telegram-backup
```

By default, the logs do not contain chat names, message text, account names or phone numbers. The per-chat progress lines show a position such as `[12/40] Backing up`, not the chat. Archived chats show as `[Archived 3/7]`. To add the chat title to those lines, set `LOG_CHAT_TITLES=true`. The archive replaces control and formatting characters in the title with spaces and cuts it to 64 characters. Private chats still show only as `private chat`, never by name.

## Watch from the viewer

Open the viewer while the backup runs. You will see the following:

- Chats appear in the list as they are written. The list does not refresh on its own, so reload the page to pick up new chats.
- The open chat checks for new messages every 3 seconds, so you can watch a chat fill up.
- The **Stats** dropdown in the sidebar header shows cached totals and a **Backup in progress** marker from the last page load. On a new archive the totals show what the viewer saw when it started. The run recalculates them when it finishes. Reload the page to see the new totals or a change in the marker.
- Only the master login sees the [Archive Status](../viewer/using-the-viewer.md#archive-status) panel.

For a full tour of the viewer, see [Using the viewer](../viewer/using-the-viewer.md).

## Check the container health

```bash
docker compose ps
```

The backup container reports `healthy` while the scheduler is running.

!!! warning "Healthy does not mean the backup worked"
    The health check only proves the process runs. Read the logs and the Archive Status panel to confirm the backup. See [Backup container](../operations/troubleshooting.md#backup-container).

## After the first run

Later runs are much shorter. For each chat they only fetch messages newer than the last one stored.

Later runs do not read older messages again. To catch edits and deletions, see [Catching changes to older messages](../configuration/schedule.md#catching-changes-to-older-messages).

## Settings worth changing now

| Setting | What it does | Read more |
|---|---|---|
| `ENABLE_LISTENER=true` | Saves new messages, edits and other changes as they happen, and sends live updates to the viewer. | [Real-time listener](../configuration/listener.md) |
| `CHAT_TYPES` | Chooses which kinds of chat are saved. Bot chats are left out by default. Add `bots` to include them. | [Choosing chats](../configuration/choosing-chats.md) |
| `SCHEDULE` | Sets when backups run, as a cron expression. It uses UTC unless you set `TZ` on the backup service, for example `TZ=Europe/Madrid`. | [Schedule and backup tuning](../configuration/schedule.md) |
| `MAX_MEDIA_SIZE_MB` | The backup skips files larger than this. The default is 100. `0` removes the limit. | [Media downloads](../configuration/media.md) |
| `TRANSCRIPTION_URL` | Points at a transcription server to get text for voice messages. | [Voice transcription](../configuration/transcription.md) |

Change the values in `.env`, then apply them:

```bash
docker compose up -d
```

Compose recreates the containers whose settings changed. A recreated backup container starts a new run right away. The run continues from the saved progress.

## Stop the backup

```bash
docker compose stop telegram-backup
```

The scheduler handles the stop signal and ends its work cleanly. The compose file gives the container 90 seconds to exit. Progress up to the last saved batch is kept.

To stop the whole stack, run `docker compose stop` with no service name. That stops the viewer too.

!!! danger "One Telegram client per session"
    Stop the `telegram-backup` service before any command that connects to Telegram. If two clients use one session file, Telegram can invalidate the login. [One client per session](telegram-login.md#one-client-per-session) lists which commands connect and which only read the database.
