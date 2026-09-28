# Install from PyPI

This page runs the backup and the viewer from the Python package, without Docker. Run `migrate` before any other command, as [First run](#first-run) shows.

## Availability

| What | Name |
|------|------|
| PyPI package | `telegram-archive` |
| Command | `telegram-archive` |
| Python module | `telegram_archive` |

PyPI carries the package from 8.17.0 on. Earlier versions exist only as Docker images.

It requires Python 3.14 or newer.

The wheel holds only the `telegram_archive` package with its templates, static files and Alembic migrations. The repository's `scripts/` folder and the old `src` alias are not in it. See [repository scripts](../reference/cli.md#repository-scripts) for how to run those.

## System tools

Put `ffmpeg` and `ffprobe` on your `PATH`. The backup and the viewer use them for two things:

- Video thumbnails, in the viewer and during the backup.
- [Voice transcription](../configuration/transcription.md), to check for an audio stream and to extract the audio track of videos and documents.

Without them, no command fails. Videos get no thumbnail, and transcription sends the whole stored file instead of its audio track.

`DOWNLOAD_DOCUMENT_MIME_TYPES` uses the system's list of file types, such as `/etc/mime.types`, to also match documents by file extension. For a type missing from that list, it matches only documents that declare that type, and the backup logs a warning. See [Media downloads](../configuration/media.md).

## Install

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install telegram-archive
```

The `telegram-archive` command exists only inside that environment. Activate it again in every new shell, or call `.venv/bin/telegram-archive` directly.

## Configuration

Settings are environment variables, the same ones the Docker images read. There is no config file.

The package does not read `.env` from the working directory. The package looks for `.env` in its own install directory and then in each parent directory:

- A `.env` in the project folder that contains `.venv` is found.
- A system-wide install finds none.

Variables already set in the shell win over the `.env` file. If no `.env` is found, export the variables in the shell before each command. See [how settings are read](../reference/environment-variables.md#how-settings-are-read).

A minimal `.env` for one account:

```bash
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_PHONE=+15551234567
```

## Data directory

Without `--data-dir` or `BACKUP_PATH`, the commands try to use `/data/backups` and `/data/session`. Most commands then fail with a permission error while creating those directories. `migrate` fails with `unable to open database file`.

Pass `--data-dir` before the subcommand:

```bash
telegram-archive --data-dir ./data <command>
```

It sets `BACKUP_PATH=./data/backups` and `SESSION_DIR=./data/session` as absolute paths, and creates both directories. The SQLite database then lives at `./data/backups/telegram_backup.db` and media under `./data/backups/media`.

`--data-dir` does not override `DATABASE_URL`, `DATABASE_PATH`, `DATABASE_DIR` or `DB_PATH`. If one of those is set, the database stays where it points.

## First run

Run these in this order, from the project folder, with the environment active.

1. Create the database schema:

    ```bash
    telegram-archive --data-dir ./data migrate
    ```

    It prints `Database schema is up to date.` when it succeeds.

2. Log in to Telegram:

    ```bash
    telegram-archive --data-dir ./data auth
    ```

    It asks for the code Telegram sends you, and for your two-step verification password if you have one. See [Log in to Telegram](telegram-login.md).

    Ignore the closing hint about `python scheduler.py`. The command is `telegram-archive schedule`, step 3 below.

3. Start backing up. For scheduled backups plus the [real-time listener](../configuration/listener.md), run:

    ```bash
    telegram-archive --data-dir ./data schedule
    ```

    `schedule` runs a full backup at once, then follows `SCHEDULE`. For a single run that exits when done, use `backup` instead:

    ```bash
    telegram-archive --data-dir ./data backup
    ```

!!! warning "Run `migrate` before anything else"
    A pip install never migrates the database by itself. The Docker backup image does, on every start except the `auth` command.

    - On PostgreSQL, no command creates tables except `migrate`. Every other command that opens the database fails against an unmigrated database. `auth` does not open it.
    - On an empty SQLite file, every command except `auth` and `migrate` builds the schema without recording its version. A later `migrate` then fails with `table chats already exists`.
    - The viewer does the same. Starting `uvicorn` against an empty SQLite file builds the schema without recording its version, so run `migrate` before the first viewer start too.

    No CLI command repairs that SQLite state. The Docker backup image can record the version of such a file and then upgrade it. See [migrations](../configuration/database.md#migrations).

## The viewer

The viewer runs as a separate process. Start it with `uvicorn`, which comes with the package. `--data-dir` does not apply to the viewer, so give it the same `BACKUP_PATH` and database variables as the backup:

```bash
export BACKUP_PATH="$PWD/data/backups"
export VIEWER_USERNAME=admin
export VIEWER_PASSWORD='choose-a-long-password'
uvicorn telegram_archive.web.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> and sign in. Without `VIEWER_USERNAME` and `VIEWER_PASSWORD`, the viewer serves the page but answers every request that needs a login with 503 `Viewer authentication is not configured`. See [The viewer starts closed](../viewer/access.md#the-viewer-starts-closed) for other ways to sign in.

On SQLite, set `VIEWER_PORT=8000` for the backup process, in its shell or in `.env`. The backup sends live updates to port 8080 by default. Without this setting, a viewer on port 8000 misses them. See [Live updates and notifications](../viewer/live-updates.md).

Both `schedule` and `uvicorn` run until stopped. The package includes no service files, so run them under your own process manager, such as a systemd unit or a launchd agent.

`MEDIA_OPEN_CMD` and `MEDIA_OPEN_PATH_CMD` add buttons that open a media file or its folder on the machine running the viewer. They work only when the viewer runs on your own machine, outside a container, as it does on this page. See [Using the viewer](../viewer/using-the-viewer.md).

## Upgrading

Stop the backup and the viewer, then upgrade and migrate before starting either again:

```bash
pip install -U telegram-archive
telegram-archive --data-dir ./data migrate
```

See [Upgrading](../operations/upgrading.md) for release notes that need action.

## From a git checkout

For development, or to run unreleased code, use the lockfile with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/GeiserX/Telegram-Archive.git
cd Telegram-Archive
uv sync --locked
```

Contributors add the test and lint tools with `uv sync --locked --extra dev`.

The `./telegram-archive` script at the repository root runs the CLI from the checkout without installing the package. Run it through `uv run` so it uses the dependencies from `uv sync`:

```bash
uv run ./telegram-archive --data-dir ./data migrate
```

Alembic also works directly:

```bash
uv run alembic -c telegram_archive/alembic.ini upgrade head
```

From a checkout, the package finds a `.env` at the repository root.

Do not install from `requirements.txt`. It is incomplete and lacks Pillow, which the package needs.

## Next steps

- [Command line and Python API](../reference/cli.md) lists every flag of every command and the Python API.
- [Choosing chats](../configuration/choosing-chats.md) sets which chats get backed up.
- [Your first backup](first-backup.md) explains what happens during the first sweep.
