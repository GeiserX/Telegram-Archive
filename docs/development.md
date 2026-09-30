# Development

This page explains how to send a change to Telegram-Archive: the two project rules, local setup, and what a pull request needs before it can merge. The project is licensed GPL-3.0-or-later, and by contributing you agree to license your work under it. Changes arrive as pull requests against `main` from a fork.

## Two rules that come first

!!! warning "The archive rule"
    Telegram-Archive is an archive. Nothing it has captured is deleted or overwritten. New state is recorded beside the old state, as a new row, a new column or a new file, and the viewer decides what to show.

    - Edited messages keep their earlier versions. Deleted messages stay in soft mode. Removed reactions, old avatars and every avatar change are kept.
    - Chat and user metadata, such as titles, usernames and participant counts, still overwrite in place. This is known debt. New code must not copy it.
    - The only removals are the ones an operator turns on:
        - `DELETION_MODE=hard`. The default is `soft`.
        - `EXCLUDE_DELETE_EXISTING`
        - `SKIP_MEDIA_DELETE_EXISTING`
        - `YOUTUBE_VIDEOS_DELETE_EXISTING`
        - `VERIFY_MEDIA`, which replaces a corrupted file with a fresh copy of the same media.
    - A new or wider removal needs three things: a setting that defaults to keeping data, a row on the [environment variables page](reference/environment-variables.md) that says it deletes, and explicit approval from the maintainer in the pull request.

    Reviews state what a pull request deletes, what it overwrites and what it forgets, in that order.

!!! danger "The privacy rule"
    Issues, pull requests, commits, review comments, docs and tests are all public.

    - Never name a person. Say "account 1" or "the second account".
    - Never describe a real deployment: no account labels, chat titles or ids, message or chat counts, hostnames, ports, domains or paths.
    - Never quote a private conversation, not even a short excerpt. Describe the problem in the project's own terms.
    - Fake data in tests and docs must look fake, such as `Account A` or `+1 555 0100`.

    If a detail was pushed by mistake, remove it from every place it reached at once: edit the pull request body or comment, rewrite the commit on an unmerged branch, or fix the file in a follow-up commit. If it already reached `main`, tell the maintainer.

## Set up a checkout

