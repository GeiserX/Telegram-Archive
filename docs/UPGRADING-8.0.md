# Upgrading to 8.0.0

8.0.0 is the release that lets one archive hold more than one Telegram account.
Making that safe meant rewriting the database once, and changing every URL the
viewer serves. Both are one-way changes, so read this page before you start.

The upgrade itself is short — on a million messages the rewrite takes about
seven seconds — but it happens on what is usually the only copy of your Telegram
history, so the two minutes of preparation below are worth taking.

## What changes for you

- **Your archive is rewritten once.** Every chat, message, media row, sync
  cursor, forum topic and folder gains the account that captured it. Migration
  `022` does this in a single transaction, and everything you already have
  becomes account 1.
- **Nothing on disk moves.** Media files stay exactly where they are, under the
  same paths. Only the database changes.
- **Every viewer URL changes shape.** Chats are addressed by an opaque ref
  instead of a chat id. Your old bookmarks and share links stop working.
- **You do not have to configure anything.** A single-account install keeps
  running as it is, with the account you already logged in with.
- **You cannot migrate back down.** Restoring a backup is the way back, which is
  why the first step below is taking one.

## Before you upgrade

**1. Stop both containers — the viewer as well as the backup.**

```bash
docker compose stop telegram-backup telegram-viewer
```

Stopping comes first for the backup's sake too: a running SQLite database keeps
recent commits in its write-ahead log, and copying only the `.db` file while it
runs would silently miss them. After a clean stop the log is folded back into
the file.

**2. Back up the database.**

This is the one step you cannot go back and take afterwards. Copy the file (or
take a `pg_dump`) somewhere off the machine if you can:

```bash
# SQLite — the default. Adjust the path to your mounted volume. If -wal or
# -shm files still sit beside the database, copy those too.
cp /data/backups/telegram_backup.db* /somewhere/safe/

# PostgreSQL (the server container can stay up for the dump)
docker exec telegram-postgres pg_dump -U telegram telegram_backup \
  > telegram_backup.pre-8.0.sql
```

Media is untouched by the upgrade, so it does not need backing up for this.

On SQLite this is required, not advisory. The migration rebuilds every table,
and it refuses to start while another process holds the database open — a viewer
left running would come up serving a schema that was replaced underneath it.
If you forget, nothing breaks: the migration stops before writing anything, tells
you to stop the viewer, and the next start goes through once you have. It
self-heals, but it costs you a container restart.

**3. Check free disk space — SQLite only.**

You need **at least three times the size of the database file** free on the same
filesystem. The rebuild itself peaks at about 2.4 times the file: SQLite's
write-ahead log holds the whole rewrite and cannot be checkpointed away while the
transaction is open. Three times is that measurement plus a margin, and the
migration refuses to run below it rather than filling your disk halfway through.

```bash
du -h /path/to/data/telegram_backup.db
df -h /path/to/data
```

