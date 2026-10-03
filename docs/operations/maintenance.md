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
- From a JSON export it keeps locations, venues, live locations, shared contacts and polls in the message data, under the same keys the backup uses, so the viewer shows them as cards. The export has no poll option ids, so an imported poll numbers its answers. An HTML export has none of these details.
- It copies media files into `media/<chat_id>/` in the archive. The export must stay readable for the whole run, and the copies need free disk space of their own. Media the archive already holds for a message is skipped. A photo's width and height come from a JSON export as written. An HTML export gives only its thumbnail's size, so the importer reads the photo's size from the file's header instead.
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
    An open viewer tab shows "missing from the archive disk" for a re-typed video until you reload the page.

## Fill old locations, contacts, polls and edit flags

Messages archived before the archive kept locations, venues, live locations and contacts show their card with `Details not archived`. Old polls archived by the backup can be in the same state. Messages archived before 9.0 also lack Telegram's flag for an edit time moved by a reaction, so a reaction shows as a pencil until a backup reads the message again. `backfill-details` asks Telegram for those messages again, each once, and adds only the missing details and flags. It never replaces text, dates, reactions or details already stored. It also clears the placeholder path older releases left on these rows when the file is empty, missing or no longer needed, and leaves every file on disk where it is. Run it where the media folder is mounted, as in the commands below: without it, every path is kept.

It also fetches the map picture of each location, venue and live location that has a point and no picture yet, from Telegram's own servers, the way the backup now keeps it for new messages. It reads those messages in the same requests, so a message on several lists is still asked for once. Each picture is one more request, after the same one-second pause, and a run fetches at most 500; the rest are counted as deferred and wait for the next run. Pictures follow `DOWNLOAD_MEDIA` and `SKIP_MEDIA_CHAT_IDS`, and none is fetched when the media folder is missing or empty where the command runs. The picture takes the place of an old placeholder path on the same row.

It also collects every custom emoji the account's reactions hold, and those in the text of its messages and their earlier versions. An emoji the archive has no record of gets one, and an emoji whose file is still missing, because Telegram left it out of three answers or its download failed three times, is marked to be fetched again. Nothing is deleted. It then fetches the files the way the backup does, at most 500 per run. A dry run only counts them. See [Custom emoji](../configuration/media.md#custom-emoji).

It is a dry run unless you add `--apply`:

```bash
docker compose stop telegram-backup
docker compose run --rm telegram-backup python -m telegram_archive backfill-details
docker compose run --rm telegram-backup python -m telegram_archive backfill-details --apply
docker compose start telegram-backup
```

It costs one request per 100 messages to read, plus one per chat, plus one per map picture, with a second between requests. An archive with 50,000 old edited messages takes at least 500 requests, so ten minutes or more, and 500 old locations add about ten minutes more. A dry run reads the same messages as a real run, but fetches no map picture.

