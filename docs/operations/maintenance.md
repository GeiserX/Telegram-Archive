# Import and maintenance tasks

This page covers one-off jobs on an archive that is already running.

## Ground rules

Stop the backup service before any command that connects to Telegram, and start it again afterwards. See [One client per session](../getting-started/telegram-login.md#one-client-per-session).

The container's root filesystem is read-only. Any file a command writes must go under `/data`, which is the `./data` folder on the host.

Without Docker, run the same commands with the `telegram-archive` command and point it at your data directory:

```bash
telegram-archive --data-dir ./data fill-gaps
```

## Import a Telegram Desktop export

The `import` command reads an export made by Telegram Desktop and writes it into the archive. It needs no Telegram login.

### Make the export

Telegram Desktop offers two export formats, and the importer reads both:

- JSON: open Settings, Advanced, Export Telegram data, and choose the machine-readable JSON format. You can export the full account or a single chat.
- HTML: a per-chat HTML export also works. It has no chat id, so pass one with `-c`.

### Put it where the container can see it

Copy the export folder under `./data`, for example `./data/import/<folder>`. Inside the container it appears as `/data/import/<folder>`. The container runs as uid 1000, so give that user ownership:

```bash
sudo chown -R 1000:1000 data/import
```

### Run the import

Always start with a dry run. It parses and checks the export without writing to the database or copying media:

=== "Docker"

    ```bash
    docker compose run --rm telegram-backup \
      python -m telegram_archive import -p /data/import/<folder> --dry-run
    docker compose run --rm telegram-backup \
      python -m telegram_archive import -p /data/import/<folder>
    ```

=== "Without Docker"

    ```bash
    telegram-archive --data-dir ./data import -p ./data/import/<folder> --dry-run
    telegram-archive --data-dir ./data import -p ./data/import/<folder>
    ```

`-p` names the export folder. For an HTML export, pass the chat id in marked form with `-c`, for example `-1001234567890` for a supergroup or channel. With a multi-chat JSON export, `-c` imports only the first chat, under that id. For every flag, see [import](../reference/cli.md#import).

When it finishes, the command prints `Import complete:` with the number of chats, messages and media files, then one line per chat with its id, message count and media count.

### What the importer does

- It uses `result.json` when the folder has one. Otherwise it reads `messages.html`, `messages2.html` and the rest. When neither exists it stops with `No result.json or messages.html found in <path>. Expected a Telegram Desktop export directory.`
- For a JSON export it derives the marked id from the chat type. Private chats, bot chats and Saved Messages keep the raw id. Basic groups become `-id`. Supergroups and channels become `-(1000000000000 + id)`, the form that starts with `-100`.
- It streams `result.json` one message at a time, so memory use stays flat on a large export.
- If a JSON import stops partway, run the same command on the same file again. The importer skips the chats it finished and replays the interrupted one. You do not need `--merge` for this. An HTML import does not resume.
- It refuses to import into a chat that already has messages, unless you pass `--merge`.
- It copies media files into `media/<chat_id>/` in the archive. The export must stay readable for the whole run, and the copies need free disk space of their own. Media the archive already holds for a message is skipped.
- Everything is written under account 1, even when the install has several accounts.
- Only a full-account JSON export tells the importer which messages you sent. HTML and single-chat exports leave that flag unset.
- When an HTML export date carries a `UTC+HH:MM` suffix, the time is converted to UTC. Without the suffix the time is stored as written, as the exporting computer's local time.

### After the import

The next backup uses the imported media files and does not download them again.

The backup continues from the export's newest message only when the export starts at message 1. An export that starts later, such as one limited to a date range, does not move the chat's backup starting point. The next backup still fetches the older history from Telegram.

A Telegram Desktop export has no forum topic data, so imported messages from a forum group all land in the General topic. To sort them into their topics, run `backfill-topics` with the scheduler stopped:

```bash
docker compose stop telegram-backup
docker compose run --rm telegram-backup python -m telegram_archive backfill-topics -c -1001234567890
docker compose start telegram-backup
```

`backfill-topics` makes every account forget how far it had backed up that chat, then reads the whole chat again from Telegram. This pass downloads no media, does not sync edits or deletions, and does not verify media. It refuses a chat that is not in the archive yet, so import or back up the chat first. If an account has its own `TG_ACCOUNT_<N>_CHAT_IDS`, that account re-reads the chats in that list, not the chat you passed with `-c`.

## Fill gaps in message history

`fill-gaps` looks for holes in the message ids of archived chats and fetches the missing messages from Telegram.

This example stops the backup service, scans one chat for gaps larger than 20 ids, and starts the service again:

```bash
docker compose stop telegram-backup
docker compose run --rm telegram-backup python -m telegram_archive fill-gaps -c -1001234567890 -t 20
docker compose start telegram-backup
```

`-c` limits the scan to one chat. Without it, every archived chat that passes the current chat filters is scanned. `-t` sets the threshold: only gaps larger than it are investigated. It overrides `GAP_THRESHOLD`, which defaults to 50. For every flag, see [fill-gaps](../reference/cli.md#fill-gaps).

A gap is two neighboring stored message ids that are further apart than the threshold. The command prints a summary:

```text
Gap-fill complete:
  Chats scanned: ...
  Chats with gaps: ...
  Total gaps found: ...
  Messages recovered: ...
```

It also reports history missing before a chat's earliest archived message, but does not fetch it. To fetch that history, import an export that covers it. With several accounts, the command leaves this line out of the summary. Look for the `ids missing before id` note on the per-chat lines instead.

To run gap-filling after every scheduled backup instead, set `FILL_GAPS=true`. See [Schedule and backup tuning](../configuration/schedule.md).

## Reclassify round videos

Archives captured before 8.5.0 stored round video messages as ordinary videos. `reclassify-round-videos` asks Telegram which archived videos are round videos and changes the type of those rows in place. Nothing is downloaded, renamed or deleted.

```bash
docker compose stop telegram-backup
docker compose run --rm telegram-backup python -m telegram_archive reclassify-round-videos --dry-run
docker compose run --rm telegram-backup python -m telegram_archive reclassify-round-videos
docker compose start telegram-backup
```

`-c CHAT_ID` limits it to one chat. Without it, every chat with videos is checked. The summary lists chats scanned, round videos found and rows re-typed.

!!! note "Reload open viewer tabs afterwards"
    An open viewer tab shows "Media not found" for a re-typed video until you reload the page.

## Verify media files

Media verification checks every downloaded file and downloads it again when it is missing, empty or the wrong size. You turn it on with a setting:

1. Set `VERIFY_MEDIA=true` in `.env`.
2. Run `docker compose up -d telegram-backup`. A plain restart keeps the old environment.
3. Wait for the backup that runs at startup to finish. It verifies the files.
4. Set `VERIFY_MEDIA=false` and run `docker compose up -d telegram-backup` again.

See [Media downloads](../configuration/media.md) for what it checks.

## Export to JSON

`export` writes messages to a JSON file. It only reads the database, so it can run in the live container:

```bash
docker compose exec telegram-backup \
  python -m telegram_archive export -o /data/backups/export.json -c -1001234567890 -s 2024-01-01 -e 2025-01-01
```

`-o` names the output file, which must be under `/data`. `-c` exports one chat's messages. `-s` is the first day to include. `-e` is the first day to exclude: the command compares it as midnight at the start of that day. To include a whole last day, pass the day after it. The example above covers all of 2024. For every flag, see [export](../reference/cli.md#export).

The `chats` list in the file always holds every chat in the archive, even with `-c`. The export leaves out media files. For the file layout, see [Command line and Python API](../reference/cli.md).

## Merge two archives

The SQL scripts in `scripts/merge` combine two archives. They accept only databases at schema revision 023, the newest revision in 8.0.0 and 8.0.1, and refuse every later one. Use them only on an archive that never left 8.0.0 or 8.0.1.

The scripts do not carry data added after revision 023: avatar history, transcripts, chat avatar photo ids, media skip reasons and the search tables. Downgrading with `docker compose run --rm telegram-backup alembic downgrade 023` drops that same data, and the project does not document or test that path. From a checkout, the command is `alembic -c telegram_archive/alembic.ini downgrade 023`.

For an archive still on 8.0.x, the scripts need the `sqlite3` command line tool 3.33.0 or newer on the host for SQLite, or `postgres_fdw` on PostgreSQL. Neither app image contains them.

The supported way to hold two accounts in one archive is to declare both accounts in one install and let it fetch the second account's history again from Telegram. See [Multiple accounts](../configuration/multiple-accounts.md).

## Maintenance scripts

The repository's `scripts/` folder ships in the backup image under `/app/scripts`. It is not part of the PyPI package. Run a script in the backup container from `/app`. When a script has `--dry-run`, always run it with that flag first:

```bash
docker compose run --rm telegram-backup python scripts/detect_albums.py --dry-run
docker compose run --rm telegram-backup python scripts/detect_albums.py
```

Stop the backup service before a run that changes data, and take a backup first. See [Backing up the archive](backup-and-restore.md).

[Command line and Python API](../reference/cli.md#repository-scripts) lists every script with its flags and defaults. Before running these scripts, note the following:

- `deduplicate_media.py` moves every media file in the chat folders into `media/_shared` and replaces each one with a relative symlink. It removes duplicate copies of the same file, so one file serves every chat that holds it.
- `restore_chat.py` sends messages as the Telegram account of the session. See [Putting messages back into Telegram](backup-and-restore.md#putting-messages-back-into-telegram).
- `fix_media_sizes.py` is broken and fails on every run. Use `update_media_sizes.py` instead.
- `cleanup_legacy_avatars.py` deletes old-style avatar files that already have a new-style replacement. Run it with `--dry-run` first and take a backup. It reads `--backup-path`, default `/data/backups`.

`migrate_media_paths.py` builds its database address from `DATABASE_URL`, then `DB_TYPE` and the `POSTGRES_*` variables, then `BACKUP_PATH/telegram_backup.db`. Pass `--db-url` if your database lives elsewhere.

`generate_dummy_db.py` migrates the new database to the current schema before filling it. It needs `ffmpeg` on the `PATH` to make the voice notes and the round video. Without it, those messages keep their rows but get no file. It stops if the target folder already holds an archive.

!!! danger "Never point the demo generator at your real data"
    `generate_dummy_db.py --force` deletes the whole `backups` folder under `--data-dir`, including the database and media. It does this only when the folder holds the marker file the script writes, so it never deletes an archive it did not create. The default `--data-dir` is `demo-data`. Inside Docker, give it a folder under the volume, such as `--data-dir /data/demo`.

The folder also holds three SQL helpers:

| File | What it does |
|------|--------------|
| `fix_reactions_sequence.sql` | PostgreSQL only. Resets the reactions id sequence after a restore or manual import. Current releases detect and fix this themselves, so the file is a manual fallback. |
| `migrate_to_marked_ids.sql` | PostgreSQL. Converts chat ids in archives from 4.0.5 and earlier from raw positive ids to marked ids. |
| `migrate_to_marked_ids_sqlite.sql` | The same conversion for SQLite. |

The app images contain no `psql` or `sqlite3` client. Run a SQL helper from a checkout of the repository with your own client, for example through the PostgreSQL container from the stock compose file:

```bash
docker exec -i telegram-postgres psql -U telegram -d telegram_backup < scripts/fix_reactions_sequence.sql
```

To move an archive from SQLite to PostgreSQL, see [SQLite and PostgreSQL](../configuration/database.md).
