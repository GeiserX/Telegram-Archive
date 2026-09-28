# Logins, viewer accounts and share links

This page explains who can see what in the viewer. TLS, proxy setup and the routes that need no login are in [Exposing the viewer safely](exposing.md).

## The viewer starts closed

The viewer serves no archive data until you choose how people log in. You have three options:

- `VIEWER_USERNAME` plus `VIEWER_PASSWORD` for a master login.
- `AUTH_PROXY_HEADER` for identity set by a trusted reverse proxy.
- `ALLOW_ANONYMOUS_VIEWER=true` for an open, read-only viewer.

With none of them set, the container is up and healthy, the login page shows, and sign-in fails. Every data route answers HTTP 503 `Viewer authentication is not configured`.

## Roles

| Role | Who gets it | What it can do |
|------|-------------|----------------|
| master | The `VIEWER_USERNAME` login, or a proxy user listed in `AUTH_PROXY_ADMIN_USERS` | Everything: Admin Settings, viewer accounts, share tokens, the audit log, the Archive Status panel, file paths in the info panel and, when `MEDIA_OPEN_CMD` or `MEDIA_OPEN_PATH_CMD` is set, the media Open buttons |
| viewer | A database account created by the master, or a proxy user | Only the accounts and chats granted to it |
| token | A session opened with a share link | Only the chats of that token |
| anonymous | Every visitor when `ALLOW_ANONYMOUS_VIEWER=true` | Every chat allowed by `DISPLAY_CHAT_IDS`, with downloads. Never gets master rights. |

## The master login

Set both variables in the viewer's environment:

```yaml
services:
  telegram-viewer:
    environment:
      VIEWER_USERNAME: admin
      VIEWER_PASSWORD: change-me-to-a-long-password
```

Password login is on only when both values are non-empty. Leading and trailing spaces are stripped from both. At login the viewer checks database viewer accounts first, then the master credentials. The master comparison takes the same time whether the guess is close or not, so timing reveals nothing.

A request with the header `X-Viewer-Only: true` cannot log in as master or reach master routes. Database viewer accounts can still log in with that header present.

Viewer passwords and share tokens are stored as PBKDF2-HMAC-SHA256 hashes with 600,000 rounds. The master password is never stored; it lives only in the environment.

## Sessions

A successful login sets the cookie `viewer_auth`, which is `HttpOnly` and `SameSite=Lax`.

- A session lasts `AUTH_SESSION_DAYS` days, default 30, counted from login, not from last use. The value must be a whole number: anything else stops the viewer at start.
- Each user holds at most 10 sessions. An 11th login ends the oldest one.
- Sessions live in the database and survive a viewer restart. A sweep every 900 seconds removes expired ones and closes their live connections.
- Logging out ends that session and deletes every push subscription of that user. Other browsers of the same user subscribe again on their next load.

