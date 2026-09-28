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

Telegram Archive backs up one or more Telegram accounts to a machine you host. It runs in Docker and saves messages, media, edits and deletions to SQLite or PostgreSQL on your own disk. A web viewer lets you read and search what it saved. The viewer never talks to Telegram.

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

   Open `.env` and set these lines. The three viewer lines start commented out, so remove the `#` in front of them:

   ```ini
   TELEGRAM_API_ID=12345678
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   TELEGRAM_PHONE=+15551234567
   VIEWER_USERNAME=admin
   VIEWER_PASSWORD=choose-a-long-password
   VIEWER_TIMEZONE=Europe/London
   ```

4. Create the data directory. Both containers run as user ID 1000, so that user must own the directory:

   ```bash
   mkdir -p data && sudo chown -R 1000:1000 data
   ```

5. Log in to Telegram:

   ```bash
   docker compose run --rm telegram-backup python -m telegram_archive auth
   ```

   Telegram sends a login code, and the command asks for it. If the account uses two-step verification, the command also asks for that password. The password is visible as you type, so run this where nobody can see your screen. See [Log in to Telegram](https://geiserx.github.io/Telegram-Archive/getting-started/telegram-login/) if the login fails or must run without a terminal.

6. Start both containers:

   ```bash
   docker compose up -d
   ```

7. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with `VIEWER_USERNAME` and `VIEWER_PASSWORD`.

The first backup starts right away. [Your first backup](https://geiserx.github.io/Telegram-Archive/getting-started/first-backup/) covers the schedule and what to check next.

The compose file pins both images to this release:

```text
drumsergio/telegram-archive:8.17.0
drumsergio/telegram-archive-viewer:8.17.0
```

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

GPL-3.0. See [LICENSE](https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE). Built on [Telethon](https://github.com/LonamiWebs/Telethon).
