<p align="center">
  <img src="https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/banner.svg" alt="Telegram Archive" width="900"/>
</p>

<p align="center">
  <a href="https://github.com/GeiserX/Telegram-Archive/releases"><img src="https://img.shields.io/github/v/release/GeiserX/Telegram-Archive?style=flat-square" alt="Release"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/Telegram-Archive/tests.yml?style=flat-square&label=tests" alt="Tests"></a>
  <a href="https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/Telegram-Archive?style=flat-square" alt="License"></a>
  <a href="https://hub.docker.com/r/drumsergio/telegram-archive"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-archive?style=flat-square&logo=docker" alt="Docker Pulls"></a>
  <a href="https://codecov.io/gh/GeiserX/Telegram-Archive"><img src="https://codecov.io/gh/GeiserX/Telegram-Archive/graph/badge.svg" alt="Coverage"></a>
</p>

Telegram Archive backs up one or more Telegram accounts to a machine you host. It runs in Docker or from `pip`, and saves messages, media, edits and deletions to SQLite or PostgreSQL on your own disk. A web viewer, which never talks to Telegram, lets you read and search what it saved.

Telegram Desktop's own export is a one-off file with no earlier versions and no deleted messages; this runs on a schedule, keeps earlier versions and deleted messages, and can import that export too.

<p align="center"><img src="https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docs/images/screenshots/chat-desktop.png" alt="A group chat in the viewer, with a pinned message, a four-photo album, replies and reactions" width="880"></p>

## Features

- Saves new messages, their media, edits and reactions the moment they happen, and runs a daily full pass that fetches only what is new to fill in anything the live connection missed.
- Keeps every edit with its earlier text, and keeps every deleted message, marked as deleted, with its text and media.
- Saves photos, videos, voice notes, stickers, documents and link previews once each, however many chats share them, and skips files over a size you set.
- Reads like Telegram: chats, forum topics, folders, archived chats, reactions, pinned messages, polls and shared media, with search across every chat.
- Shows what Telegram no longer does: a What changed feed of deletions, edits and new transcripts, and the earlier profile photos of every chat.
- Backs up several accounts into one archive, and imports Telegram Desktop exports.
- Turns voice messages into searchable text through a transcription server you run or rent.
- Gives other people a login to chosen chats or a share link that opens one chat, and sends browser notifications for new messages.
- Twelve themes, including one that follows the system light or dark setting, on a viewer that works on a phone.
- Stays yours: SQLite or PostgreSQL on your disk, read-only containers with every capability dropped, for amd64 and arm64, or a plain `pip` install.

## Quick start

You need Docker Compose, and an `api_id` and `api_hash` for your account from [my.telegram.org/apps](https://my.telegram.org/apps).

```bash
curl -fsSLO https://raw.githubusercontent.com/GeiserX/Telegram-Archive/main/docker-compose.yml
cat > .env <<'EOF'
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_PHONE=+15551234567
VIEWER_USERNAME=admin
VIEWER_PASSWORD=choose-a-long-password
VIEWER_TIMEZONE=Europe/London
EOF
mkdir -p data && sudo chown -R 1000:1000 data
docker compose run --rm telegram-backup python -m telegram_archive auth
docker compose up -d   # drumsergio/telegram-archive:9.1.0 and drumsergio/telegram-archive-viewer:9.1.0
```

`auth` asks for the code Telegram sends you, and for your two-step password if you have one. Then open [http://127.0.0.1:8000](http://127.0.0.1:8000), sign in with the viewer username and password, and watch the first backup fill the chat list. [Run with Docker](https://geiserx.github.io/Telegram-Archive/getting-started/docker/) has every step, with the Podman and Colima notes, and [Install from PyPI](https://geiserx.github.io/Telegram-Archive/getting-started/pip/) runs the same thing from `pip install telegram-archive`.

## Documentation

The full documentation is at [geiserx.github.io/Telegram-Archive](https://geiserx.github.io/Telegram-Archive/).

- Get started: [Run with Docker](https://geiserx.github.io/Telegram-Archive/getting-started/docker/), [Log in to Telegram](https://geiserx.github.io/Telegram-Archive/getting-started/telegram-login/), [Your first backup](https://geiserx.github.io/Telegram-Archive/getting-started/first-backup/), [Install from PyPI](https://geiserx.github.io/Telegram-Archive/getting-started/pip/)
- Configuration: [Choosing chats](https://geiserx.github.io/Telegram-Archive/configuration/choosing-chats/), [Media downloads](https://geiserx.github.io/Telegram-Archive/configuration/media/), [Schedule and backup tuning](https://geiserx.github.io/Telegram-Archive/configuration/schedule/), [Multiple accounts](https://geiserx.github.io/Telegram-Archive/configuration/multiple-accounts/), [Real-time listener](https://geiserx.github.io/Telegram-Archive/configuration/listener/), [Event webhook](https://geiserx.github.io/Telegram-Archive/configuration/event-webhook/), [Voice transcription](https://geiserx.github.io/Telegram-Archive/configuration/transcription/), [SQLite and PostgreSQL](https://geiserx.github.io/Telegram-Archive/configuration/database/)
- Viewer: [Using the viewer](https://geiserx.github.io/Telegram-Archive/viewer/using-the-viewer/), [Logins, viewer accounts and share links](https://geiserx.github.io/Telegram-Archive/viewer/access/), [Themes and wallpaper](https://geiserx.github.io/Telegram-Archive/viewer/themes/), [Live updates and notifications](https://geiserx.github.io/Telegram-Archive/viewer/live-updates/), [Exposing the viewer safely](https://geiserx.github.io/Telegram-Archive/viewer/exposing/)
- Operations: [Upgrading](https://geiserx.github.io/Telegram-Archive/operations/upgrading/), [Backing up the archive](https://geiserx.github.io/Telegram-Archive/operations/backup-and-restore/), [Import and maintenance tasks](https://geiserx.github.io/Telegram-Archive/operations/maintenance/), [Monitoring and troubleshooting](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/)
- Reference: [Glossary](https://geiserx.github.io/Telegram-Archive/reference/glossary/), [Environment variables](https://geiserx.github.io/Telegram-Archive/reference/environment-variables/), [Command line and Python API](https://geiserx.github.io/Telegram-Archive/reference/cli/), [HTTP API](https://geiserx.github.io/Telegram-Archive/reference/api/)
- [Development](https://geiserx.github.io/Telegram-Archive/development/) and the [Roadmap](https://geiserx.github.io/Telegram-Archive/roadmap/)

Release notes are in the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md). Open an [issue](https://github.com/GeiserX/Telegram-Archive/issues) for bugs and questions; the [troubleshooting page](https://geiserx.github.io/Telegram-Archive/operations/troubleshooting/#reporting-a-bug) says what to include. Report security problems through the [security policy](https://github.com/GeiserX/Telegram-Archive/blob/main/SECURITY.md), never in a public issue.

## License

[GPL-3.0-or-later](https://github.com/GeiserX/Telegram-Archive/blob/main/LICENSE). Built on [Telethon](https://github.com/LonamiWebs/Telethon).
