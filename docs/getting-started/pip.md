# Install from PyPI

This page runs the backup and the viewer from the Python package, without Docker. Run the first commands in the order shown. The database must be migrated before anything else touches it.

## Availability

| What | Name |
|------|------|
| PyPI package | `telegram-archive` |
| Command | `telegram-archive` |
| Python module | `telegram_archive` |

The package is published to PyPI starting with the first release after 8.16.1. Earlier versions exist only as Docker images. Until that release is out, install from a checkout. See [From a git checkout](#from-a-git-checkout).

It requires Python 3.14 or newer.

The wheel holds only the `telegram_archive` package with its templates, static files and Alembic migrations. The repository's `scripts/` folder and the old `src` alias are not in it. See [repository scripts](../reference/cli.md#repository-scripts) for how to run those.

## System tools

Put `ffmpeg` and `ffprobe` on your `PATH`. The backup and the viewer use them for two things:

- Video thumbnails, in the viewer and during the backup.
- [Voice transcription](../configuration/transcription.md), to check for an audio stream and to extract the audio track of videos and documents.

Without them nothing fails loudly. Videos get no thumbnail, and transcription sends the whole stored file instead of its audio track.

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

A `.env` file is not read from the working directory. The package looks for `.env` in its own install directory and then in each parent directory:

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

Without `--data-dir` or `BACKUP_PATH`, the commands try to use `/data/backups` and `/data/session`. On most machines that fails with a permission error.

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
    A pip install never migrates the database by itself. The Docker backup image migrates on every start except the `auth` command. The package does not.

    - On PostgreSQL, no command creates tables except `migrate`. Every other command fails against an unmigrated database.
    - On an empty SQLite file, any command that opens the database, which is every command except `auth` and `migrate`, first builds the schema without recording its version. A later `migrate` then fails with `table chats already exists`.

    No CLI command repairs that SQLite state. The Docker backup image can record the version of such a file and then upgrade it. See [migrations](../configuration/database.md#migrations). To avoid the problem, run `migrate` first.

## The viewer

The viewer runs as a separate process. Start it with `uvicorn`, which comes with the package. `--data-dir` does not apply to the viewer, so give it the same `BACKUP_PATH` and database variables as the backup:

```bash
export BACKUP_PATH="$PWD/data/backups"
export VIEWER_USERNAME=admin
export VIEWER_PASSWORD='choose-a-long-password'
uvicorn telegram_archive.web.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> and sign in. Without `VIEWER_USERNAME` and `VIEWER_PASSWORD`, the viewer serves the page but answers every request that needs a login with 503 `Viewer authentication is not configured`. See [Logins, viewer accounts and share links](../viewer/access.md) for the other options.

On SQLite, set `VIEWER_PORT=8000` for the backup process, in its shell or in `.env`. The backup sends live updates to port 8080 by default, so the viewer would miss them. See [Live updates and notifications](../viewer/live-updates.md).

Both `schedule` and `uvicorn` run until stopped. The package includes no service files, so run them under your own process manager, such as a systemd unit or a launchd agent.

`MEDIA_OPEN_CMD` and `MEDIA_OPEN_PATH_CMD` add buttons that open a media file or its folder on the machine running the viewer. They are useful only when the viewer runs directly on your machine, as it does here, and not in a container. See [Using the viewer](../viewer/using-the-viewer.md).

## Upgrading

```bash
pip install -U telegram-archive
telegram-archive --data-dir ./data migrate
```

Stop the backup and the viewer first, and run `migrate` before starting either again. See [Upgrading](../operations/upgrading.md) for release notes that need action.

## From a git checkout

For development, or to run unreleased code, use the lockfile with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/GeiserX/Telegram-Archive.git
cd Telegram-Archive
uv sync --locked
```

Contributors add the test and lint tools with `uv sync --locked --extra dev`.

The `./telegram-archive` script at the repository root runs the CLI from the checkout without installing the package. The dependencies must still be installed, so run it inside the environment `uv sync` created:

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

- Every flag of every command, and the Python API: [Command line and Python API](../reference/cli.md).
- Which chats get backed up: [Choosing chats](../configuration/choosing-chats.md).
- What happens during the first sweep: [Your first backup](first-backup.md).
