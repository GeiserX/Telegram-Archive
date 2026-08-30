# Merging two archives into one

This page is for one specific situation: **two people each ran their own
single-account install for years, and now want one multi-account 8.0 archive
that holds both histories.** One install keeps running and absorbs the other;
this page calls them the **target** and the **source** throughout.

If instead you have one archive and want to *add* a second Telegram account
that starts capturing from today, you do not need any of this — declare the
account and let it log in, as the
[multi-account quickstart](UPGRADING-8.0.md#multi-account-quickstart) shows.
The reason to merge is everything a fresh account cannot bring with it:

- **Deleted-message history.** Messages the source captured that have since
  been deleted on Telegram exist nowhere else. A fresh account can never
  recapture them; a merge carries them over, deletion markers and all.
- **Edit history.** Every stored version of every edited message comes along.
- **Downloaded media.** Files Telegram has since expired or that would take
  days to re-download are copied, not re-fetched.
- **Sync cursors.** The source's per-chat progress markers are imported, so
  the merged account's **first sweep is incremental** — it resumes each chat
  where the source install left off instead of re-capturing years of history
  from message 1.

The merge itself is two shell commands around one SQL script that you can read
line by line before running it — `scripts/merge/merge_sqlite.sql` or
`scripts/merge/merge_postgres.sql`. That is deliberate. This procedure runs
against the only copy of two people's Telegram history, so it is plain SQL you
can audit, not a tool you have to trust: every guard, every insert and every
verification is on the page, heavily commented, and the whole thing runs in a
single transaction that either completes every check or leaves the target
byte-for-byte untouched.

## What this page does not cover

- **Two archives of the same Telegram account.** That is message-level
  deduplication, a different problem, and the scripts refuse it outright when
  they see the same Telegram user id on both sides.
- **A multi-account source.** The source must hold exactly one account (the
  scripts check). To fold a third archive in, run this whole procedure again
  with the next source.
- **A PostgreSQL source into a SQLite target.** There is no
  PostgreSQL-to-SQLite mover in this project. Make the PostgreSQL install the
  target instead — see [Mixed backends](#mixed-backends).

## How the merge works

Since 8.0, every table holding Telegram data is keyed by the account that
captured it, and the `accounts` table maps each row of history to a Telegram
user id filled in at login. The merge script copies the source's account row
and all of its data into the target under a fresh account id. The next time
the target starts with the second account declared, `ensure_account` (in
`src/db/adapter.py`) matches the login's Telegram user id against the imported
row and **adopts it** — the second account picks up its entire imported
history, cursors included, as its own. Nothing about the matching depends on
env variable order or on which install the data came from.

Chats keep the opaque `ref` values minted for them in the source. Refs are
128-bit random tokens, so two independent archives cannot collide in practice
— and the script checks anyway and refuses on a collision rather than relying
on "cannot". Re-minting would buy nothing: the source viewer's share links die
with the source install either way.

## Before you start

**1. Upgrade both installs to the same 8.0.x release and start each one once,
then stop them.** The scripts refuse to run otherwise, for two concrete
reasons: the merge is written against the 8.0 schema (revision `023`) on both
sides, and the one-time start is what makes each install claim its account row
and record its Telegram user id — the value the whole adoption mechanism above
keys on. A source that never started under 8.0 would import as history no
login can ever claim.

**2. Stop all four containers — backup and viewer, on both installs.**

```bash
docker compose stop telegram-backup telegram-viewer   # on each machine
```

The scripts enforce what they can see (on PostgreSQL, any other connection to
either database aborts the merge; on SQLite, a concurrent writer makes the
transaction refuse to open), but a running viewer merely *reading* a SQLite
file is invisible to them — stopping everything is on you.

**3. Back up both databases.** The merge is guarded and roll-back-safe, but a
merge you later regret — wrong target, wrong direction — is only undone by
restoring a backup. Same commands as the
[8.0 upgrade](UPGRADING-8.0.md#before-you-upgrade):

```bash
# SQLite (after a clean stop; if -wal or -shm files sit beside the db, copy them too)
cp /path/to/data/backups/telegram_backup.db* /somewhere/safe/

# PostgreSQL
docker exec telegram-postgres pg_dump -U telegram telegram_backup > pre-merge.sql
```

**4. Pick the target.** The target keeps its container names, viewer URL,
viewer accounts and settings; the source install is retired afterwards. If one
install is PostgreSQL and the other SQLite, the PostgreSQL one is the target —
see [Mixed backends](#mixed-backends).

Expect the target database to grow by roughly the size of the source database,
and the media directory by the size of the source's media (minus whatever both
already had from shared chats).

## The merge: SQLite target, SQLite source

Copy the source's database file to the target machine first (its install was
cleanly stopped in step 2, so the file is complete without sidecar files; if
`-wal`/`-shm` files exist anyway, copy them alongside).

Run the script **interactively** — it deliberately does not commit. You need
the `sqlite3` command-line shell, version 3.33.0 or newer — the script uses
`.mode box` for its report (`apt install sqlite3` on Debian/Ubuntu; macOS
ships one):

```bash
cd telegram-archive          # the checkout containing scripts/merge/
sqlite3 /path/to/data/backups/telegram_backup.db
```

```
sqlite> .bail on
sqlite> ATTACH DATABASE '/path/to/copied/source/telegram_backup.db' AS source;
sqlite> .read scripts/merge/merge_sqlite.sql
```

The script checks every precondition, imports, re-counts everything it
imported, and ends with a one-line verdict. **Nothing is saved yet.** The last
word is yours, and it is one of exactly two:

```
sqlite> COMMIT;      -- only if the verdict line says OK
sqlite> ROLLBACK;    -- in every other case, or whenever in doubt
```

Quitting sqlite3 without typing `COMMIT;` also rolls everything back — there
is no state in between. If any check fails, the script stops at that check
with a `MERGE ABORTED:` message naming exactly what is wrong and what to do
about it; type `ROLLBACK;` (or just quit) and the target is untouched.

What a good run prints: a per-table list of expected vs actual imported row
counts (all `ok`), a summary with the imported account's row id and a
`media roots match` indicator, and then:

```
VERDICT: OK — every check passed. Type COMMIT; now to keep the merge.
```

## The merge: PostgreSQL target, PostgreSQL source

The script reads the source through `postgres_fdw`, which the stock postgres
image ships. The simplest way to give it a source is to restore the source's
dump as a scratch database on the target's own PostgreSQL server:

```bash
# On the source machine (containers stopped; the postgres server can stay up):
docker exec telegram-postgres pg_dump -U telegram telegram_backup > source-archive.sql
# copy source-archive.sql to the target machine, then on the target:
docker exec telegram-postgres psql -U telegram -d postgres -c 'CREATE DATABASE ta_merge_source'
docker exec -i telegram-postgres psql -U telegram -d ta_merge_source -f - < source-archive.sql
```

(If both installs can reach each other on the network you can skip the dump
and point `SRC_HOST`/`SRC_PORT` at the source's live PostgreSQL server instead
— with its containers stopped. The script verifies nobody else is connected on
either side.)

Then run the merge (`SRC_PASSWORD` is the source *server's* password — for the
scratch-database route above, that is the target server's own one):

```bash
read -rs SRC_PW    # type the source password; it stays out of shell history
docker exec -i telegram-postgres psql -U telegram -d telegram_backup \
  -v SRC_HOST=127.0.0.1 -v SRC_PORT=5432 -v SRC_DB=ta_merge_source \
  -v SRC_USER=telegram -v SRC_PASSWORD="$SRC_PW" \
  -f - < scripts/merge/merge_postgres.sql
```

`read -rs` keeps the password out of your shell history; it still appears
briefly in the container's process arguments while psql runs, which on a
single-admin machine is usually acceptable. On a shared machine, prefer the
scratch-database route above — restoring the dump into the target's own
cluster means the only password involved is the target's own.

Unlike the SQLite script, this one commits itself — but only if it gets to the
end. The whole run is a single transaction; every precondition and every
verification raises an error on failure, and any error rolls the entire
transaction back, so the only two outcomes are `MERGE COMMITTED` with a clean
per-table count report, or an explicit `MERGE ABORTED:`/`MERGE FAILED
VERIFICATION:` message with the target untouched. There is no partial state.

Afterwards, drop the scratch database:

```bash
docker exec telegram-postgres psql -U telegram -d postgres -c 'DROP DATABASE ta_merge_source'
```

## What the scripts refuse, on purpose

Each of these stops the merge before it writes anything, with a message naming
the condition:

- Source or target not at schema revision `023` (the revision these scripts
  were written for — use the scripts from the release you are running).
- A target or source account row that never completed a first start under 8.0
  (no Telegram user id recorded).
- A source holding more than one account, or rows outside its one account.
- **The same Telegram user id on both sides** — two archives of one account
  are a deduplication problem, not a merge, and are refused with no override.
- A chat ref present on both sides.
- (PostgreSQL) any other client connected to either database.

## Media files

The database rows are merged by the script; the files they point at are copied
by you. Media lives under `<BACKUP_PATH>/media/` — with the stock compose
file, `./data/backups/media/` on the host — as one directory per chat plus a
`_shared/` store of deduplicated blobs that the chat directories symlink into
(relatively, so the links survive copying).

Copy the source's tree into the target's, **never overwriting what the target
already has**:

```bash
rsync -a --ignore-existing /path/to/source/data/backups/media/ /path/to/target/data/backups/media/
# or, without rsync:  cp -an /path/to/source/data/backups/media/. /path/to/target/data/backups/media/
# (BSD/macOS cp exits 1 when it skips already-existing files — the skips are
#  the point here, so judge the copy by its output, not its exit code)
```

`--ignore-existing` matters for chats both accounts were in: those directories
already exist in the target, and filenames collide precisely where both
installs downloaded the same Telegram file — identical content, so keeping the
target's copy is always right. Everything only the source had (its own chats'
directories, its `_shared` blobs) copies over normally.

The database stores media paths in two shapes. Rows the API sweep and the
realtime listener wrote are absolute container paths; rows a Telegram Desktop
import wrote are relative to the media root (`<chat_id>/<filename>`). The
relative ones carry no base at all, so they survive a `BACKUP_PATH` change
untouched and never need the rewrite below.

If both installs used the default `BACKUP_PATH=/data/backups`, the absolute rows
point at the right places as soon as the files are copied — the script's
`media roots match: yes` line confirms it. If it says `NO`, the installs used
different `BACKUP_PATH`s, and those rows need their prefix rewritten once
(adjust the two literals; the imported account's row id is in the merge
summary). The `LIKE` guard leaves the relative rows alone, which is correct:

```sql
-- Same statement on both backends (replace() exists in SQLite and PostgreSQL)
UPDATE media SET file_path = replace(file_path, '/old/backups/media/', '/data/backups/media/')
WHERE account_id = 2 AND file_path LIKE '/old/backups/media/%';
```

## The session file: move it, never copy it

The source person's Telegram login lives in a Telethon session file. It must
be **moved** — if two processes ever run on the same session, Telegram
invalidates the key (`AuthKeyDuplicatedError`) and kills the login for both
copies. Moving it, and never starting the retired source install again, makes
that impossible; the reward is that the second account joins the target
without a new login.

Sessions live in `SESSION_DIR` (default: the `session/` directory next to
`backups/` — `./data/session/` on the host with the stock compose file). Move
the source's file to the target under the second account's default session
name:

```bash
mv /path/to/source/data/session/telegram_backup.session \
   /path/to/target/data/session/telegram_backup_account2.session
```

(A `.session-journal` sitting beside it after an unclean stop moves along
under the same rename. If the source used a custom `SESSION_NAME`, that is the
file to move.)

## Declare the second account

In the target's environment, name both accounts by index — account 1 is the
target's own credentials, account 2 the source person's API credentials (from
[my.telegram.org](https://my.telegram.org), the same values the source install
used):

```yaml
environment:
  TG_ACCOUNT_1_API_ID: "1234567"            # the target's existing values
  TG_ACCOUNT_1_API_HASH: "target_api_hash_here"
  TG_ACCOUNT_1_PHONE_NUMBER: "+1234567890"

  TG_ACCOUNT_2_API_ID: "7654321"            # the source install's values
  TG_ACCOUNT_2_API_HASH: "source_api_hash_here"
  TG_ACCOUNT_2_PHONE_NUMBER: "+1987654321"
  TG_ACCOUNT_2_LABEL: "their-name"          # optional display name
```

Index 2's session file defaults to `telegram_backup_account2` — the name the
move above used, so no `TG_ACCOUNT_2_SESSION_NAME` is needed. The index is
only an env coordinate: accounts are matched to their history by Telegram user
id at login, so even swapping the indexes later would not misfile a single row.

## First start, and what to check

Start the backup container alone and watch the logs:

```bash
docker compose up -d telegram-backup
docker compose logs -f telegram-backup
```

A good first start looks like: migrations report nothing to do, both accounts
connect **without a login prompt** (account 2 is riding the moved session),
and the sweep runs the accounts one after the other. Account 2's first sweep
is the point of the whole exercise: it should be roughly as quick as the
source install's regular nightly run was — the imported cursors mean it picks
up each chat where the source left off, plus the gap between the source's last
run and now. A full-length re-capture of everything means the cursors did not
come across, which the merge's count checks make effectively impossible — but
it is the symptom worth knowing.

Then start the viewer. The imported account's chats appear alongside the
target's own, with their media, edit histories and deleted messages intact. As
always, the logs report counts only, never chat ids or titles.

## Viewer access after the merge

The source install's viewer accounts, sessions and share tokens are **not
imported**, deliberately. They are the retiring install's identities and
grants — password hashes, live session tokens and share links whose trust was
rooted in that install and whose URLs are dead now anyway. Silently carrying
them into an archive that now holds two people's history would move access
nobody re-decided. The target's own viewers keep working unchanged (their
grants say nothing about the new account, and permissions fail closed); to
give anyone access to the imported chats, grant it on the target explicitly —
create the viewer or mint the share token there, scoped to the new account's
chats.

## Mixed backends

**SQLite source into a PostgreSQL target** works, with one conversion first.
`scripts/migrate-sqlite-to-postgres.py` is 8.0-schema-aware — it copies
through the current models, `accounts` first, so `account_id` keys and the
recorded Telegram user id survive — but it is a whole-archive *mover*, not a
merge: pointed at a non-empty database it would overwrite rows, so it must
only ever target an **empty scratch database**, never your archive. The order:

```bash
# 1. A scratch database on the target's PostgreSQL server:
docker exec telegram-postgres psql -U telegram -d postgres -c 'CREATE DATABASE ta_merge_source'

# 2. Move the source SQLite archive into it (run wherever this repo's Python
#    runs and the postgres port is reachable — the script's own header shows a
#    docker run variant if you have no local Python environment):
python scripts/migrate-sqlite-to-postgres.py \
  --sqlite /path/to/copied/source/telegram_backup.db \
  --postgres postgresql+asyncpg://telegram:your-postgres-password@127.0.0.1:5432/ta_merge_source

# 3. Stamp the schema revision. The mover provisions the full 8.0 schema but
#    records no Alembic revision (the app's entrypoint normally does that);
#    the scratch database genuinely is at the 8.0 shape, so say so:
docker exec telegram-postgres psql -U telegram -d ta_merge_source -c \
  "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)); INSERT INTO alembic_version VALUES ('023')"

# 4. Run the PostgreSQL merge exactly as above, with SRC_DB=ta_merge_source,
#    then drop the scratch database.
```

The mover verifies its own copy (per-table source-vs-target counts) before you
ever reach the merge, and the merge's guards then run against the scratch copy
like against any other source.

**PostgreSQL source into a SQLite target** is not supported: no
PostgreSQL-to-SQLite mover exists in this project, and pretending otherwise
here would help nobody. Make the PostgreSQL install the target and merge the
SQLite archive into it as just described — if the "wrong" person's hardware
hosts PostgreSQL, moving a docker volume to the preferred machine is a far
smaller job than a merge in an unsupported direction.

## If it fails, and the way back

A failed or aborted run changes nothing — that is the design, not a promise:
one transaction per backend, and every guard fires before `COMMIT` is
reachable. On SQLite you additionally hold the commit yourself; when in doubt,
`ROLLBACK;`. A *committed* merge is undone only by restoring the backups from
step 3 — which is why step 3 is not optional. The one asymmetric caution:
never start the retired source install again once its session file has moved
— two processes on one session kill the login for both.
