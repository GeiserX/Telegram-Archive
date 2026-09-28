<p align="center">
  <img src="https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/banner.svg" alt="Telegram Archive" width="900"/>
</p>

<h1 align="center">Telegram Archive</h1>

<p align="center">
  <a href="https://hub.docker.com/r/drumsergio/telegram-archive"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-archive?style=flat-square&logo=docker" alt="Docker Pulls"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/stargazers"><img src="https://img.shields.io/github/stars/GeiserX/Telegram-Archive?style=flat-square&logo=github" alt="GitHub Stars"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/Telegram-Archive?style=flat-square" alt="License"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/releases"><img src="https://img.shields.io/github/v/release/GeiserX/Telegram-Archive?style=flat-square" alt="Release"></a>
  <a href="https://codecov.io/gh/GeiserX/Telegram-Archive"><img src="https://codecov.io/gh/GeiserX/Telegram-Archive/graph/badge.svg" alt="codecov"></a>
  <a href="https://geiserx.github.io/Telegram-Archive/"><img src="https://img.shields.io/badge/docs-geiserx.github.io-blue?style=flat-square" alt="Docs"></a>
</p>

Telegram Archive is a self-hosted backup of one or more Telegram accounts. It runs in Docker on your own machine and saves messages, media, edits and deletions to SQLite or PostgreSQL on your own disk. A web viewer lets you read and search what it saved. The viewer never talks to Telegram.

![Telegram Archive viewer](https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/screenshots/chat-desktop.png)

## Features

- Scheduled incremental backups, with an optional real-time listener for new messages, edits and deletions.
- Each media file is saved once, even when several chats hold it. You can skip files by size or type.
- Earlier versions of edited messages, and deleted messages kept and marked as deleted, with the real-time listener or the scheduled edit and deletion sync.
- Several Telegram accounts in one archive.
- Imports from Telegram Desktop exports.
- A web viewer with search, forum topics, folders, a media gallery and seven themes.
- Extra viewer accounts, share links that open only chosen chats, and browser notifications for new messages.
- Optional voice transcription.
- SQLite by default, or PostgreSQL if you prefer.
- Docker images for amd64 and arm64.

## Quick start

1. Get an `api_id` and `api_hash` for your account at [my.telegram.org/apps](https://my.telegram.org/apps).

2. Get the files:

   ```bash
   git clone https://github.com/GeiserX/Telegram-Archive.git
   cd Telegram-Archive
   ```

3. Copy the settings file:

   ```bash
   cp .env.example .env
   ```

   Open `.env` and set `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `VIEWER_USERNAME`, `VIEWER_PASSWORD` and `VIEWER_TIMEZONE`.

4. Create the data directory. Both containers run as user ID 1000, so that user must own the directory:

   ```bash
   mkdir -p data && sudo chown -R 1000:1000 data
   ```

5. Log in to Telegram:

   ```bash
   docker compose run --rm telegram-backup python -m src auth
   ```

   The login code arrives in your Telegram app. If the account uses two-step verification, the command also asks for that password. The password shows on screen as you type, so run this where nobody can see your screen.

6. Start both containers:

   ```bash
   docker compose up -d
   ```

7. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with `VIEWER_USERNAME` and `VIEWER_PASSWORD`.

The first backup starts right away. For the schedule and what to check next, see [Your first backup](https://geiserx.github.io/Telegram-Archive/getting-started/first-backup/). The compose file pins both images to this release:

```text
drumsergio/telegram-archive:8.16.1
drumsergio/telegram-archive-viewer:8.16.1
```

## Documentation

The full documentation is at [geiserx.github.io/Telegram-Archive](https://geiserx.github.io/Telegram-Archive/).

- [Run with Docker](https://geiserx.github.io/Telegram-Archive/getting-started/docker/)
- [Your first backup](https://geiserx.github.io/Telegram-Archive/getting-started/first-backup/)
- [Install from PyPI](https://geiserx.github.io/Telegram-Archive/getting-started/pip/)
- [Choosing chats](https://geiserx.github.io/Telegram-Archive/configuration/choosing-chats/)
- [Using the viewer](https://geiserx.github.io/Telegram-Archive/viewer/using-the-viewer/)
- [Environment variables](https://geiserx.github.io/Telegram-Archive/reference/environment-variables/)
- [Upgrading](https://geiserx.github.io/Telegram-Archive/operations/upgrading/)
- [Monitoring and troubleshooting](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/)

Release notes are in the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md).

Found a bug or have a question? Open an [issue](https://github.com/GeiserX/Telegram-Archive/issues). The [troubleshooting page](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/#reporting-a-bug) says what to include. Report security problems through the [security policy](https://github.com/GeiserX/Telegram-Archive/blob/main/SECURITY.md), never in a public issue.

## License

GPL-3.0. See [LICENSE](https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE). Built on [Telethon](https://github.com/LonamiWebs/Telethon).