You need [uv](https://docs.astral.sh/uv/) and Python 3.14 or newer.

```bash
git clone https://github.com/YOUR_USERNAME/Telegram-Archive.git
cd Telegram-Archive
uv sync --locked --extra dev
source .venv/bin/activate
pre-commit install
cp .env.example .env
```

`uv sync --locked` creates `.venv` from the committed `uv.lock`. CI and the published images install from the same lockfile, so your checkout runs the same dependency versions. Fill in the Telegram API credentials in `.env` only when you test the backup against Telegram. The test suite does not need them.

The `dev` extra adds these tools:

| Tool | Version | Used for |
| --- | --- | --- |
| pytest | 9.0 or newer | The test suite, which needs the built-in `subtests` fixture |
| pytest-asyncio | 0.26.0 or newer | Async tests, with `asyncio_mode = auto` |
| pytest-cov | 7.1.0 or newer | Coverage reports |
| ruff | 0.16.8, pinned | Linting and formatting |
| pre-commit | 4.5.1 or newer | The commit hooks |

When you add a runtime dependency, add it to `[project] dependencies` in `pyproject.toml` and run `uv lock`. If the viewer needs it too, add the same requirement string, character for character, to the `viewer-runtime` dependency group. That group is the only set installed into the viewer image, and a test fails when its entries drift from the main list.

New code goes in the `telegram_archive` package. `src/` is the package's old name. The Docker images keep it so older compose files still work. It is not in the wheel. Put nothing new there.

## Run the backup and the viewer from a checkout

Run the backup with a local data directory. Migrate first, then log in, then start a backup:

```bash
telegram-archive --data-dir ./data migrate
telegram-archive --data-dir ./data auth
telegram-archive --data-dir ./data backup
```

Use `schedule` instead of `backup` to run on the cron schedule. `python -m telegram_archive` works the same as `telegram-archive`. Without `--data-dir` or `BACKUP_PATH`, the commands use `/data`. Settings already exported in the shell win over the `.env` file at the repository root.

The viewer is a separate process:

```bash
export BACKUP_PATH="$PWD/data/backups"
export VIEWER_USERNAME=admin
export VIEWER_PASSWORD='choose-a-long-password'
uvicorn telegram_archive.web.main:app --host 127.0.0.1 --port 8000
```

Without `VIEWER_USERNAME` and `VIEWER_PASSWORD`, every request that needs a login gets a 503. On SQLite, set `VIEWER_PORT=8000` for the backup process too, or live updates go to port 8080 and never reach this viewer.

[Install from PyPI](getting-started/pip.md) explains the order of the first commands and the data directory layout in more detail.

## Tests

```bash
# Everything
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=telegram_archive --cov-report=term-missing

# One file
python -m pytest tests/test_db_adapter.py -v
```

Add a test with every change. A bug fix gets a test that fails without the fix. New database operations get tests for the data types they store.

### SQLite and PostgreSQL

Tests that use the `real_db` or `real_adapter` fixture run twice, once on a throwaway SQLite file and once on PostgreSQL. Use those fixtures for new database tests; a mocked manager never compiles the SQL. Without a PostgreSQL server, the PostgreSQL leg is skipped with `no PostgreSQL server reachable`. That is fine for a quick local run, but CI fails the job when that message appears.

To run the PostgreSQL leg, start a server and point `TEST_POSTGRES_URL` at it:

```bash
docker run --rm -d --name telegram-archive-test-pg \
  -e POSTGRES_USER=telegram \
  -e POSTGRES_PASSWORD=not-a-secret \
  -p 127.0.0.1:5432:5432 \
  postgres:18-alpine

export TEST_POSTGRES_URL=postgresql://telegram:not-a-secret@localhost:5432/postgres
python -m pytest tests/ -v
```

The suite connects to the server in that URL but never touches the database named in it. It creates its own databases on that server, `telegram_archive_pytest` and a few more whose names start with `telegram_archive_`, and rebuilds each one's `public` schema on every run. The user in the URL therefore needs permission to create databases.

Use `TEST_POSTGRES_URL`, not `DATABASE_URL`. When `TEST_POSTGRES_URL` is unset, the suite falls back to `DATABASE_URL`. That variable also points the application at an archive. Exporting it therefore repoints every test that builds a database manager without its own settings.

### ffmpeg

The tests for video thumbnails and the transcription audio check need `ffmpeg` and `ffprobe` on the `PATH`. Without them those tests are skipped locally. CI installs ffmpeg and fails the job if they are skipped.

### Coverage

Codecov checks coverage on every pull request. The project target is 90% with a 2% threshold, and the patch target is 90% with a 5% threshold. Codecov does not count the tests themselves.

## Lint and format

[Ruff](https://docs.astral.sh/ruff/) does both. The line length is 120, the target is Python 3.14, and strings use double quotes.

```bash
ruff check .           # lint
ruff check --fix .     # lint and fix what Ruff can
ruff format --check .  # check formatting
ruff format .          # format
```

CI runs the Ruff version from `uv.lock`. Run Ruff from the `.venv` so you get the same version.

The pre-commit hooks run on every commit once you have run `pre-commit install`. To run them on the whole tree, as CI does:

```bash
pre-commit run --all-files
```

The hooks:

- check YAML and TOML syntax, except `mkdocs.yml`, which the strict docs build validates
- make every file end with one newline and strip trailing whitespace
- block merge-conflict markers and files over 500 KB
- run `ruff --fix` and `ruff format`

None of the fixing hooks touch `telegram_archive/web/static/vendor/`. Those vendored files are checked against a sha256 manifest, and any edit breaks it. The Ruff hook version in `.pre-commit-config.yaml` can lag behind the Ruff pin in `pyproject.toml`. When they differ, the version the `ruff` CI job runs is the one that counts. When you change the Ruff pin, change the hook version to match.

## Database changes

A schema change needs an Alembic migration. Create it from the checkout:

```bash
alembic -c telegram_archive/alembic.ini revision --rev-id 034 -m "add something"
telegram-archive --data-dir ./data migrate
```

`migrate` runs `alembic upgrade head` against the `./data` directory from the earlier section. A bare `alembic upgrade head` reads `BACKUP_PATH` from `.env`, which points at `/data/backups`.

Alembic puts the time of day in the new file's name. Rename the file to the pattern the existing revisions use, such as `YYYYMMDD_034_add_something.py`.

Revision ids are three-digit numbers in sequence, so pass the next one with `--rev-id` instead of taking the random id Alembic would generate. A new migration must also run safely on a database whose schema already has its change, because some databases are built without migration history. Guard each step by inspecting the table first. See [Migrations](configuration/database.md#migrations) for how the images run them.

Every change to database code follows these rules:

- Every `chat_id` uses the marked format, through `_get_marked_id()`.
- Every datetime passes through `_strip_tz()` before a database operation.
- INSERT and UPDATE handle the same fields in the same way.
- Tests for data type handling go in `tests/test_db_adapter.py`.

A data fix that cannot be a migration goes in `scripts/` as a data migration script.

## Build the docs locally

The site is built with MkDocs Material and published at <https://geiserx.github.io/Telegram-Archive>.

```bash
pip install -r docs/requirements-docs.txt
mkdocs serve           # live preview at http://127.0.0.1:8000
mkdocs build --strict  # what CI runs
```

The strict build fails on any warning, including a broken link, a missing anchor, an absolute link between pages or a missing snippet include.

- Every new page needs an entry in the `nav` section of `mkdocs.yml`.
- MkDocs builds every Markdown file under `docs/`. A file that must not become a page goes in `exclude_docs`.
- Link between pages with relative paths, such as `[Upgrading](operations/upgrading.md)` from this page.
- Every page has an edit link that opens the file on GitHub.

A pull request that touches `docs/` or `mkdocs.yml` runs the strict build. A merge to `main` that touches them publishes the site.

## Branches, commits and pull requests

Branch from `main` and name the branch by its kind of change:

| Prefix | For |
| --- | --- |
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation |
| `refactor/` | Code changes that keep behavior |
| `test/` | Tests only |

Commits follow [Conventional Commits](https://www.conventionalcommits.org/), in the form `type(scope): description`. The types are `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore` and `ci`. For example:

```text
fix(backup): handle timezone-aware datetimes in updates
```

Pull requests are squash-merged, so the pull request title becomes the commit on `main`, with the pull request number added. Write the title as a Conventional Commit that says why the change matters.

The pull request template asks for:

| Section | What to write |
| --- | --- |
| Summary | The change in one or two bullets |
| Type of change | Bug fix, feature, breaking change, documentation, or infrastructure and CI |
| Database changes | An Alembic migration, a data migration script in `scripts/`, or none |
| Data consistency checklist | The database rules above |
| Testing | pytest, `ruff check`, `ruff format --check`, and a manual test |
| Security checklist | No committed secrets, validated input, checked authentication and authorization |
| Deployment notes | Anything an operator must do, such as an image rebuild |

These checks must pass before a merge. They run on every pull request, whatever it touches:

| Check | What it runs |
| --- | --- |
| Tests | The full suite on Python 3.14 with coverage, PostgreSQL 18 and ffmpeg. See [Tests](#tests) for the skip rules. |
| Lint: ruff | `ruff check .` and `ruff format --check .` |
| Lint: pre-commit | Every hook on every file, so a pull request from a fork cannot skip them |
| CodeQL: Analyze | Static analysis of the Python package. This is a required check on `main`. |
| CodeQL: Analyze JavaScript | Static analysis of the viewer's script blocks and service worker |
| Package: wheel | Builds the sdist and wheel, runs `twine check --strict`, then installs the wheel in a clean environment and uses it |

A pull request that changes the docs also runs the [strict docs build](#build-the-docs-locally).

## Issues and security

Open an issue with one of the three forms: Bug report, Feature request or Question. The repository does not accept blank issues. The bug report form asks for:

- the version
- how you deploy
- SQLite or PostgreSQL
- what happened
- what you expected
- steps to reproduce
- logs
- your environment

Remove names, chat titles and deployment details from logs before you paste them, as the privacy rule says.

Before you request a feature, check the [roadmap](roadmap/index.md).

Never report a security problem in a public issue. Use [GitHub Security Advisories](https://github.com/GeiserX/Telegram-Archive/security/advisories/new), or the email address in the [security policy](https://github.com/GeiserX/Telegram-Archive/blob/main/SECURITY.md). Only the latest release receives fixes.
