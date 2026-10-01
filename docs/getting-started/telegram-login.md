# Log in to Telegram

The backup talks to Telegram as your own account, so it needs a logged-in session before it can capture anything.

## Credentials

You need three values in your `.env` file:

| Variable | What it is |
|----------|------------|
| `TELEGRAM_API_ID` | The numeric API id of an application you create at [my.telegram.org](https://my.telegram.org/apps). |
| `TELEGRAM_API_HASH` | The API hash of that same application. |
| `TELEGRAM_PHONE` | The account's phone number with its country code, for example `+15550100000`. |

```ini
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_PHONE=+15550100000
```

`TELEGRAM_API_ID` must be a number. If it still holds a placeholder or a typo, startup stops with a message that points you back to my.telegram.org.

To back up several accounts, declare them with the `TG_ACCOUNT_<N>_*` variables instead. See [Multiple accounts](../configuration/multiple-accounts.md).

## Run the login

=== "Docker"

    ```bash
    docker compose run --rm telegram-backup python -m telegram_archive auth
    ```

=== "Native"

    ```bash
    telegram-archive --data-dir ./data auth
    ```

    [Install from PyPI](pip.md) explains how the command finds your `.env` file.

`python -m telegram_archive.setup_auth` runs the same login. The `Session not authorized` error names this form.

Telegram sends a code to your Telegram app. The command asks for it:

```text
Enter verification code:
```

If two-step verification is on for the account, it then asks:

```text
Enter your 2FA password:
```

!!! warning "The password is visible"
    The 2FA password is shown on screen as you type it. Run the login where nobody can watch your screen.

After the login, the command compares the account's phone number with the one you configured. It compares digits only and drops a leading `00`, so `+34...` and `0034...` match. If the numbers differ, the login fails. Delete the session file and run the login again, or fix how the number is written in `.env`.

What else the command does:

- It walks every configured account in turn. An account whose session is already authorized gets no prompt, but its phone number is still compared.
- A failing account stops the walk. Run the command again after fixing it. Finished accounts are skipped.
- It exits with code 0 on success and 1 on failure.
- It creates the data directories but never opens the database.

Before each command, the Docker image updates the database schema. It skips this step for `python -m telegram_archive auth` and `python -m src auth`, so the login starts at once.

When it succeeds, the command tells you to run a `scheduler.py` file. That file does not exist. The real next step is `docker compose up -d`, or `telegram-archive --data-dir ./data schedule` for a native install.

## Helper scripts

The repository ships two wrappers around the Docker login.

`./init_auth.sh` checks that `.env` exists in the current directory, creates `data/backups` and runs the Docker login command above.

The script creates `data/backups` as your own user, and the container runs as uid 1000. If your uid is not 1000, see [Permission errors](#permission-errors).

`init_auth.bat` does the same on Windows but runs `python -m telegram_archive.setup_auth`. The image does not treat that as the login command, so it updates the database schema first.

## Log in without a terminal

If you cannot type answers to prompts, for example in a script or over SSH without a terminal, use the two-step login script.

```bash
docker compose run --rm telegram-backup python scripts/auth_noninteractive.py send
```

`send` asks Telegram for a code. It saves a token that links that code to this login in `<SESSION_DIR>/<SESSION_NAME>.phone_code_hash`. The default path is `/data/session/telegram_backup.phone_code_hash`. The file has mode 0600, so only your user can read it.

```bash
docker compose run --rm telegram-backup python scripts/auth_noninteractive.py verify CODE
```

With two-step verification on, add the password:

```bash
docker compose run --rm telegram-backup python scripts/auth_noninteractive.py verify CODE 2FA_PASSWORD
```

`verify` reads the hash from that file, or from `TELEGRAM_PHONE_CODE_HASH` when the variable is set. It deletes the file after a successful login. The password goes on the command line, so it can end up in your shell history.

Limits of this script:

- It reads `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`, `SESSION_NAME`, `SESSION_DIR`, `BACKUP_PATH`, `TELEGRAM_PHONE_CODE_HASH`, `TELEGRAM_DEVICE_MODEL` and the `TELEGRAM_PROXY_*` variables. It logs in a single account and ignores `TG_ACCOUNT_<N>_*`.
- Its own usage text names a compose service called `backup`. The service is `telegram-backup`.
- It does not check that the phone number matches, unlike the `auth` command.
- When the session file is already authorized, both `send` and `verify` print `Already authorized` and stop.
- If two-step verification is on and you run `verify CODE` without the password, it exits with code 1 and asks you to re-run with the password.

## Session files

A session file holds the login. The backup reads it on every connection.

| Setting | Default |
|---------|---------|
| `SESSION_DIR` | `session` next to `BACKUP_PATH`: `/data/session` in Docker, `PATH/session` for a native run with `--data-dir PATH`. |
| `SESSION_NAME` | `telegram_backup`. Used for the single account, and for account 1 unless `TG_ACCOUNT_1_SESSION_NAME` is set. |
| Accounts 2 and up | `telegram_backup_account<N>`. Override with `TG_ACCOUNT_<N>_SESSION_NAME`. |

The Telegram library adds `.session` to the name, so the default file is `/data/session/telegram_backup.session`.

The `schedule` service keeps two copies next to each session:

- Before each connection attempt, it copies a session that holds a login to `<name>.session.bak`.
- Each time a connection succeeds with an authorized session, it copies the session to `<name>.session.authenticated`.

One-off commands, the standalone listener and the helper scripts open the session with their own client. They neither write nor restore these copies.

When authorization fails, the service restores the session from the `.authenticated` copy first, then from the `.bak` copy. The next connection attempt uses the restored copy. What the service does in the meantime depends on the case:

- A service with one account that starts with an unauthorized session exits with `Session not authorized. Please run authentication setup.` The container restarts into the same error.
- A service with several accounts keeps running the other accounts. It retries the failing account on each sweep and listener start.
- A session that stops working while the service runs is logged and retried on the next cycle. The process does not exit, so watch the log rather than the container state.

!!! danger "A session file is your account"
    Anyone who has a session file can read and send messages as you. Keep the session directory private. Include it in your backups, together with the database and media. See [Backing up the archive](../operations/backup-and-restore.md).

### What Telegram sees

The login counts as a new device on the account and appears in Telegram under Settings, Devices. The entry is named `Telegram Archive`, with the operating system and its version, and the Telegram Archive version as the app version. Set [`TELEGRAM_DEVICE_MODEL`](../reference/environment-variables.md#telegram_device_model) to give an install its own name, so several installs are easy to tell apart. The name is sent each time the backup connects. An existing login keeps working after a change, and you do not need to log in again. Telegram may keep showing the old name for an existing entry. A new login always shows the current one. Ending that entry in Telegram invalidates the session file. The backup then logs `Session not authorized` until you [log in again](#log-in-again). A service with one account that starts in this state exits, and the container restarts into the same error. A running service, or a service with several accounts, keeps retrying, so watch the log. The `.authenticated` and `.bak` copies hold the same key, so they do not help here. They only cover a session file that is damaged on disk.

## Log in again

When the logs show `Session not authorized`, Telegram no longer accepts the session. Stop the backup, log in, and start it again:

=== "Docker"

    ```bash
    docker compose stop telegram-backup
    docker compose run --rm telegram-backup python -m telegram_archive auth
    docker compose up -d
    ```

=== "Native"

    Stop the running `schedule` process first, then:

    ```bash
    telegram-archive --data-dir ./data auth
    telegram-archive --data-dir ./data schedule
    ```

## One client per session

Nothing in the code stops two processes from using the same session file at once. If two clients connect with one session, Telegram can invalidate the login.

While `schedule` runs, do not run any of these against the same session:

- `backup`, `fill-gaps`, `backfill-topics`, `reclassify-round-videos` or `backfill-details`
- `auth`
- `python -m telegram_archive.listener`
- `scripts/auth_noninteractive.py`
- `scripts/restore_chat.py`

Stop the backup service first, run the command, then start the service again. `export`, `stats`, `status` and `list-chats` never change archived data and never connect to Telegram, so they are safe while the service runs.

Never point two installs at the same session file.

## Permission errors

If the login cannot write the session file, the data directory usually does not belong to uid 1000. Change its owner as shown in [Create the data directory](docker.md#3-create-the-data-directory). You can also run the container as your own user with `--user <uid>:<gid>`.

## SOCKS5 or MTProxy transport

The backup can reach Telegram through a SOCKS5 proxy or a Telegram MTProxy. Setting any of `TELEGRAM_PROXY_TYPE`, `TELEGRAM_PROXY_ADDR`, `TELEGRAM_PROXY_PORT`, `TELEGRAM_PROXY_USERNAME`, `TELEGRAM_PROXY_PASSWORD` or `TELEGRAM_PROXY_SECRET` turns the proxy on. From then on these rules apply, and startup stops if one is broken:

- `TELEGRAM_PROXY_TYPE`, `TELEGRAM_PROXY_ADDR` and `TELEGRAM_PROXY_PORT` are required.
- `TELEGRAM_PROXY_TYPE` must be `socks5` or `mtproxy`, in any letter case.
- `TELEGRAM_PROXY_PORT` must be a number from 1 to 65535.
- For SOCKS5, `TELEGRAM_PROXY_USERNAME` and `TELEGRAM_PROXY_PASSWORD` must be set together, or not at all. `TELEGRAM_PROXY_SECRET` is invalid.
- For MTProxy, `TELEGRAM_PROXY_SECRET` is required. Username and password are invalid, and `TELEGRAM_PROXY_RDNS` must be unset or false.

For SOCKS5, `TELEGRAM_PROXY_RDNS=true` makes the proxy look up host names. It has no effect unless the proxy is already on. When the proxy is on, it accepts 1/true/yes/on and 0/false/no/off, and any other value stops startup.

```ini
TELEGRAM_PROXY_TYPE=socks5
TELEGRAM_PROXY_ADDR=proxy.example.com
TELEGRAM_PROXY_PORT=1080
TELEGRAM_PROXY_USERNAME=proxyuser
TELEGRAM_PROXY_PASSWORD=proxypassword
```

For MTProxy, use:

```ini
TELEGRAM_PROXY_TYPE=mtproxy
TELEGRAM_PROXY_ADDR=proxy.example.com
TELEGRAM_PROXY_PORT=1443
TELEGRAM_PROXY_SECRET=your-mtproxy-secret
```

MTProxy uses Telethon's `ConnectionTcpMTProxyRandomizedIntermediate`. Use a 16-byte secret written as 32 hexadecimal characters, optionally prefixed with `dd`. This mode does not provide FakeTLS support for `ee` secrets.

The selected transport applies to every Telegram connection: the login, the scheduled backup, the real-time listener and the helper scripts. The `python-socks` library needed by SOCKS5 ships in the image and in the PyPI package. Keep proxy secrets in a protected `.env`; do not put them in command arguments or logs.
