# Run on Unraid

Unraid installs both images from **Community Applications**: `Telegram Archive` is the archiver, `Telegram Archive Viewer` is the web interface. They are separate containers that share one folder on disk.

The templates live in [GeiserX/docker-templates](https://github.com/GeiserX/docker-templates); [the archiver's](https://github.com/GeiserX/docker-templates/blob/main/drumsergio/telegram-archive.xml) and [the viewer's](https://github.com/GeiserX/docker-templates/blob/main/drumsergio/telegram-archive-viewer.xml) are the exact XML that Community Applications shows you.

## 1. Prepare the folders

The image runs as UID 1000. Docker creates a missing bind-mount path as `root`, and the container then cannot write to it, so make the folders first and give them to 1000:

```bash
mkdir -p /mnt/user/appdata/telegram-archive/session /mnt/user/appdata/telegram-archive/backups
chown -R 1000:1000 /mnt/user/appdata/telegram-archive
```

Run that from the Unraid terminal, the one in the top-right of the web interface.

## 2. Install the archiver

In **Apps**, search for `Telegram Archive` and install it. Fill in:

| Field | Value |
|-------|-------|
| API ID | The numeric API id from [my.telegram.org](https://my.telegram.org/apps). |
| API Hash | The API hash of that same application. |
| Phone Number | The account's number with its country code, for example `+15550100000`. |
| Session Data | `/mnt/user/appdata/telegram-archive/session` |
| Backups | `/mnt/user/appdata/telegram-archive/backups` |

[Log in to Telegram](telegram-login.md) explains where the first two come from. Everything else on the template has a working default; [Environment variables](../reference/environment-variables.md) is the full list, and anything on it can be added as an extra variable in the Unraid form.

## 3. Log in once

Telegram sends a login code to your phone, and the archiver has to type it back. The Unraid container has no terminal attached, so do this one step by hand, from the Unraid terminal, with the same paths and credentials you just filled in:

```bash
docker run -it --rm \
  -v /mnt/user/appdata/telegram-archive/session:/data/session \
  -v /mnt/user/appdata/telegram-archive/backups:/data/backups \
  -e TELEGRAM_API_ID=12345678 \
  -e TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef \
  -e TELEGRAM_PHONE=+15550100000 \
  drumsergio/telegram-archive:latest \
  python -m telegram_archive auth
```

`latest` is the tag the Community Applications template installs. If you changed it, use the tag the container's **Repository** field shows, so the login runs on the same image.

Enter the code it asks for, and your two-step verification password if the account has one. If the code never arrives, type `qr` at the prompt, or run the same command with `--qr` after `auth`, and scan the QR code from your phone. The session file lands in the `session` folder and every later start reuses it. The [login page](telegram-login.md) shows what the prompts look like and what to do when the code never arrives.

## 4. Start it

Start the container from the Docker tab. Its log should show the migrations running, then the first pass fetching your chats. [Your first backup](first-backup.md) describes what that pass does and how long it takes.

## 5. Install the viewer

Install `Telegram Archive Viewer` and point **Backups** at the same `/mnt/user/appdata/telegram-archive/backups` folder. The database file sits there, and SQLite writes a journal beside it, so the viewer needs that mount read-write even though it never changes a message. Open it on the port you chose. Set **Username** and **Password**: without them the viewer has no login, and anything on your network can read the whole archive.

## When something goes wrong

**The container prints the "GETTING STARTED" help and stops.** It was started with no command. Images up to 9.3.6 printed the CLI help in that case, and a template without a command left a container that exited on every start. Later images run the scheduler by default. On an older image, set **Post Arguments** to `python -m telegram_archive schedule` in the container's template.

**`Session not authorized`, over and over.** Step 3 has not been done, or it wrote the session somewhere else. The paths in the `docker run` command above must be exactly the paths in the template.

**`PERMISSION ERROR - Unable to write to session directory`.** The folders are not owned by UID 1000. Run the `chown` from step 1 again, and check that the template's paths match the folders you chowned.

**The viewer shows no chats.** It is reading a different database. Both containers must map the same host folder at `/data/backups`.
