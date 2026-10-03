# SQLite and PostgreSQL

Pick a database, point both containers at it, and move an existing SQLite archive to PostgreSQL. This page also covers migrations and search.

## Which database

The archive uses SQLite by default. The database is one file, `/data/backups/telegram_backup.db`, with `-wal` and `-shm` files beside it while it is in use. PostgreSQL is optional. Both backends support [live updates](../viewer/live-updates.md) and full-text search. They differ only in how updates reach the viewer. On SQLite the backup sends them to the viewer over HTTP. On PostgreSQL it uses the database's `LISTEN/NOTIFY`.

| | SQLite | PostgreSQL |
|---|---|---|
| Setup | Nothing to do | One more container, or an external server |
| Where the data lives | A file under `./data` | The PostgreSQL server |
| Backup | Copy the file with both containers stopped | `pg_dump` |
| Viewer's `/data` mount | Must be read-write | May be `:ro` |

!!! warning "Both containers need the same database settings"
    The backup and the viewer must resolve to the same database, or the viewer shows no data. The viewer has no `env_file`. It only receives the variables listed in its `environment:` block, which covers `DATABASE_URL`, `DB_TYPE`, `DB_PATH` and the `POSTGRES_*` variables.

    Setting `DATABASE_DIR` or `DATABASE_PATH` only in `.env` is the usual cause. Use `DB_PATH`, which both containers receive. [Run with Docker](../getting-started/docker.md#what-the-stock-compose-file-does) explains why.

## How the database location is resolved

The app, Alembic and the backup image's entrypoint all use the same order. The first rule that matches wins.

1. `DATABASE_URL`, if set.
2. Otherwise, if `DB_TYPE` is `postgresql` or `postgres`, PostgreSQL. Upper and lower case both work. The URL is built from the five `POSTGRES_*` variables below.
3. Otherwise SQLite, at the first of these that is set: `DATABASE_PATH`, then `DATABASE_DIR/telegram_backup.db`, then `DB_PATH`, then `BACKUP_PATH/telegram_backup.db`. The path is made absolute.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | empty | Full URL. Beats every other database setting. |
| `DB_TYPE` | `sqlite` | `sqlite`, `postgresql` or `postgres`. Used only when `DATABASE_URL` is empty. |
| `DB_PATH` | `$BACKUP_PATH/telegram_backup.db` | SQLite file. The stock compose file sets `/data/backups/telegram_backup.db` in both services. |
| `DATABASE_PATH` | unset | Older full-path variable for SQLite. |
| `DATABASE_DIR` | unset | Older directory variable for SQLite. |
| `POSTGRES_HOST` | `localhost`, `postgres` in the compose file | Server host. |
| `POSTGRES_PORT` | `5432` | Server port. |
| `POSTGRES_USER` | `telegram` | Database user. |
| `POSTGRES_PASSWORD` | empty | Database password. |
| `POSTGRES_DB` | `telegram_backup` | Database name. |

The full list with scope per container is in [Environment variables](../reference/environment-variables.md).

The app percent-encodes `POSTGRES_USER` and `POSTGRES_PASSWORD` when it builds the URL. In a `DATABASE_URL` you write yourself, percent-encode special characters in the password yourself.

`DATABASE_URL` accepts these schemes:

- `sqlite:///` and `sqlite+aiosqlite:///`
- `postgresql://`, `postgresql+asyncpg://` and `postgres://`

An absolute SQLite path needs four slashes. Three slashes make the path relative to `/app`, which is not the data volume and is read-only under the stock compose file.

```bash
DATABASE_URL=sqlite:////data/backups/telegram_backup.db
DATABASE_URL=postgresql://telegram:change-me@postgres:5432/telegram_backup
```

The backup image refuses to start with `ERROR: unrecognised database configuration` for any other scheme, such as `postgresql+psycopg2://`. It also refuses any `DB_TYPE` that is set to something other than `sqlite`, `postgres` or `postgresql`.

## SQLite settings

- The database runs in write-ahead log (WAL) mode with `synchronous=NORMAL`. On a read-only file the app skips WAL and logs a warning.
- `DATABASE_TIMEOUT` sets how long a connection waits for a lock, in seconds. The default is 60. Invalid, zero or negative values fall back to 60. It has no effect on PostgreSQL. Raise it on slow disks if the logs show `database is locked`.
- The viewer needs write access to the `/data` mount. It writes to the WAL files, the shared `.push-secret` file for live updates and the thumbnail cache.
- `DB_ECHO=true` logs every SQL statement. Only the literal `true`, in any case, turns it on.

`DATABASE_TIMEOUT` and `DB_ECHO` are not in the viewer's `environment:` block. Add them there if the viewer should use them too.

## PostgreSQL with the stock compose file

The shipped `docker-compose.yml` carries a PostgreSQL service in comments. To turn it on:

1. Uncomment the `postgres` service. It runs `postgres:18-alpine` as the container `telegram-postgres`. Its data lives in the named volume `postgres_data` at `/var/lib/postgresql`. A `pg_isready` healthcheck reports when it is ready.
2. Uncomment the top-level `volumes:` block with `postgres_data:` at the end of the file.
3. Uncomment `depends_on` in both `telegram-backup` and `telegram-viewer`, so each waits for the database to be healthy:

    ```yaml
        depends_on:
          postgres:
            condition: service_healthy
    ```

4. Set the backend and a password in `.env`. The `postgres` service refuses to start without `POSTGRES_PASSWORD`.

    ```bash
    DB_TYPE=postgresql
    POSTGRES_PASSWORD=change-me
    ```

5. Start the stack:

    ```bash
    docker compose up -d
    ```

The compose file defaults `POSTGRES_HOST` to `postgres`, the service name. The code default is `localhost`, which matters only outside the compose file. The database variables are already in the viewer's `environment:` block, so both containers pick up the values from `.env`.

On the first start the backup creates every table. Check the schema revision with:

```bash
docker compose exec telegram-backup alembic current
```

### An external PostgreSQL server

- Set `DATABASE_URL` or the `POSTGRES_*` variables in both services.
- The database user must be allowed to run `CREATE EXTENSION pg_trgm`. The schema needs it.
- The backup's entrypoint tries to reach the server 30 times, 2 seconds apart. If the server never answers, it exits.
- Each process keeps a fixed connection pool of 5, plus up to 10 overflow connections. It is not configurable.
- The viewer's `/data` mount may be read-only, `./data:/data:ro`, because no database files live there.

## Migrations

The backup image's entrypoint runs Alembic on every start, except for the `auth` command.

- On SQLite, when the database file does not exist yet, it creates it directly at the newest revision.
- Some databases have tables but no `alembic_version` table. For these, the entrypoint works out the revision from the tables it finds and records it, up to revision 018. Then it upgrades to the newest revision. Every revision after 018 can run safely on a schema that already has its changes.
- On PostgreSQL the entrypoint takes a database lock first, so two containers never migrate at the same time.
- Migration 022, which rebuilds every table, refuses to run while another process holds a non-empty SQLite file. The container exits with a message asking you to stop the viewer. Under the stock compose file the restart policy retries it, and it runs once the file is free.

The viewer never migrates. On PostgreSQL it has no tables until the backup has run once, so it answers with errors until then. During an upgrade it also answers with errors for the seconds a migration takes.

A pip install has no entrypoint. Run `telegram-archive migrate` yourself, before any other command and after every upgrade. [Install from PyPI](../getting-started/pip.md) explains why the order matters.

To check or run migrations by hand:

=== "Docker"

    ```bash
    docker compose exec telegram-backup alembic current
    ```

=== "From a checkout"

    ```bash
    alembic -c telegram_archive/alembic.ini current
    alembic -c telegram_archive/alembic.ini upgrade head
    ```

=== "pip"

    ```bash
    telegram-archive --data-dir ./data migrate
    ```

The newest revision is `038`, in 9.0.0. There is no supported downgrade. To go back to an older release, restore the backup you took before upgrading. See [Upgrading](../operations/upgrading.md).

## Search

Both backends index message text for word search.

- On SQLite, an FTS5 full-text table that ignores accents, so `cafe` also finds `café`.
- On PostgreSQL, a stored `tsvector` column with a GIN index.

Voice transcripts get the same kind of index. The upgrade that adds the index also indexes every existing message, so it takes longer on a large archive.

If the SQLite build has no FTS5, the migration skips the index. Message search then falls back to plain substring matching, and transcript search stops working.

![Global search with message hits in the sidebar and the chat opened at the clicked hit](../images/screenshots/search-results.png)

## Move an existing SQLite archive to PostgreSQL

The backup image ships a mover script. The image entrypoint migrates the empty PostgreSQL database to the newest revision first. The script then copies every table except `avatar_history` and checks the counts.

1. Stop both containers:

    ```bash
    docker compose stop telegram-backup telegram-viewer
    ```

2. Back up `data/`. See [Backing up the archive](../operations/backup-and-restore.md).
3. Check that `data/backups/telegram_backup.db-wal` is missing or empty. SQLite writes that file into the database when the last connection closes. The mover reads only the database file, so anything still in the `-wal` file would be lost.
4. Turn on PostgreSQL as in [PostgreSQL with the stock compose file](#postgresql-with-the-stock-compose-file), but start only the database. It must be empty.

    ```bash
    docker compose up -d postgres
    ```

5. Find the compose network. By default it is named after the project directory, for example `telegram-archive_telegram-network`:

    ```bash
    docker network ls
    ```

6. Run the mover from the backup image. Use the same tag your compose file pins, and your own network name and password:

    ```bash
    docker run --rm -it \
      --network telegram-archive_telegram-network \
      -v ./data/backups/telegram_backup.db:/sqlite.db:ro \
      -e DATABASE_URL=postgresql://telegram:change-me@postgres:5432/telegram_backup \
      drumsergio/telegram-archive:9.2.1 \
      python scripts/migrate-sqlite-to-postgres.py --sqlite /sqlite.db
    ```

7. Switch both containers to PostgreSQL. With the stock compose file that means `DB_TYPE=postgresql` and `POSTGRES_PASSWORD` in `.env`, and `depends_on` uncommented. Then start them:

    ```bash
    docker compose up -d
    ```

    Because the mover runs through the image entrypoint, the empty PostgreSQL database is migrated to the newest revision before the copy starts. `alembic_version` is already at head when the backup container starts. If you run the mover outside the image, with a pip install or `--entrypoint python`, the database has no `alembic_version`. On its first start the backup then works out the revision from the schema, records it and upgrades.

8. Check the result. `docker compose exec telegram-backup alembic current` shows the newest revision, and the viewer lists your chats and their messages. If the real-time listener is on, a new message also arrives as a [live update](../viewer/live-updates.md).

Mover options:

| Option | Default | Effect |
|---|---|---|
| `--sqlite PATH` | looked up | SQLite file to read. |
| `--postgres URL` | from the environment | PostgreSQL URL to write to. |
| `--batch-size N` | `1000` | Rows per batch. |
| `--dry-run` | off | Show what would be copied, write nothing. |
| `--verify-only` | off | Only compare row counts between the two databases. |

Without `--sqlite`, the mover takes the first existing file from `SQLITE_PATH`, `DATABASE_PATH`, `DATABASE_DIR/telegram_backup.db` and `DB_PATH`. Then it tries `/data/db/telegram_backup.db`, `/data/backups/telegram_backup.db` and `./telegram_backup.db`. Without `--postgres`, it uses `DATABASE_URL` when that contains `postgresql`, otherwise the `POSTGRES_*` variables, where `POSTGRES_HOST` is required.

It copies each table in primary-key order and merges rows, so a row that already exists in the target is overwritten. Afterwards it resets the `media_transcripts`, `media_versions`, `reaction_history` and `message_snapshots` id sequences and compares row counts table by table. The `reactions` sequence is not reset. The first new reaction after the move triggers the automatic recovery described in [Duplicate key errors on reactions](#duplicate-key-errors-on-reactions).

!!! warning "Before you run the mover"
    - Point it only at an empty database.
    - Keep the backup container stopped for the whole copy. Rows written or deleted during the copy can be missed.
    - The mover does not copy avatar history. The avatar files stay on disk and the current avatar carries over, but the viewer no longer lists earlier profile photos.
    - There is no way back from PostgreSQL to SQLite. Keep the SQLite file.

To combine two archives, SQLite or PostgreSQL in any mix, use `telegram-archive merge`. See [Merge two archives](../operations/maintenance.md#merge-two-archives).

## PostgreSQL backups

Back up a PostgreSQL archive with `pg_dump`, together with the media and session folders. [Backing up the archive](../operations/backup-and-restore.md) has the commands and the restore steps.

## Duplicate key errors on reactions

On PostgreSQL the `reactions` id sequence can fall behind the rows in the table, for example after a restore. Inserts then fail with `duplicate key value violates unique constraint "reactions_pkey"`. The backup detects this, resets the sequence and retries. You do not need to do anything.

To fix it by hand, run the same statement, which is also in `scripts/fix_reactions_sequence.sql`:

```bash
docker exec telegram-postgres psql -U telegram -d telegram_backup \
  -c "SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false);"
```
