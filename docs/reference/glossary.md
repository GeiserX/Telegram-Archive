# Glossary

Each entry below links to the page that explains the term in full.

## Anonymous mode

With `ALLOW_ANONYMOUS_VIEWER=true` and no other login method set, every visitor is logged in without a password. The visitor gets the viewer role under the user name `anonymous`, sees every chat allowed by `DISPLAY_CHAT_IDS` and can download files. See [Anonymous mode](../viewer/access.md#anonymous-mode).

## The archive

The saved data: the media folder and session files under the data directory, plus the database, which is a file in that directory on SQLite and a separate PostgreSQL volume otherwise. The backup writes it and the viewer reads it. The product itself is called Telegram Archive. See [Backing up the archive](../operations/backup-and-restore.md#where-everything-lives).

## Archive status

A page of the viewer's main menu that only the master login can open. It says whether everything runs, then has sections for the backup, live sync per Telegram account, media, transcription, the database and the statistics. See [Archive status](../viewer/using-the-viewer.md#archive-status).

## Audit log

The record of logins, failed logins, logouts, share-token logins and admin changes, kept in the database. The master login reads it under **Admin settings > Activity**. Ordinary reads of chats are not logged. See [The audit log](../viewer/access.md#the-audit-log).

## The backup

The process that talks to Telegram and writes the archive. It runs in the backup container, or as a native `telegram-archive` process, and it is the only part that holds a Telegram session while the archive runs. The login command and the maintenance scripts that talk to Telegram use the same session, so stop the backup before you run them. See [Two ways to run the backup](../configuration/schedule.md#two-ways-to-run-the-backup).

## Backup run

One pass of the backup over every configured Telegram account, one account after another. The `schedule` command starts one run at startup and one on each `SCHEDULE` tick. The `backup` command does one run and exits. After first mention, the site calls it a run. See [What one run does](../configuration/schedule.md#what-one-run-does).

## Chat id

The numeric id Telegram gives a chat, written with its type prefix: `-100` for supergroups and channels, `-` for basic groups, none for users and bots. This prefixed form is also called the marked chat id. Every id list in the settings uses it. See [Chat ids](../configuration/choosing-chats.md#chat-ids).

## Chat ref

A random 22-character handle the archive gives each chat when it first stores it. The handle never changes. The viewer uses it in links, API routes, push notifications and chat grants in place of the chat id. See [Links to a message](../viewer/using-the-viewer.md#links-to-a-message).

## Data directory

The directory mounted at `/data` in both containers. The stock compose file uses `./data` on the host. It holds the session files, the database and the media, and it must belong to uid 1000. See [Create the data directory](../getting-started/docker.md#3-create-the-data-directory).

## Deletion mode

How a deletion is applied, set by `DELETION_MODE`. The default is `soft`, which marks the message deleted and keeps it. `hard` removes the row. It governs both the listener and the sync pass. See [Deletions](../configuration/listener.md#deletions).

## Event webhook

An HTTP request the backup sends to another service when the real-time listener applies an edit or a deletion. It needs the listener. The sync pass sends no webhooks. See [Event webhook](../configuration/event-webhook.md).

## FloodWait

Telegram's order to wait a given number of seconds before the next request. The backup waits, then retries the call a bounded number of times. If the wait is longer than `MAX_FLOOD_WAIT_SECONDS`, the backup leaves that chat or file for the next run. FloodWaits are normal during a large first backup. See [Rate limits and retries](../configuration/schedule.md#rate-limits-and-retries).

## Gap-fill

A scan for holes between two stored message ids that are further apart than `GAP_THRESHOLD`, followed by a fetch of the missing range. The scheduler runs it after each backup run when `FILL_GAPS=true`, and the `fill-gaps` command runs it by hand. See [Gap-fill](../configuration/schedule.md#gap-fill).

## Heartbeat

A file the `schedule` command rewrites every 30 seconds, which the backup container's health check reads. It shows that the process is alive. It does not show whether a backup run worked. See [Backup container](../operations/troubleshooting.md#backup-container).

## Indexed mode

The way to declare more than one Telegram account, with numbered `TG_ACCOUNT_<N>_*` variables. Setting any of the five credential variables, `API_ID`, `API_HASH`, `PHONE_NUMBER`, `LABEL` or `SESSION_NAME`, switches the install out of single-account mode, and the plain `TELEGRAM_*` credentials are then ignored. A per-account filter override on its own does not. See [Single-account and indexed mode](../configuration/multiple-accounts.md#single-account-and-indexed-mode).

## Live updates

Events the backup sends to the viewer so an open chat changes without a reload and notifications can fire. They come from the real-time listener and from transcript status changes. See [Live updates and notifications](../viewer/live-updates.md).

## Mass-operation protection

A per-chat rate limit on edits and deletions applied by the real-time listener. When a chat goes over it, the listener discards that chat's edits and deletions for one more window. It does not cover the sync pass. See [Mass-operation protection](../configuration/listener.md#mass-operation-protection).

## Master login

The `VIEWER_USERNAME` and `VIEWER_PASSWORD` credentials, and the session opened with them. It has the master role, which can do everything, including Admin settings and Archive status. A proxy user listed in `AUTH_PROXY_ADMIN_USERS` gets the same role. See [The master login](../viewer/access.md#the-master-login).

## No-download login

A viewer account or share token with downloads off. It can read messages, but media files, thumbnails, exports and transcripts answer HTTP 403, and files show `hidden for this login`. See [No-download logins](../viewer/access.md#no-download-logins).

## One client per session

The rule that only one process may connect to Telegram with a given session file at a time. Two clients on one session can make Telegram invalidate the login, so stop the backup service before any other command that connects. See [One client per session](../getting-started/telegram-login.md#one-client-per-session).

## One-shot backup

The `backup` command: one backup run over every account, then exit. It starts no listener, runs no gap-fill and writes no heartbeat. See [Two ways to run the backup](../configuration/schedule.md#two-ways-to-run-the-backup).

## Position

The id of the newest message already archived, stored per chat and per Telegram account. A backup run fetches only messages above it, and it never moves back. See [Where a run picks up](../configuration/schedule.md#where-a-run-picks-up).

## Proxy identity

A login method where a trusted reverse proxy puts the user name in the header named by `AUTH_PROXY_HEADER`. Users listed in `AUTH_PROXY_ADMIN_USERS` get the master role. Others become viewer accounts on first visit. See [Identity from a reverse proxy](../viewer/access.md#identity-from-a-reverse-proxy).

## Real-time listener

A connection per Telegram account that stays open between backup runs and writes new messages, edits, deletions and other changes as they happen. It runs only inside the `schedule` command with `ENABLE_LISTENER=true`. After first mention, the site calls it the listener. See [Real-time listener](../configuration/listener.md).

## Role

What a viewer session may do: `master`, `viewer` or `token`. The master login gets `master`, viewer accounts and anonymous visitors get `viewer`, and share-token sessions get `token`. See [Roles](../viewer/access.md#roles).

## Scheduled backup

The normal mode of operation: the `schedule` command, which starts a backup run at startup and on each `SCHEDULE` tick until stopped. The stock compose file runs it in the backup container. See [When backups run](../configuration/schedule.md#when-backups-run).

## Session file

The file under `SESSION_DIR` that holds a Telegram account's login, one per account. Anyone who has it can read and send messages as that account, so keep it private and include it in backups. See [Session files](../getting-started/telegram-login.md#session-files).

## Share link

The URL of the form `https://<host>/#token=<token>` that carries a share token. Opening it signs in with the token and removes the token from the address bar. See [Share links](../viewer/access.md#share-links).

## Share token

A 64-character secret the master login creates under **Admin settings > Share tokens**, bound to a fixed set of chats. It can carry a label, an expiry and downloads off, and its sessions get the `token` role. See [Share links](../viewer/access.md#share-links).

## Sync pass

The optional step of a backup run, on when `SYNC_DELETIONS_EDITS=true`, that re-reads every archived message to pick up edits and deletions. It applies `DELETION_MODE`. Mass-operation protection does not cover it, and it fires no event webhook. It is expensive on large archives. See [SYNC_DELETIONS_EDITS](../configuration/schedule.md#sync_deletions_edits).

## Telegram account

One archived Telegram identity, declared by the `TELEGRAM_*` variables or by `TG_ACCOUNT_<N>_*` in indexed mode. Each has its own session file and its own position in every chat. See [Multiple accounts](../configuration/multiple-accounts.md).

## The viewer

The web app that reads the archive and serves it to browsers. It never contacts Telegram, and it serves no data until a login method is set. See [Using the viewer](../viewer/using-the-viewer.md) and [The viewer starts closed](../viewer/access.md#the-viewer-starts-closed).

## Viewer account

A login stored in the database with the viewer role, limited to the Telegram accounts and chats granted to it. The master login creates it under **Admin settings > Viewers**, or the viewer creates it on first visit for a proxy user who is not an admin. See [Viewer accounts](../viewer/access.md#viewer-accounts).

## Viewer session

What a successful viewer login opens, held in the `viewer_auth` cookie and in the database. It lasts `AUTH_SESSION_DAYS` from login, and each user holds at most 10. It is separate from the Telegram session file. See [Sessions](../viewer/access.md#sessions).