`-c CHAT_ID` limits it to one chat. The summary counts, per kind, the messages filled, the ones that already had their details and the ones Telegram no longer serves, then the edit flags filled as hidden (the pencil goes) and as shown (a real edit keeps it), the edits with a later edit time, then the chats Telegram no longer serves and the placeholder paths cleared and kept, then the map pictures saved, not served, with no point and deferred, then the custom emoji collected, saved, unavailable and deferred. If the run stops, run it again: it picks up the messages still missing their details, their flag or their picture, and a second complete run fills nothing. A location Telegram serves no picture for is marked and not asked for again. For every flag and the full output, see [backfill-details](../reference/cli.md#backfill-details).

## Verify media files

Media verification checks every downloaded file and downloads it again when it is missing, empty or the wrong size. You turn it on with a setting:

1. Set `VERIFY_MEDIA=true` in `.env`.
2. Run `docker compose up -d telegram-backup`. A plain restart keeps the old environment.
3. Wait for the backup that runs at startup to finish. It verifies the files.
4. Set `VERIFY_MEDIA=false` and run `docker compose up -d telegram-backup` again.

See [Media downloads](../configuration/media.md) for what it checks.

To check the files without downloading anything, run [`check-media`](../reference/cli.md#check-media). It counts broken links and missing files and, with `--repair`, puts back the ones with a copy on disk and marks the rest to download again at the next backup run. It also marks downloaded again a file that is back at its path, after an outage of the media volume left its row not downloaded. It finds a video or audio file whose download stopped early and, with `--repair`, marks it to download again. It finds photos stored without their size and, with `--repair`, reads the size from each file's header so the viewer draws them at their real shape. With the media folder not mounted it changes nothing and exits 1:

```bash
docker compose exec telegram-backup python -m telegram_archive check-media
docker compose exec telegram-backup python -m telegram_archive check-media --repair
```

## Export to JSON

`export` writes messages to a JSON file. It only reads the database, so it can run in the live container:

```bash
docker compose exec telegram-backup \
  python -m telegram_archive export -o /data/backups/export.json -c -1001234567890 -s 2024-01-01 -e 2025-01-01
```

`-o` names the output file, which must be under `/data`. `-c` exports one chat's messages. `-s` is the first day to include. `-e` is the first day to exclude: the command compares it as midnight at the start of that day. To include a whole last day, pass the day after it. The example above covers all of 2024. For every flag, see [export](../reference/cli.md#export).

The `chats` list in the file always holds every chat in the archive, even with `-c`. The export lists each message's media but leaves out the files. For the file layout, see [Command line and Python API](../reference/cli.md).

## Merge two archives

The `merge` command copies every Telegram account of another archive, the source, into this archive, the target. Use it when two installs each backed up a different Telegram account and you want one archive and one viewer for both. The command needs no Telegram login.

It reads the target from the same settings as every other command: `DATABASE_URL` or the other database variables, and `--data-dir`. The source is only read, and nothing already in the target is changed or deleted.

### What it copies

Each source Telegram account is added to the target under the next free account id. Every row the account owns follows it under that id:

- its chats, messages, edit history, reactions and their history, media rows and transcripts
- forum topics, folders and folder membership, its [positions](../reference/glossary.md#position) and avatar history
- its per-account records: followed chat migrations, failed-message records and import progress

Chats keep their chat ids and their [chat refs](../reference/glossary.md#chat-ref). A message keeps its media, its edit history and its reactions, because they share its chat id and message id. A transcript that was copied from another transcript still points at its copy in the target.

Users, the table of senders shared by every account, are added only when the target does not know them. The target's row for a user it already has stays as it is. With `--account`, only the users the merged account points at are added: its message senders, the users who reacted, and the other party of its private chats. People only the other source accounts ever saw stay behind.

Media rows are rewritten to paths relative to the target's media folder. The files come across the way the backup writes them:

- A plain file in a chat folder is copied.
- A file stored once in `media/_shared` is copied by content hash. When one of the target's own media rows names a `_shared` file with the same hash, and that file's bytes match the hash, the new chat-folder link points at that file and nothing is copied. The chat-folder entry is created as a relative symlink into `_shared`.
- The avatar files of the merged accounts' chats and senders are copied when the target lacks them.
- Custom emoji are shared by every account. The emoji records and the files in `media/_emoji` that the target lacks are added, and the target's own stay as they are. An emoji whose file does not come across, because the source folder lacks it or no source media folder is given, arrives as not fetched yet, and the target's next backup fetches it.

A name the target already uses for the same bytes counts as already there. A file the source's database lists but its media folder lacks is counted as missing, and its row is still copied.

### What it never touches

- Rows already in the target. Nothing is updated in place or removed, in the database or in the media folder.
- The source archive. PostgreSQL is read in a read-only transaction. A SQLite source that was closed cleanly is opened as an unchanging file, so nothing is written beside it and a read-only folder works. When its `-wal` file still holds changes, SQLite has to write a `-shm` file beside it to read them, so that folder must be writable.
- Viewer state. Viewer accounts, viewer sessions, share links, push subscriptions, the viewer audit log and viewer settings are not merged. They describe who may read the source install, not what it archived. A viewer account in the target that is limited to some accounts does not see the merged accounts until you grant them.
- The target's own records: owner id, last backup time, the backup running flag, the cached statistics and the push keys. The viewer's counts include the merged accounts after the next backup run or the daily recount.
- The Telegram session files. They live outside the database. Copy them yourself, as the example shows.

### The dry run

`--dry-run` runs every check, then prints the row counts per table and the media plan: files, `_shared` files, links, avatars, custom emoji files and their size. It writes nothing. The real run prints the same report, and it matches the dry run.

### When it refuses

The command checks everything before it writes, and stops with `Merge refused: <reason>` when:

- The source and the target are the same database.
- The source or the target is not a SQLite or PostgreSQL database.
- The target's records say one of its backups is running, or, on SQLite, another process is writing to the target database. Stop the target install. If it is already stopped and the backup flag stays set, a run was cut off: start the target, let one backup finish, stop it and try again.
- The source database file, the target database file or the `--source-media` folder does not exist.
- Either archive is not at this release's newest schema revision. Upgrade both installs to the same release and start each once, or run `telegram-archive migrate` against it.
- The source has no account, or `--account` matches none, or two source accounts share the label you passed.
- An account on either side has never logged in, so it has no Telegram user id yet. Start that install once.
- A source account is the same Telegram account as one in the target. Two archives of one account are not merged.
- A chat ref or a per-account record key from the source already exists in the target.
- A file name in the target's media folder holds different bytes than the source file of the same name.
- The SQLite source's `-wal` file still holds changes and its folder is read-only. Start and stop the source install once, or copy the database with its `-wal` file to a writable folder.
- The source is SQLite, the target is PostgreSQL, and the source holds rows whose parent row it lacks, for example a reaction whose message is gone. SQLite keeps such rows and PostgreSQL refuses them. The reason lists the count per table. Run again with `--add-missing-parents`, described below.

The rows are then copied in one transaction, counted again, and the media files copied. If anything fails before the commit, the whole transaction is rolled back and the target database is as it was. Files copied before the failure stay: they are new names, never replacements, and the next run counts them as already there.

### Example

Two installs, each with one Telegram account. The target is the install you keep. Stop both first: nothing may write to either database during the merge. Take a backup of the target, see [Backing up the archive](backup-and-restore.md).

1. Copy the other install's `backups` folder, which holds its database and its `media` folder, under the target's data folder:

    ```bash
    docker compose stop telegram-backup telegram-viewer
    cp -a /srv/other-archive/data/backups ./data/incoming
    ```

2. Run the dry run and read the counts:

    ```bash
    docker compose run --rm telegram-backup \
      python -m telegram_archive merge --source /data/incoming/telegram_backup.db --dry-run
    ```

    ```text
    [DRY RUN] Merge plan, nothing written:
      Source account 1 -> target account 2
      Rows per table:
        accounts: 1
        users: 42
        chats: 17
        chat_folders: 2
        messages: 12000
        ...
      Media files copied: 310
      Shared files copied: 95
      Links created: 120
      Already in the target: 8
      Missing in the source folder: 0
      Avatar files copied: 25 (already there: 3, target's own kept: 0)
      Size: 1840.2 MB
    ```

3. Run it again without `--dry-run`. The heading becomes `Merge complete:`.

4. To keep backing up the merged account from the target install, declare it in `.env` as a second account, as [Going from one account to two](../configuration/multiple-accounts.md#going-from-one-account-to-two) describes, and copy its session file into the target's `data/session` folder under that account's session name. With the session file in place, the account needs no new login.

    ```bash
    cp /srv/other-archive/data/session/telegram_backup.session ./data/session/telegram_backup_account2.session
    ```

    ```dotenv
    TG_ACCOUNT_2_API_ID=87654321
    TG_ACCOUNT_2_API_HASH=fedcba9876543210fedcba9876543210
    TG_ACCOUNT_2_PHONE_NUMBER=+15550100002
    TG_ACCOUNT_2_LABEL=Work
    ```

    After login the archive finds the account's rows by its Telegram user id, so the index in `.env` does not have to match the account id the merge printed. The positions came across with the rows, so the next backup run continues where the source install stopped.

5. Start the stack. Once the viewer shows both accounts, remove `data/incoming`, which the target never reads.

A source in PostgreSQL is named by its URL, and its media folder with `--source-media`. The container must be able to reach that server:

```bash
docker compose run --rm telegram-backup \
  python -m telegram_archive merge --source postgresql://telegram:change-me@other-db:5432/telegram_backup \
  --source-media /data/incoming/media --dry-run
```

Without `--source-media`, the command looks for a `media` folder beside a SQLite source file. When there is none, the rows are merged and no file is copied, and the report says so.

`--account` merges one source account, by label or by account id. A label wins: a value made of digits picks the account with that label, and the account with that id only when no label matches. A transcript copied from a transcript of an account you left out keeps its text, and the report counts its link to the other copy as left empty. Users follow the rule above: only the ones the merged account points at are added.

### Rows whose parent is missing

A SQLite archive can hold rows whose parent row is gone: a message whose chat row is missing, a reaction or a media row whose message is missing, a folder entry whose folder is missing, or a reaction by a user no archive knows. SQLite keeps them. A PostgreSQL target refuses them, so the merge refuses such a source before it writes anything.

`--add-missing-parents` keeps every one of those rows. For each missing parent it adds an empty placeholder row to the target under the merged account's new id:

- a chat with an empty title, typed from its id: supergroup, group or private
- a message with no text, dated 1 January 1970, so it sits at the very top of its chat
- a folder with an empty title
- a user with only its id

The rows that point at them are then copied as they are. The report and the dry run list the placeholders per table under `Placeholder parent rows added`. The option works with a SQLite target too. Without it, a SQLite target takes the rows without parents, as the source held them.

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