The space is transient — see [Time and space expectations](#time-and-space-expectations)
for what stays. On PostgreSQL there is no such requirement; the database barely
grows.

## The upgrade itself

**1. Move your image pins to 8.0.0.** If you use the compose file this repo
ships, `git pull` brings the new pins. If you keep your own, edit both:

```yaml
services:
  telegram-backup:
    image: drumsergio/telegram-archive:8.0.1
  telegram-viewer:
    image: drumsergio/telegram-archive-viewer:8.0.1
```

Use 8.0.1 or later, not 8.0.0: the first production archive to attempt this
upgrade was refused by 8.0.0 over messages whose chat no longer existed —
history that Telegram's group→supergroup renumbering leaves behind in any
sufficiently old archive — and 8.0.1 upgrades those archives instead.

**2. Start the backup container on its own**, and let it migrate:

```bash
docker compose up -d telegram-backup
docker compose logs -f telegram-backup
```

**3. Watch the migration finish before starting anything else.** It runs at
startup, before the archive does any work.

**4. Start the viewer** once the migration has completed:

```bash
docker compose up -d telegram-viewer
```

## What it looks like when it worked

In the backup container's logs, migration `022` — the data rewrite — runs and
completes, followed by `023`, a small PostgreSQL-only search-index migration, then
the archive starting its normal schedule. The migration reports counts — rows
moved, viewer grants converted — and never chat ids, titles or message content,
like everything else this project logs.

In the viewer, all your chats are there, unchanged, now belonging to account 1.
Existing viewer accounts, sessions and share tokens keep working with the same
restrictions they had: migration `022` converts every restricted grant as part
of the rewrite, so nobody has to be set up again.

The one thing that will look wrong is a bookmark. Every chat URL changed, so an
old one no longer resolves. Open the chat from the sidebar and bookmark it again.

You may also find chats with no title in the sidebar that you have never seen
before. Those are recovered history: messages whose chat vanished from Telegram
years ago — group→supergroup migrations leave exactly this behind — were
unreachable in 7.x, and the upgrade gives each such orphaned chat id a
placeholder chat so its messages can be served again. The migration reports
how many it created, as a count.

## If it fails

**An interrupted upgrade leaves your 7.x database intact.** The migration is a
single transaction on both SQLite and PostgreSQL, so a failure at any point —
out of disk, a crash, a power cut — rolls the whole thing back. The database's
logical contents are exactly what they were (the file's bytes and its sidecar
files may differ — that is normal), and the 7.x image you were running still
runs it. Pin
back to 7.33.6, start it, and you are exactly where you were.

Two failures stop the migration before it writes anything at all, and both say
so explicitly and end with "Nothing has been changed":

- **Another process is holding the database.** Stop the viewer container, and
  anything else pointed at the same file, then start the backup container again.
- **Not enough free disk space.** The message gives you both numbers — what it
  needs and what is free. Free some space and start the container again.

If 8.0.0 refused your archive with a foreign-key violation on
`fk_messages_chat`, that is the one failure that was the migration's fault
rather than your machine's: your archive holds messages whose chat no longer
exists, and 8.0.0 refused them where it should have recovered them. Your
database is intact — the transaction rolled back — and the fix is to upgrade
with 8.0.1 or later instead, which turns exactly that refusal into recovered
history.

**A completed upgrade cannot be migrated back below `022`.** Reversing the
search-index migration (`023`) works; `alembic downgrade` past `022` refuses,
deliberately. Once a second account exists the old keys cannot
identify a row: two messages that differ only by which account captured them
collapse onto a single key, so a downgrade would have to choose whose copy of
every shared message, folder and edit history to destroy. Rather than write
something that quietly picks one, the downgrade raises and tells you to restore
the backup you took before the upgrade. That backup is the only honest way back,
and it is why this page opens with it.

## Multi-account quickstart

You do not need this to upgrade. A single-account install needs no configuration
change at all: with no indexed account declared, account 1 is built from the
`TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE` you already have,
and it adopts the session you already logged in with. Your archive and your login
both carry over — there is no re-authentication.

To add a second account, declare them by index instead:

```yaml
environment:
  # Account 1 — your existing archive and session, now named explicitly
  TG_ACCOUNT_1_API_ID: "1234567"
  TG_ACCOUNT_1_API_HASH: "your_api_hash_here"
  TG_ACCOUNT_1_PHONE_NUMBER: "+1234567890"

  # Account 2 — a second Telegram account in the same archive
  TG_ACCOUNT_2_API_ID: "7654321"
  TG_ACCOUNT_2_API_HASH: "another_api_hash_here"
  TG_ACCOUNT_2_PHONE_NUMBER: "+1987654321"
```

Three things to know about how this behaves:

- **Account 1 keeps everything.** It is the account your existing history was
  migrated to, and it reuses your existing session file, so it does not ask you
  to log in again. Declaring it explicitly changes nothing about it.
- **Each account gets its own session file.** A new account needs its own login
  the first time it runs, exactly like your first one did.
- **Sweeps run one account after another, not at once.** Two accounts hitting
  Telegram in parallel is how you get rate-limited on both, so a scheduled run
  works through the accounts in order. A run therefore takes about as long as
  the accounts' runs added together.

Each account keeps its own chats, its own sync cursors and its own folder
numbering, and they cannot overwrite each other — that is what the database
rewrite was for.

## Viewer changes

**URLs are refs now.** Every chat-scoped route, the WebSocket protocol, and
every media, thumbnail and avatar URL addresses a chat by an opaque ref minted
per chat, instead of by its Telegram chat id. That is a privacy improvement —
no chat id reaches a URL, your browser history, or a reverse proxy's access log
— and it is why old links break. Old bookmarks and previously shared links no
longer resolve; open the chat from the sidebar to get a current URL, and mint
share links again.

A ref that does not exist, one that is malformed, and one belonging to a chat
you may not see all answer with the same 404, so nothing distinguishes a chat
you are not allowed to see from one that is not there.

**Permissions read from new fields, and they fail closed.** A viewer, session or
share token is now restricted by `allowed_accounts` and `allowed_chat_refs`:

- **`null` means unrestricted** — everything in the archive.
- **A list means exactly those and nothing else.**
- **Anything unparseable means nothing at all** — no access, rather than a guess.

Migration `022` converts your existing restricted viewers, sessions and share
tokens into these fields, so grants you already set up keep working. The old
`allowed_chat_ids` field is never read as a grant again; 8.0.0 keeps writing a
deny-only marker into it so that rolling back to a 7.x image cannot accidentally
widen someone's access.

If you create or update viewers through the admin API rather than the UI, send
`allowed_chat_refs` (and `allowed_accounts`) instead of `allowed_chat_ids`. A
write still carrying the old field is rejected with a 400 that names the
replacement, rather than being reinterpreted — an old `[123, 456]` read under the
new meaning would grant access rather than deny it, so it is refused instead.
`POST /api/push/subscribe` takes `chat_ref` for the same reason; browsers
resubscribe on their own, so this only affects your own scripts.

## Time and space expectations

Measured on NVMe: **about 7 seconds per million messages** to migrate. A SQLite
database ends up **about 1.45 times** its previous size, permanently — the ratio
held across a fourfold change in archive size, so you can scale it to yours.
PostgreSQL barely grows at all.

- **SQLite**, 400k messages: 152 MiB → 218 MiB in 2.2 s, peaking at 356 MiB.
- **SQLite**, 1.6M messages: 611 MiB → 880 MiB in 11.9 s, peaking at 1442 MiB.
- **PostgreSQL**, 1.6M messages: 601 MiB → 605 MiB in 5.5 s.

The peak is transient — it is the write-ahead log, which SQLite folds back into
the database when the migration's connection closes (the entrypoint exits it
before the archive starts). The growth that stays is the permanent one, and it
is the account key now present in every row and every index.

On a spinning disk, a NAS, or a network mount, expect this to take
proportionally longer — the rewrite is bound by how fast the disk can write the
whole database again. On PostgreSQL the migration holds an exclusive lock for its
duration, so that time is startup downtime for the archive rather than
background work.