!!! warning "Changing the master password does not log anyone out"
    The viewer does not check existing master sessions against `VIEWER_USERNAME` or `VIEWER_PASSWORD` again. After a change they stay valid until `AUTH_SESSION_DAYS` runs out, or until you [end every session](#end-every-session).

Logins and share-token logins share one rate limit: 15 attempts per client IP in 300 seconds, then HTTP 429. Behind a reverse proxy every client can look like the same IP, so one client can use up the limit for all. [Exposing the viewer safely](exposing.md) shows how to fix this.

## Reset a password

**The master password.** It lives only in the viewer's environment and is never stored. Set a new `VIEWER_PASSWORD` in `.env` and recreate the viewer:

```bash
docker compose up -d telegram-viewer
```

Sessions opened with the old password stay valid until `AUTH_SESSION_DAYS` runs out. To log them out, [end every session](#end-every-session) once the viewer runs with the new password.

**A viewer account.** Open **Admin Settings**, **Viewer Accounts**, edit the account and type a new password of at least 8 characters. Saving ends that account's sessions, so the person signs in again with the new password.

**A share link.** The token is shown once and only its hash is stored, so a lost link cannot be recovered. Revoke or delete the token and create a new one.

**Proxy identity.** The password belongs to the reverse proxy or the identity provider in front of it. The viewer stores none.

## End every session

The master can log out every browser at once, for example after a master password change. This ends the master's sessions, every viewer account's sessions and every share-link session. Accounts and share tokens stay as they are, so people log in again with their current password or link.

Open the gear icon, **Admin Settings**, then the **Sessions** tab. It has two buttons, and each asks you to confirm:

- **End every session** logs out everyone, you included. The page returns to the login.
- **End all but this one** keeps the browser you click it in and logs out everything else, your other browsers included.

Both close the live connections of the ended sessions and delete the push subscriptions of their users. Those users subscribe again after their next login. The audit log records the action as `sessions_ended_all`, or `sessions_ended_all:kept_current` when your session was kept.

The same action is the route `POST /api/admin/sessions/end-all`. Only the master may call it: the master login, or a proxy identity listed in `AUTH_PROXY_ADMIN_USERS`. Viewer accounts and share links get HTTP 403, and so does a request with `X-Viewer-Only: true`. Add `?keep_current=true`, or send the JSON body `{"keep_current": true}`, to keep the session of the cookie you call it with. Without either, your own session ends too:

```bash
curl -X POST -b "viewer_auth=<your session cookie>" \
  "https://archive.example.com/api/admin/sessions/end-all?keep_current=true"
```

The answer names how many sessions ended and whether yours was one of them:

```json
{"success": true, "ended": 4, "current_session_ended": false}
```

When `current_session_ended` is `true`, the answer also clears the cookie. A caller signed in through proxy identity has no session of its own, so the action ends nothing of theirs and the next request signs them in again through the proxy.

## Viewer accounts

The master manages accounts in the viewer itself. Open the gear icon, **Admin Settings**, then the **Viewer Accounts** tab. The **Create Viewer** form has these fields:

| Field | Meaning |
|-------|---------|
| Username | At least 3 characters. It cannot match the master username, in any letter case. |
| Password | At least 8 characters. When editing, leave it empty to keep the current one. |
| Allowed Chats | The chats this user may open. |
| Allowed Accounts | **All accounts**, or only the Telegram accounts you tick. Ticking none means the user sees no chats. |
| Active | Untick to block the account without deleting it. |
| No Downloads | Makes this a [no-download login](#no-download-logins). |

Saving a change to an account, or deleting it, ends all of its sessions, closes its live connections and deletes its push subscriptions. The user has to log in again.

!!! warning "No chats ticked means all chats"
    The form sends an empty **Allowed Chats** box as "all chats". Accounts created through proxy identity start with no chats at all. If you edit one of them and tick nothing, you widen it to every chat. Always tick the chats you mean to grant.

## Share links

A share link gives someone access to a fixed set of chats without an account. In **Admin Settings**, open **Share Tokens** and fill in **Create Share Token**:

- **Label**, optional. It names the session and the audit entries.
- An expiry date and time, optional. You enter it in your browser's local time and it is stored in UTC.
- **Allowed Chats**, required. A token must name at least one chat.
- **No Downloads**, optional.

Press **Create Token**. The viewer shows a 64-character hex token and a ready link, each with a copy button. They are shown once only; the database keeps just a salted hash. The link has this form:

```text
https://archive.example.com/#token=<token>
```

When the link opens, the page signs in with the token and removes it from the address bar. Browsers never send the part after `#` to the server, so the token stays out of access logs. The login page also has a **Share Token** mode where the token can be pasted. A hand-made `?token=` link works too, but that one does reach server access logs.

The session is named `token:<label>`, and it sees only the chats of the token. A token with no label is named `token:token:<id>`, where `<id>` is its number in the token list. Tokens have no account grant; the chat list is the whole scope.

![A viewer session opened through a share link, listing only the one shared chat](../images/screenshots/share-view.png)

To end access:

- **Revoke** or **Delete** the token in the list. Either one ends every session the token created and deletes their push subscriptions. Changing its chats or its no-download setting through the API does the same.
- Do not rely on expiry for this. Expiry only stops new logins with the token. Sessions it already opened keep working until `AUTH_SESSION_DAYS` runs out.

Each token login checks the presented token against every live token in turn, at about 50 ms per token. Delete tokens you no longer need.

!!! note "Share links need the master login"
    Token sessions and database viewer accounts use the session cookie. The viewer checks that cookie only when `VIEWER_USERNAME` and `VIEWER_PASSWORD` are set. With proxy identity alone they cannot be used.

## No-download logins

A viewer account or share token with **No Downloads** ticked can read messages but cannot take files away:

- Media files, thumbnails, chat exports and voice transcripts answer HTTP 403.
- Files in messages show `Not available for this login`, and audio play buttons are disabled.
- The lightbox has no download button, and search skips hits that match only inside a transcript.
- Profile photos still show.

## A second viewer

You can run more than one viewer container against the same archive, for example one for yourself and one limited to a few chats for other people. The stock `docker-compose.yml` has a commented example named `telegram-channel-viewer`. Before you uncomment it, know what the two viewers share and what they do not.

- Both read the same database, so viewer accounts, share tokens, sessions, the audit log and the generated Web Push keys are shared. Each viewer applies its own `DISPLAY_CHAT_IDS`, master login and display settings.
- Browsers send a cookie to every port of a host name. On one host name, a master login on the main viewer is also a master login on the second viewer, and the second viewer only narrows it with its own `DISPLAY_CHAT_IDS`. Give the second viewer its own host name behind the reverse proxy, or use it only for other people.
- Ending sessions reaches every viewer, with a short delay on the other one. Editing or deleting an account, revoking a token or [ending every session](#end-every-session) removes the sessions from the database and from the memory of the viewer where you do it. The other viewer checks each session it holds against the database again once a minute, so its next request after that fails and the browser returns to the login. A browser there that only keeps a live connection open, with no requests, is closed by the sweep that runs every 900 seconds.
- On SQLite the backup sends live events to one address, `VIEWER_HOST` and `VIEWER_PORT`. The second viewer gets none. An open chat there picks up new messages through its 3-second poll only, and it sends no notifications. On PostgreSQL both viewers receive every event.
- The shipped example publishes port 8001 on every interface and carries no database variables, no time zone and none of the hardening of the stock services. Use this instead:

```yaml
  telegram-channel-viewer:
    image: drumsergio/telegram-archive-viewer:8.16.1
    container_name: telegram-channel-viewer
    restart: unless-stopped
    ports:
      - "127.0.0.1:8001:8000"
    environment:
      BACKUP_PATH: /data/backups
      DB_TYPE: ${DB_TYPE:-sqlite}
      DB_PATH: ${DB_PATH:-/data/backups/telegram_backup.db}
      DISPLAY_CHAT_IDS: "-1001234567890,-1009876543210"
      VIEWER_USERNAME: public_viewer
      VIEWER_PASSWORD: choose-another-long-password
      VIEWER_TIMEZONE: ${VIEWER_TIMEZONE:-Europe/Madrid}
    volumes:
      - ./data:/data
    networks:
      - telegram-network
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
```

With PostgreSQL, add the `POSTGRES_*` variables as well, the same as on `telegram-viewer`. Put a reverse proxy in front before you open the port to other machines. See [Exposing the viewer safely](exposing.md).

## How visibility combines

Three rules apply to every request, in this order:

1. `DISPLAY_CHAT_IDS` limits the whole viewer for every role, the master included. If you write a chat id without its `-100` prefix and only the prefixed form exists in the archive, the viewer corrects it at start. The viewer drops live updates for chats outside the list.
2. The account grant of the user.
3. The chat grant of the user or token.

A grant that is not set means no restriction. A grant set to an empty list denies everything. The admin form saves an empty Allowed Chats box as not set, which is why the warning above matters. A chat you may not see answers the same 404 `Chat not found` as a chat that does not exist, so a restricted user cannot probe for chats.

A restricted user never learns about accounts outside its grant. The account list and the message sender chips hide them, and the statistics are recomputed from that user's own chats.

!!! note "Ids stay visible"
    Numeric chat and sender ids are still shown to every user. The chat list, the info panel and the sender dialog display them, and API responses include them.

## Anonymous mode

`ALLOW_ANONYMOUS_VIEWER=true` opens the viewer to anyone who can reach it. Only the literal `true` works, in any letter case; `1` or `yes` leave it off. It takes effect only when neither the master login nor proxy identity is configured.

Every visitor is logged in as a read-only user named `anonymous`. It never has master rights, so there is no Admin Settings, no audit log and no share tokens. It can read every chat allowed by `DISPLAY_CHAT_IDS` and download files. It can also subscribe to push notifications and ask for a voice transcript, which queues a transcription.

## Identity from a reverse proxy

With `AUTH_PROXY_HEADER` set, the viewer takes the username from that request header:

| Variable | Default | Effect |
|----------|---------|--------|
| `AUTH_PROXY_HEADER` | unset | Name of the header that carries the username. Setting it turns proxy identity on. |
| `AUTH_PROXY_ADMIN_USERS` | unset | Comma-separated usernames that get the master role. Exact match. |
| `AUTH_PROXY_DEFAULT_ACCESS` | `none` | `all` gives new proxy users every chat. Any other value gives them no chats until the master grants some. |

A proxy user who is not an admin is created as a viewer account on first visit. The master edits its grants in **Viewer Accounts** like any other account; read the warning in [Viewer accounts](#viewer-accounts) before you do. A disabled account gets HTTP 403. Non-admin proxy users are stored as viewer accounts in the database. If the viewer cannot reach the database, those requests get HTTP 503.

A request without the header falls back to the session cookie, so password logins keep working next to the proxy when the master login is also set.

!!! danger "The proxy must own the header"
    The viewer trusts the header as given. Anyone who can send it straight to the viewer can log in as any user, including an admin. The proxy must strip or overwrite that header on every request, and the viewer must not be reachable around the proxy. See [Exposing the viewer safely](exposing.md).

## The audit log

The viewer records these events in the database:

- Logins, failed logins and logouts.
- Share-token logins, successful and failed.
- Admin changes: viewer accounts created, updated or deleted, share tokens created, updated or deleted, and settings changed.

Login entries carry the username, role, client IP and browser user agent. Ordinary reads of chats and messages are not logged.

The master reads the log in **Admin Settings**, tab **Audit Log**. It shows the 50 newest entries and filters by action. The API route `GET /api/admin/audit` returns up to 500 entries per request and filters by `username` and `action`.

With a reverse proxy in front, login and share-token entries show the proxy's IP. To log the real client IP on them, configure the viewer to trust forwarded headers, as described in [Exposing the viewer safely](exposing.md). Logout and admin-change entries always record the address the connection came from, so behind a proxy they show the proxy's IP.
