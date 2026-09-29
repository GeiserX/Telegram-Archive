<p align="center">
  <img src="https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/banner.svg" alt="Telegram Archive" width="900"/>
</p>

<h1 align="center">Telegram Archive</h1>

<p align="center">
  <a href="https://github.com/GeiserX/Telegram-Archive/releases"><img src="https://img.shields.io/github/v/release/GeiserX/Telegram-Archive?style=flat-square" alt="Release"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/Telegram-Archive/tests.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/Telegram-Archive?style=flat-square" alt="License"></a>
  <a href="https://hub.docker.com/r/drumsergio/telegram-archive"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-archive?style=flat-square&logo=docker" alt="Docker Pulls"></a>
  <a href="https://codecov.io/gh/GeiserX/Telegram-Archive"><img src="https://codecov.io/gh/GeiserX/Telegram-Archive/graph/badge.svg" alt="codecov"></a>
</p>

Telegram Archive backs up one or more Telegram accounts to a machine you host. It runs in Docker and saves messages, media, edits and deletions to SQLite or PostgreSQL on your own disk. A web viewer, which never talks to Telegram, lets you read and search what it saved.

![Telegram Archive viewer](https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/screenshots/chat-desktop.png)

## Features

- Scheduled incremental backups, with an optional real-time listener for new messages, edits and deletions.
- Each media file is saved once, even when several chats hold it. You can skip files by size or type.
- Keeps earlier versions of edited messages, and keeps deleted messages marked as deleted. The real-time listener or the scheduled edit and deletion sync records both.
- Several Telegram accounts in one archive.
- Imports from Telegram Desktop exports.
- A web viewer with search, forum topics, folders, a media gallery and seven themes.
- Extra viewer accounts, share links that open only chosen chats, and browser notifications for new messages.
- Optional voice transcription.
- SQLite by default, or PostgreSQL.
- Docker images for amd64 and arm64.

## Quick start

You need Docker Compose, and an `api_id` and `api_hash` for your account from [my.telegram.org/apps](https://my.telegram.org/apps).

```bash
git clone https://github.com/GeiserX/Telegram-Archive.git && cd Telegram-Archive
cp .env.example .env   # set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE; uncomment and set VIEWER_USERNAME, VIEWER_PASSWORD
mkdir -p data && sudo chown -R 1000:1000 data   # both containers run as uid 1000; with Colima on macOS, skip the chown
docker compose run --rm telegram-backup python -m telegram_archive auth
docker compose up -d   # starts drumsergio/telegram-archive:8.17.0 and drumsergio/telegram-archive-viewer:8.17.0
```

The `auth` step asks for the code Telegram sends you (and your two-step verification password, which shows on screen as you type); then open [http://127.0.0.1:8000](http://127.0.0.1:8000), sign in with the viewer username and password, and the first backup is already running. [Run with Docker](https://geiserx.github.io/Telegram-Archive/getting-started/docker/) has every step in detail, and [Log in to Telegram](https://geiserx.github.io/Telegram-Archive/getting-started/telegram-login/) covers a login that fails or must run without a terminal.

## Documentation

The full documentation is at [geiserx.github.io/Telegram-Archive](https://geiserx.github.io/Telegram-Archive/).

- [Run with Docker](https://geiserx.github.io/Telegram-Archive/getting-started/docker/)
- [Log in to Telegram](https://geiserx.github.io/Telegram-Archive/getting-started/telegram-login/)
- [Your first backup](https://geiserx.github.io/Telegram-Archive/getting-started/first-backup/)
- [Install without Docker](https://geiserx.github.io/Telegram-Archive/getting-started/pip/)
- [Choosing chats](https://geiserx.github.io/Telegram-Archive/configuration/choosing-chats/)
- [Using the viewer](https://geiserx.github.io/Telegram-Archive/viewer/using-the-viewer/)
- [Environment variables](https://geiserx.github.io/Telegram-Archive/reference/environment-variables/)
- [Upgrading](https://geiserx.github.io/Telegram-Archive/operations/upgrading/)
- [Monitoring and troubleshooting](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/)

Release notes are in the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md).

Open an [issue](https://github.com/GeiserX/Telegram-Archive/issues) for bugs and questions. The [troubleshooting page](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/#reporting-a-bug) says what to include. Report security problems through the [security policy](https://github.com/GeiserX/Telegram-Archive/blob/main/SECURITY.md), never in a public issue.

## License

[GPL-3.0-or-later](https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE). Built on [Telethon](https://github.com/LonamiWebs/Telethon).
