# HTTP API

The viewer's web app uses a JSON API, and scripts can call the same routes. This page covers how to log in, which login each route needs, and each route's parameters and responses.

## Basics

- The viewer listens on port 8000. Every path on this page is relative to that address, or to the address of your reverse proxy.
- Chats are addressed by an opaque `ref`, never by the Telegram chat id. Get refs from `GET /api/chats`.
- Request bodies are JSON. Send `Content-Type: application/json`.
- Dates are ISO 8601. A timestamp without an offset is read as UTC.

The viewer serves its own generated OpenAPI document at `/openapi.json`, and it needs no login. The `/docs` and `/redoc` pages also exist, but they render blank, because the viewer's content security policy allows no scripts from other hosts. The document lists paths and query parameters, but it has no request bodies, no response shapes and no permission rules. Use this page for those.

## Authentication

There are no API keys for the `/api` routes. A script logs in, keeps the session cookie and sends it back on every request.

The viewer supports three login modes. [Logins, viewer accounts and share links](../viewer/access.md) explains how to turn each one on.

| Mode | Turned on by | What a script does |
|------|--------------|--------------------|
| Password | `VIEWER_USERNAME` and `VIEWER_PASSWORD` both set | Posts to `/api/login` and keeps the cookie |
| Proxy identity | `AUTH_PROXY_HEADER` set | Goes through the proxy, which sets the identity header. Without the header the viewer falls back to the session cookie. |
| Anonymous mode | `ALLOW_ANONYMOUS_VIEWER=true` with neither of the above | Calls routes directly with no login. It never gets master rights. |

With no mode configured, every protected route answers 503 `Viewer authentication is not configured`.

### Log in with a password

`POST /api/login` takes `{"username": "...", "password": "..."}`. It checks viewer accounts first, then the master credentials from the environment. On success it sets the `viewer_auth` cookie and answers:

```json
{"success": true, "role": "master", "username": "admin"}
```

`role` is `master` or `viewer`.

=== "curl"

    ```bash
    curl -s -c jar.txt -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"your-password"}' \
      http://localhost:8000/api/login

    curl -s -b jar.txt http://localhost:8000/api/chats
    ```

=== "Python"

    ```python
    import requests

    s = requests.Session()
    r = s.post(
        "http://localhost:8000/api/login",
        json={"username": "admin", "password": "your-password"},
    )
    r.raise_for_status()
    chats = s.get("http://localhost:8000/api/chats").json()
    ```

Login errors:

| Status | Meaning |
|--------|---------|
| 400 | The body is not JSON, or a field is missing |
| 401 | `Invalid credentials` |
| 429 | Too many attempts from this client IP |
| 503 | The database was unreachable and the master credentials did not match |

When password login is off but anonymous mode is on, the route answers `{"success": true, "message": ...}` and sets no cookie.

### The session cookie

The cookie `viewer_auth` holds an opaque random session key. It is `HttpOnly` and `SameSite=Lax`, and lasts `AUTH_SESSION_DAYS` days, default 30. The viewer marks it `Secure` when `SECURE_COOKIES=true`. When `SECURE_COOKIES` is unset, it marks it `Secure` only if the login request came over https or carried `X-Forwarded-Proto: https`. A client does not send a `Secure` cookie over plain http. If the cookie is `Secure`, call the viewer over https.

Sessions are stored in the database and survive a viewer restart. Each user holds at most 10 sessions. The 11th login ends the oldest one, so a script that logs in on every run can push a browser session out. Log out at the end of each run, or reuse one cookie jar.

### Log in with a share token

A share token is a 64-character hex string created by the master. `POST /auth/token` takes `{"token": "..."}` and sets the `viewer_auth` cookie:

```bash
curl -s -c jar.txt -H 'Content-Type: application/json' \
  -d '{"token":"<64 hex characters>"}' \
  http://localhost:8000/auth/token
```

```json
{"success": true, "role": "token", "username": "token:family", "no_download": false}
```

`username` is `token:` followed by the token's label. A token with no label gets `token:token:<id>`, so give every token a label. The session sees only the token's chats. Invalid, revoked and expired tokens all answer 401 `Invalid or expired token`. Each use updates the token's `last_used_at` and `use_count`.

A share link in the browser carries the token as `#token=<value>`. The web app reads it and posts it to `/auth/token` for you.

### Check the session

`GET /api/auth/check` needs no login. It answers:

```json
{"authenticated": true, "auth_required": true, "role": "viewer", "username": "alice", "no_download": false}
```

Proxy users also get `"proxy_auth": true`. When no login mode is configured, the answer carries `"setup_required": true`.

### Log out

`POST /api/logout` ends the session named by the cookie, closes that session's WebSocket connections, deletes every push subscription of that user and clears the cookie. It always answers `{"success": true}`. When the request carried a session cookie, the answer also sends `Clear-Site-Data: "cache"` so the browser drops the media it kept. A request without the cookie, such as another site's form post, does not get the header. That header empties the browser's HTTP cache for the viewer only. The service worker, cookies and local settings stay, and the viewer's static files download again on the next visit. Browsers apply it over HTTPS only.

```bash
curl -s -b jar.txt -X POST http://localhost:8000/api/logout
```

The master can end other people's sessions through the admin routes. See [End sessions](#end-sessions).

### Rate limit

`/api/login` and `/auth/token` share one limit: 15 attempts per client IP in 5 minutes, then 429. The client IP is the socket peer unless `TRUST_PROXY_HEADERS=true`, in which case the viewer reads `X-Forwarded-For` or `X-Real-IP`. The only other rate limit is on asking for a transcript. See [Transcripts](#transcripts). [Exposing the viewer safely](../viewer/exposing.md) explains the proxy setup.

## Who can call what

| Level | Who passes | Routes |
|-------|-----------|--------|
| Public | Anyone | `GET /`, `GET /sw.js`, `/static/*`, `GET /api/health`, `GET /api/auth/check`, `GET /api/push/config`, `GET /api/notifications/settings`, `POST /api/login`, `POST /api/logout`, `POST /auth/token`, `/docs`, `/redoc`, `/openapi.json` |
| Any login | master, viewer, share token, anonymous | Accounts, chat list, folders, stats, search, tags, change feed, transcription status, push subscriptions |
| Chat entitlement | Any login that can see the chat | Every route with `{chat_ref}` in the path |
| Master only | The master login or a proxy admin | `/api/status`, `/api/stats/refresh`, `/api/admin/*`, `/media/open/*`, `/media/open-path/*` |
| Internal | Other containers, not clients | `/internal/push`, `/api/transcriptions/callback` |

Master routes answer 403 `Admin access required` to everyone else. They also answer 403 when the request carries `X-Viewer-Only: true`, even with a master cookie. That header also makes the master credentials fail at login.

A chat route answers the same 404 `Chat not found` for a ref that does not exist, a malformed ref and a chat the caller may not see. What a caller may see combines the operator's `DISPLAY_CHAT_IDS` filter with the caller's `allowed_accounts` and `allowed_chat_refs` grants.

Logins with `no_download` set get 403 `Downloads disabled for this account` on media bytes, thumbnails, transcript routes and export. In message payloads their media `file_path` and `url` are `null`, `downloaded` is `false`, transcripts are removed and a shared contact's `raw_data.contact.vcard` is left out. Avatars stay available to them.

## Health and status

| Method and path | Login | Purpose |
|-----------------|-------|---------|
| `GET /api/health` | Public | Whether the database answers |
| `GET /api/stats` | Any | Archive totals and runtime flags |
| `GET /api/status` | Master | Backup, listener, media and database health in one call |
| `POST /api/stats/refresh` | Master | Recalculate the stats now and return them |

`GET /api/health` answers `{"status": "ok", "database": "connected"}`. When the database probe fails it answers HTTP 503 with `{"status": "degraded", "database": "unreachable"}`. The viewer container's health check calls this route.

`GET /api/stats` answers the stored daily figures and some runtime settings and flags:

| Kind | Fields |
|------|--------|
| Daily figures | `chats`, `messages`, `media_files`, `total_size_mb`, `stats_calculated_at`, `last_backup_time` |
| Settings and flags | `timezone`, `stats_calculation_hour`, `show_stats`, `listener_active`, `listener_active_since`, `backup_in_progress`, `push_notifications`, `push_enabled`, `enable_notifications` |

For a restricted login the counts cover only its chats. `POST /api/stats/refresh` recounts the whole archive, which takes a while on a large one. Do not call it on a schedule.

`GET /api/status` holds counts and timestamps only, never chat names or content. It is sent with `Cache-Control: private, no-store`.

```bash
curl -s -b jar.txt http://localhost:8000/api/status
```

```json
{
  "backup": {"last_run": "2026-09-28T03:00:12", "in_progress": false},
  "stats_calculated_at": "2026-09-28T00:00:05",
  "listeners": [{"account_id": 1, "active": true, "active_since": "2026-09-27T18:04:40"}],
  "media": {"downloaded": 1200, "pending": 3, "exhausted": 0, "skipped": 45},
  "database": {"backend": "postgresql", "size_bytes": 734003200}
}
```

`backend` is `sqlite` or `postgresql`. A monitoring script can alert when `backup.last_run` is too old, when a listener is not `active`, or when `media.exhausted` grows.

## Accounts, chats and folders

All of these need any login.

| Method and path | Parameters | Response |
|-----------------|-----------|----------|
| `GET /api/accounts` | None | `{accounts: [{id, label}]}`. `label` is `TG_ACCOUNT_<N>_LABEL` when set; otherwise `default` for the first account and `account<N>` for the others. |
| `GET /api/chats` | `limit` default 50, 1 to 1000. `offset`. `search` matches title, name or username. `archived` true or false. `folder_id`. | `{chats, total, limit, offset, has_more}`. Each chat carries `ref`, `avatar_url`, `accounts` and `preview`, its newest message for the list's second line. See [The chat list preview](#the-chat-list-preview). |
| `GET /api/chats/{chat_ref}` | None | One chat in the shape of a list row, without `preview` |
| `GET /api/folders` | None | `{folders}` with chat counts limited to what the caller can see |
| `GET /api/archived/count` | None | `{count}` |

### The chat list preview

`preview` is the chat's newest message that was not deleted in Telegram, or `null` when the chat has none. It is read from the copy of the chat the row's `ref` opens, under the same restrictions as the chat's messages, so it never shows a message the caller could not open in the chat. It carries text and a kind only, never a file or a transcript. `GET /api/chats/{chat_ref}` does not return it.

| Field | Value |
|-------|-------|
| `message_id` | The message's id in the chat |
| `date` | When it was sent, UTC |
| `text` | The text on one line, cut to 100 characters with an ellipsis. A poll with no text gives its question. A service row gives its sentence, for example `Esme joined the group`. `null` when there is no text. |
| `sender` | `You` for the account's own message in a group or a private chat, the sender's first name for anyone else in a group, and `null` in a channel, for the other person in a private chat and for a service row |
| `kind` | `text`, `service`, `poll`, the archive's media type (`photo`, `video`, `video_note`, `voice`, `audio`, `animation`, `sticker`, `document`, `geo`, `geo_live`, `venue`, `contact`, `dice`, `game`, `invoice`, `story`, `giveaway`, `giveaway_results`, `webpage`, `unsupported`), or `message` for a message with neither text nor a media record |
| `outgoing` | Whether the account sent it |
| `action`, `action_title` | For a service row with no stored sentence, from before 7.28: its `action_type` and `new_title`, so a client can word it. `null` otherwise. |

Example:

```json
"preview": {
  "message_id": 1288,
  "date": "2026-09-30T08:57:00",
  "text": "Perfect, save me a seat",
  "sender": "Esme",
  "kind": "text",
  "outgoing": false,
  "action": null,
  "action_title": null
}
```

## Messages

All of these need a login that can see the chat.

| Method and path | Parameters | Response |
|-----------------|-----------|----------|
| `GET /api/chats/{chat_ref}/messages` | `limit` default 50, 1 to 500. `offset`. `search`. `topic_id`. `deleted_only` and `edited_only`, default false. Cursor: `before_date` plus `before_id`, `before_id` alone, or `after_id`. See [Paging through messages](#paging-through-messages). | A JSON array of messages, newest first |
| `GET /api/chats/{chat_ref}/messages/{message_id}/versions` | `limit` default 100, up to 500 | Earlier versions of an edited message, newest first: `[{chat_id, message_id, text, date, captured_at, source, entities, rich_message}]`, with `media` on a version whose photo or file an edit replaced. See [Message versions](#message-versions) |
| `GET /api/chats/{chat_ref}/pinned` | None | Pinned messages, newest first, each with the `snapshots`, `reactions`, `removed_reactions`, `reaction_history` and `reaction_history_omitted` the messages route gives it |
| `GET /api/chats/{chat_ref}/messages/by-date` | `date` as `YYYY-MM-DD`. `timezone` as an IANA name, optional; defaults to the viewer's configured timezone. `topic_id`. | The first message on or after local midnight of that day, or 404 |
| `GET /api/chats/{chat_ref}/messages/dates` | `month` as `YYYY-MM` and `timezone`, both required. `topic_id`. | `{month, timezone, topic_id, dates: ["YYYY-MM-DD", ...]}` |
| `GET /api/chats/{chat_ref}/topics` | None | `{topics}` for a forum chat |
| `GET /api/chats/{chat_ref}/stats` | None | `{chat_id, messages, media_files, total_size_bytes, total_size_mb, first_message_date, last_message_date, deleted_messages, edited_messages}`, cached for 60 seconds. `deleted_messages` counts the messages deleted in Telegram that the archive kept, `edited_messages` the messages edited at least once |
| `GET /api/chats/{chat_ref}/avatars` | None | `[{photo_id, seen_at, url, available}]` |

### Paging through messages

Offset paging gets slower as the offset grows. For long walks use the cursor: pass the `date` and `id` of the last message you received as `before_date` and `before_id`.

```bash
curl -s -b jar.txt \
  'http://localhost:8000/api/chats/<ref>/messages?limit=200&before_date=2026-09-01T10:15:00&before_id=48213'
```

A lone `before_id=N` returns messages with ids below N. `after_id=N` returns messages with ids above N.

`deleted_only=true` keeps only the messages deleted in Telegram that the archive kept, the rows with `is_deleted` set to 1, newest first. It works with or without `search`: without it you get every kept deletion of the chat, with it the deletions that match. It combines with `topic_id` and the cursors like the other filters. It is a read and stays inside the chat the ref names, so a viewer restricted to some chats or accounts, or a share link, gets the same 404 for another chat as without it.

```bash
curl -s -b jar.txt \
  'http://localhost:8000/api/chats/<ref>/messages?limit=50&offset=0&deleted_only=true'
```

`edited_only=true` keeps only the edited messages, newest first, the same way. A message counts as edited when Telegram marks it (`edit_date` is set and `edit_hide` is not 1) or the archive kept at least one earlier version of it (`version_count` above 0): either can hold without the other, for a message first archived after its edit or an earlier empty text the archive filled in. `edit_hide` is Telegram's flag for that `edit_date`: 1 when Telegram says the edit is not to be shown, which it does when only the reactions changed, 0 when it shows, and null when the source did not report it: a message archived before the archive kept the flag, or one from a Telegram export import. A null flag counts as shown. It is the rule the viewer's pencil follows, and `edited_messages` in `/api/chats/{chat_ref}/stats` counts the same rows. Like `deleted_only`, it is a read that combines with `search`, `topic_id` and the cursors and stays inside the chat the ref names.

```bash
curl -s -b jar.txt \
  'http://localhost:8000/api/chats/<ref>/messages?limit=50&offset=0&edited_only=true'
```

Each message nests its media. `media.id` is the media key, `{message_id}_{type}`, and `media.url` is `/media/{chat_ref}/{key}`. `media.downloaded` is false for a row waiting for a download, such as a file `check-media --repair` marked to download again; such a row can keep its `file_path`. A kind with no file (`geo`, `contact`, `poll`, `venue`, `geo_live` and the others in [Media](../configuration/media.md)) has `media.url` null, even when an older release left a `file_path` on it. What it holds is in `raw_data` under a key named after the kind:

| Key | Fields |
|-----|--------|
| `geo` | `lat`, `long`, `accuracy_radius` |
| `contact` | `first_name`, `last_name`, `phone_number`, `vcard`, `user_id` (0 when the number has no Telegram account) |
| `venue` | `title`, `address`, `provider`, `venue_id`, `venue_type`, `lat`, `long`, `accuracy_radius` |
| `geo_live` | `lat`, `long`, `period`, `heading`, `accuracy_radius`, `at` (when that position was current, UTC), `earlier` (positions earlier reads saw, oldest first) |
| `poll` | `question`, `answers`, `closed`, `public_voters`, `multiple_choice`, `quiz`, `results` |

A field Telegram did not send is left out, so a location Telegram sent without a point has no `lat` or `long`. A message with no media row (the listener writes none for these kinds) still has its key in `raw_data`. A reply also carries `reply_to_media_type`, the kind of the message it answers, which comes from that message's `raw_data` when it has no media row, and `reply_to_media_title`, a venue's title, when it answers a venue. `sender_avatar_url` points at `/media/avatar/{chat_ref}/{message_id}`. Transcripts are attached when transcription is on.

In `GET /api/chats/{chat_ref}/messages` each message carries its reactions in two lists. `reactions` holds the live ones, one entry per emoji with its `count`. `removed_reactions` holds the reactions taken back that the archive kept, newest first: one entry per emoji, from its latest drop, with `emoji`, `count` (how many went), `count_before` (the count before the drop, above `count` when some stayed), `removed_at` (when the archive noticed, in UTC) and `back_at` (when an emoji taken back to zero was seen again, or `null`). An emoji that came back is in both lists. `reaction_history` lists every state of the message's reactions the archive kept, oldest first: `emoji`, `count` (0 when taken back), `previous_count` (`null` for the first state), `observed_at` and `source` (`listener`, `backup`, or `baseline` for a state copied from the reactions kept before 9.0). This route caps `reaction_history` so a busy post stays small: it holds the message's 20 newest states, plus each emoji's newest state, its latest drop and the first state with a count after that drop, so `removed_reactions` reads the same as from every state. `reaction_history_omitted` is how many older states it left out, 0 when the list is complete. Both exports return every state. Neither list names a person, because the archive stores counts per emoji. `reactions[].user_ids` can still list ids from rows written one per reactor before 7.23.0, until the backup or the listener reconciles that message again. `/messages/by-date` returns only `reactions`.

In `GET /api/chats/{chat_ref}/messages` each message also carries `snapshots`, the newest state the archive kept of its poll and of its link preview. See [Poll and link preview snapshots](#poll-and-link-preview-snapshots).

### Poll and link preview snapshots

`raw_data.poll` and `raw_data.webpage` hold a poll and a link preview as the archive first captured them, and never change. When a later read shows another state, the archive adds a snapshot: a poll's votes, results or closing, or the preview card's fields. A read that shows the newest kept state adds nothing. The listener writes them from edit events and poll updates, and the backup and the sync from their reads. Live locations are not followed.

`snapshots` is an object keyed by kind, `poll` or `preview`, and empty when the archive kept no later state. Each entry describes the newest state of that kind:

| Field | Meaning |
|-------|---------|
| `payload` | The whole state, in the shape of `raw_data.poll` or `raw_data.webpage`. A poll state from an update that carried the results alone holds only `results`. |
| `observed_at` | When the archive saw it, by the archive's own clock, in UTC. |
| `source` | The path that saw it: `listener`, `sync` or `backup`. |
| `count` | How many states of that kind the archive kept after the first capture. |
| `differs_from_first` | False when the newest state is the same as the first capture again, for example after a vote was taken back. |

The snapshots read through the same chat and account as the messages: a viewer restricted to some accounts sees only the snapshots its accounts wrote. Both exports list every state, oldest first.

### Message versions

Each earlier version of an edited message has these fields:

| Field | Meaning |
|-------|---------|
| `text` | The text of that version. |
| `date` | When that text became current, by Telegram's clock: the send time for the original, the edit time for each later one. An edit time Telegram hides (a reaction) is not an edit, so a text that carried one is dated at the send time. |
| `captured_at` | When the archive saw it, by the archive's own clock. |
| `source` | The path that saw it: `listener`, `sync`, `backup` or `import`. Null for a version archived before the archive kept it: unknown. |
| `entities` | The formatting of that version, in the shape of the message's `raw_data.entities`: `[{type, offset, length, ...}]`. Null when it had none, or for a version archived before the archive kept it. |
| `rich_message` | The block tree of a Rich Text Editor message, in the shape of the message's `raw_data.rich_message`. Null when that version had none. |
| `media` | Present only when an edit replaced the message's photo or file: the media this version was shown with, as `[{type, file_name, file_size, mime_type, width, height, duration, downloaded, skip_reason, first_seen, date, captured_at, source, url}]`. `skip_reason` says why a file was not downloaded (`oversize` or `filtered`, or null), and `first_seen` is when the archive first recorded that media (null when unknown). `url` is `/media/{chat_ref}/{message_id}_v{n}`, where `n` counts the message's earlier media in the order they were kept, from 1. It is null when the file was not downloaded; a login without downloads gets `url` null and `no_download` true. |
| `media_only` | True on an entry that holds only earlier media, when the text version of that moment could not be written. Its `text` is null. |

Only the listener sees each edit as it happens, and only while it runs: edits made while it was away reach it as one. The sync, a backup and an import read the text current at that moment, so several edits between two reads leave one version. When any version has one of those sources, or no source, the number of versions is a lower bound on the number of edits. The same holds when the oldest version's `date` is later than the message's `date`: the archive first saw the message already edited.

## Search, tags and the change feed

All of these need any login. Results cover only chats the caller can see.

| Method and path | Parameters | Response |
|-----------------|-----------|----------|
| `GET /api/search/messages` | `q` required, 1 to 500 characters. `limit` default 20, up to 100. `offset` up to 5000. | `{query, limit, offset, has_more, indexed, results}` |
| `GET /api/tags/{tag}` | `scope` is `chat`, `mine` or `all`, default `all`. `chat_ref`, required with `scope=chat`. `limit` default 50, up to 200. `offset`. | `{tag, results, has_more, truncated}` |
| `GET /api/changes` | `since` ISO, inclusive. `before` ISO cursor, exclusive. `limit` default 50, up to 200. `chat_ref` for one chat. `reactions=true` adds reactions taken back. | `{changes, next_before}` |

`/api/search/messages` is a word-prefix full-text search across chats, newest first. Each result has `id`, `date`, `text`, `sender_name`, `sender_account_id`, `is_deleted`, `topic_title`, `matched_in` and a `chat` object with `ref`, `title`, `first_name`, `last_name`, `username`, `type`, `is_forum` and `avatar_url`.

`/api/tags/{tag}` takes a `#hashtag` or a `$CASHTAG` and answers 400 for anything else. Encode `#` as `%23` in the URL:

```bash
curl -s -b jar.txt 'http://localhost:8000/api/tags/%23holiday?scope=all&limit=50'
```

`/api/changes` lists deletions, edits and new transcripts, newest first, and with `reactions=true` the reactions taken back. Each change has `kind`, `date`, `chat` with `ref`, `title` and `type`, `message_id` and `sender_name`, plus:

| `kind` | Extra fields |
|--------|-------------|
| `deleted` | `text` |
| `edited` | `old_text`, `new_text` |
| `transcript` | `text`, `language` |
| `reaction` | `text` (the message's current text), `emoji`, `count` (how many went), `count_before`, `count_after` |

A `reaction` row is one drop the archive kept in the reaction history: a count below the one before it, dated when the archive noticed. A partial drop and a complete one both list, and so does a removal kept before 9.0. A drop that two accounts holding one channel both saw lists once. They are left out without `reactions=true`, since they come and go far more often than the rest.

`chat_ref` narrows the feed to one chat. For a channel or group that several accounts hold, that is every copy the caller may see, and each change is listed once under the copy the feed for every chat lists it under, so a row's `chat.ref` can name another account's copy of the same chat. A private chat narrows to the copy that ref names, since each account's private chat with one person is a different conversation. A chat the caller cannot see answers 404, the same as an unknown ref. Paging works the same way.

Hard deletions never appear, and no-download logins get no transcript rows. The response is sent with `Cache-Control: private, no-store`. To walk the feed, pass `next_before` from each answer as `before` on the next request until it comes back empty. To poll for new changes, pass the time of your last poll as `since`:

```bash
curl -s -b jar.txt 'http://localhost:8000/api/changes?since=2026-09-28T00:00:00Z&limit=200'
```

## Media

A media key has the form `{message_id}_{type}`, for example `42_photo` or `7_video_note`. Get keys from message payloads or from the media gallery route. An earlier photo or file an edit replaced has the key `{message_id}_v{n}`, from the `url` of its [message version](#message-versions): `n` is its place among the message's earlier media, 1 for the first one kept. It works on the file, thumbnail and open routes, and the transcript routes read its transcripts; asking for a new transcript of it answers 404. A key for an earlier media the message does not have answers 404.

| Method and path | Login | Purpose |
|-----------------|-------|---------|
| `GET /media/{chat_ref}/{media_key}` | Chat entitlement | The original file. Add `?download=1` to force an attachment. |
| `GET /media/thumb/{size}/{chat_ref}/{media_key}` | Chat entitlement | A generated WebP thumbnail. `size` is 200 or 400. |
| `GET /media/avatar/{chat_ref}` | Chat entitlement | The chat's avatar. `photo_id` picks an older one. |
| `GET /media/avatar/{chat_ref}/{message_id}` | Chat entitlement | The avatar of that message's sender |
| `GET /api/chats/{chat_ref}/media` | Chat entitlement | The chat's media gallery |
| `GET /api/chats/{chat_ref}/media/counts` | Chat entitlement | `{type: count}` for the chat |
| `POST /media/open/{chat_ref}/{media_key}` | Master | Run `MEDIA_OPEN_CMD` on the file, on the viewer's host |
| `POST /media/open-path/{chat_ref}/{media_key}` | Master | Run `MEDIA_OPEN_PATH_CMD` on the file, on the viewer's host |

The viewer serves files a browser can show inline. It sends other files, and any request with `download=1`, as an attachment.

Media, thumbnails and avatars are sent with `Cache-Control: private, no-cache`, an `ETag` and a `Last-Modified`. The browser may keep a copy, but it asks the server before each reuse, and the viewer runs the same login and chat checks on that request. A session that still has access gets `304 Not Modified` and no body, so the file is not sent again. A logged-out browser gets 401, never the kept copy. Copies cached by an older release, which did not ask, can still be reused until their old lifetime runs out, up to a day for thumbnails and avatars; logging out once over HTTPS clears them. Editing a viewer, or changing a token's chats or downloads, ends its sessions, so that browser gets 401 too. A session that no longer sees the chat gets 404. Originals and thumbnails answer 403 to a login whose downloads are off; avatars stay available to it. When its session ends, the viewer page reloads itself at the same address, so the next login on the same tab does not see the chat that was open until the server allows it again. The files under `/static` are not behind a login and keep their own caching.

After an edit replaced a message's media, the `url` of its current media carries `?v={n}`, so a browser never shows cached bytes of the old media under it. The routes ignore the parameter; add `download=1` with `&`.

The gallery route takes `types` as a comma list, `limit` default 50, up to 200, and either `before_id` or `after_id`. Both take a media key; `before_id` pages to older items and `after_id` to newer ones. Sending both is a 400. It answers `{items, has_more}`, where each item has `id` set to the media key plus `thumb_url` and `media_url`, and the message's `text`, `is_deleted` and `deleted_at`.

The open routes answer `{"ok": true}` on success and 404 `Not configured` when their command is unset. `/media/open` accepts only types the viewer shows inline and answers 415 for others. `/media/open-path` accepts any type. Both answer 400 `File name cannot be passed to the command` when the file name cannot be passed to the command, and 500 when the command fails to start or exits non-zero within half a second.

## Export

`GET /api/chats/{chat_ref}/export` streams a chat as one JSON file. It needs a login that can see the chat and answers 403 for no-download logins. It is sent with `Cache-Control: private, no-store`, so neither the browser nor a proxy keeps a copy.

| Parameter | Meaning |
|-----------|---------|
| `from` | Start, inclusive, ISO 8601 |
| `to` | A bare date such as `2026-06-30` includes that whole day. A full timestamp is exclusive. |

`from` must be before `to`, or the route answers 400. Without either, the export holds the whole chat. Save the stream to a file:

```bash
curl -s -b jar.txt -o june.json \
  'http://localhost:8000/api/chats/<ref>/export?from=2026-06-01&to=2026-06-30'
```

The response is an `application/json` attachment named `<chat name>_export.json`:

```json
{
  "chat": {"id": -1001234567890, "ref": "<ref>", "type": "channel", "title": "Example", "username": null},
  "filters": {"from": "2026-06-01", "to": "2026-06-30"},
  "messages": []
}
```

`filters` appears only when `from` or `to` was given. The window picks messages by their send date.

Each message has `id`, `date`, `sender` (`name`, `username`), `text`, `is_outgoing` and `reply_to`, and these fields that say what the archive kept:

| Field | Content |
|-------|---------|
| `is_deleted` | `true` when the message was deleted in Telegram. The archive keeps it, so the export includes it. |
| `deleted_at` | When the archive noticed the deletion, ISO 8601 UTC, or `null`. |
| `edit_date` | The message's edit time in Telegram, ISO 8601 UTC, or `null`. Telegram also moves it when only the reactions change. |
| `edit_hide` | Telegram's flag for that `edit_date`: 1 when it says the edit is not to be shown, as when only the reactions changed, 0 when it shows, and `null` when the source did not report it (a message archived before the archive kept the flag, or one from a Telegram export import). A null flag counts as shown. With `edit_hide` 1 Telegram shows no edit mark on the message. |
| `media` | The message's current media, as a list of [export media](#export-media). Usually one entry. A message can hold more than one media row, and the first entry is the one the viewer shows. An empty list means the message has no media. |
| `versions` | Every earlier version the archive kept of the message, oldest first, whatever its date. An empty list means the archive kept none. |
| `reaction_history` | Every state of the message's reactions the archive kept, oldest first, whatever its date: `emoji`, `count` (0 when taken back), `previous_count`, `observed_at` (ISO 8601 UTC) and `source`, as in [the messages list](#messages). |
| `snapshots` | Every later state of the message's poll or link preview the archive kept, oldest first. Each has `kind` (`poll` or `preview`), `payload`, `observed_at` (ISO 8601 UTC) and `source`, as in [Poll and link preview snapshots](#poll-and-link-preview-snapshots). The first capture of a poll is under `media_payload.poll`; the export has no `raw_data`, so the first capture of a link preview is not in it. An empty list means no later state was kept. |
| `transcripts` | Present only when the message's media has transcripts: every transcript row, newest first. Each names its media by `media_id`. |

A location, a contact, a poll or another kind with no file has a `media_payload` object, keyed and shaped as in `raw_data` (see [Paging through messages](#paging-through-messages)).

Each entry of `versions` has these fields:

| Field | Content |
|-------|---------|
| `text` | The text of that version. Null on a `media_only` entry. |
| `date` | When that text was current in Telegram, ISO 8601 UTC. |
| `captured_at` | When the archive saw it replaced, ISO 8601 UTC. |
| `source`, `entities`, `rich_message` | As in [Message versions](#message-versions). |
| `media` | The earlier media this version was shown with, kept when an edit replaced the photo or file, as a list of [export media](#export-media). Empty when the edit kept the media. |
| `media_only` | Present and `true` only on an entry that holds earlier media and no text: the text version of that moment could not be written. |

The export pairs earlier media with versions the way the edit history does. It sits under the text version with the same `date`.

### Export media { #export-media }

Each media entry, current or earlier, has these fields. None is a file path, and the export holds no files.

| Field | Content |
|-------|---------|
| `media_id` | The id the media's transcripts name. An earlier media keeps the id it had before the edit replaced it. |
| `type` | `photo`, `video`, `voice`, `audio`, `document` and the other media types. |
| `file_name` | The stored file name, or `null`. |
| `file_size` | Bytes, or `null`. |
| `mime_type` | Or `null`. |
| `width`, `height` | Pixels, or `null`. |
| `duration` | Seconds, or `null`. |

So every transcript in the file names a media listed in the same file, on its message or under one of its versions. A voice note whose audio an edit replaced, with a transcript of each audio:

```json
{
  "id": 1270,
  "text": "",
  "edit_date": "2026-10-01T10:40:00",
  "media": [
    {"media_id": "-1001900000001_1270_voice_v1", "type": "voice", "file_name": "1270_second.ogg",
     "file_size": 9000, "mime_type": "audio/ogg", "width": null, "height": null, "duration": 9}
  ],
  "versions": [
    {
      "text": "", "date": "2026-10-01T10:38:00", "captured_at": "2026-10-01T10:40:00",
      "source": "listener", "entities": null, "rich_message": null,
      "media": [
        {"media_id": "-1001900000001_1270_voice", "type": "voice", "file_name": "1270_first.ogg",
         "file_size": 7000, "mime_type": "audio/ogg", "width": null, "height": null, "duration": 7}
      ]
    }
  ],
  "transcripts": [
    {"media_id": "-1001900000001_1270_voice_v1", "text": "Meet at seven thirty at the north lot.", "...": "..."},
    {"media_id": "-1001900000001_1270_voice", "text": "Meet at eight at the south lot.", "...": "..."}
  ]
}
```

Before 9.0 the file also ended with a flat `message_versions` list. It is gone: every version is under its message. See [Upgrading to 9.0](../operations/upgrading.md#upgrading-to-90).

Everything in the file is read from one snapshot of the archive, so a backup writing during the export cannot make a message disagree with its versions, its media, its reaction history, its snapshots or its transcripts. The export reads a message's versions, media, reaction states and snapshots as it writes that message, so a long chat is never held in memory at once. If they ever stop lining up with the messages, the export stops with an error instead of writing messages without them. The file then ends early and is not valid JSON.

## Transcripts

| Method and path | Login | Purpose |
|-----------------|-------|---------|
| `GET /api/transcription/status` | Any | `{enabled, configured, server_name, server_version}`. Never includes the server URL. |
| `GET /api/chats/{chat_ref}/media/{media_key}/transcripts` | Chat entitlement | Transcripts of one media item, newest first |
| `POST /api/chats/{chat_ref}/media/{media_key}/transcripts` | Chat entitlement | Ask for a transcript now |
| `GET /api/media/{media_id}/transcripts` | Chat entitlement | The same, addressed by the storage media id. Returns full rows. |
| `POST /api/media/{media_id}/transcripts` | Chat entitlement | The same, addressed by the storage media id |

Each transcript has `id`, `status`, `error`, `text`, `language`, `source`, `engine_name`, `engine_version`, `preset`, `models`, `confidence`, `duration_s`, `requested_at`, `completed_at` and `turns`.

The POST routes queue a request, or return the one already open, and make no outbound call themselves. They answer 409 when transcription is off, when the media has no sound, or when the file is not downloaded yet. For anyone but the master they answer 429 with a `Retry-After` header after `TRANSCRIPTION_ASK_RATE_LIMIT` presses in 10 minutes from the same client, or when `TRANSCRIPTION_ASK_MAX_OPEN` pressed files already wait for the backup. A press on a file whose request is already open returns that request and is never refused. In an open viewer (`ALLOW_ANONYMOUS_VIEWER=true`) the same holds for a file whose newest result is done or skipped: the press returns it and queues nothing. A login's press on such a file queues it again. For the rate limit, `Retry-After` and the `detail` say when the next press is allowed. For the cap, room only comes back when a backup run picks up waiting files, so `Retry-After` is a polling hint of one hour. All transcript routes except status answer 403 for no-download logins. Setup is in [Voice transcription](../configuration/transcription.md).

## Live updates over WebSocket

`/ws/updates` is a WebSocket and is not in the OpenAPI document. There is no server-sent events endpoint.

The viewer checks the upgrade request the same way it checks any HTTP request, using the `viewer_auth` cookie or the proxy header. A failed login closes the socket with code 4001. The `Origin` header must match `Host` or appear in `CORS_ORIGINS`, or the socket closes with 4003. The viewer holds at most `MAX_WS_CONNECTIONS` sockets, default 200, and refuses more with close code 1013. Each socket follows at most `MAX_WS_SUBSCRIPTIONS_PER_CONNECTION` chats, default 16.

The client sends:

```json
{"action": "subscribe", "chat_ref": "<ref>"}
{"action": "unsubscribe", "chat_ref": "<ref>"}
{"action": "ping"}
```

The server replies with `subscribed`, `subscribe_denied`, `unsubscribed` or `pong`. A denial past the subscription limit carries `reason: "subscription_limit"`.

Event frames all carry `type` and `chat_ref`:

| `type` | Fields |
|--------|--------|
| `new_message` | `message` |
| `edit` | `message_id`, `new_text`, `edit_date`, `edit_hide`, and `entities` when the frame carries the new formatting (left out when `new_text` was cut to fit). `media` when the edit replaced the photo or file (see below) |
| `delete` | `message_id`, `deletion_mode`, `deleted_at` |
| `pin` | `message_ids`, `pinned` |
| `reaction` | `message_id`, `reactions` (the live set; an emoji missing from it was taken back) |
| `transcript` | `message_id`, `transcript_id`, `status` |

The nested `media` of a `new_message` frame's `message`, and of an `edit` frame, has the shape `/api/chats/{ref}/messages` gives a message's `media`: `id` is the `{message_id}_{type}` key, and `url` is the ref-addressed `/media/` URL, or null when the file is not on disk. The URL of media an edit replaced ends in `?v={n}`, so a browser never shows the earlier file from its cache. A login whose downloads are off gets `url` and `file_path` null and `no_download: true`, as on the messages route. An `edit` frame without `media` says the media did not change. It is also left out when the frame would pass PostgreSQL's notification size limit, after the `entities`; the text is never left out.

The viewer closes a socket with 4001 `Session revoked` when its session ends: logout, expiry, eviction, an admin change to the viewer account or share token behind it, or the end-all action. How updates reach the viewer is in [Live updates and notifications](../viewer/live-updates.md).

## Push notifications

| Method and path | Login | Purpose |
|-----------------|-------|---------|
| `GET /api/push/config` | Public | `{mode, enabled, vapid_public_key}` |
| `GET /api/notifications/settings` | Public | `{enabled, mode, websocket_url: "/ws/updates"}` |
| `POST /api/push/subscribe` | Any | Register a Web Push subscription |
| `POST /api/push/unsubscribe` | Any | Remove a Web Push subscription |

`/api/notifications/settings` answers `{"enabled": false, "reason": "Not authenticated"}` when password login is on and the cookie is missing or expired.

`POST /api/push/subscribe` takes `{endpoint, keys: {p256dh, auth}, chat_ref}`, with `chat_ref` optional to follow one chat. It answers 400 unless `PUSH_NOTIFICATIONS=full`, 400 for an invalid endpoint, and 400 when the body sends `chat_id` instead of `chat_ref`. On success it answers `{"status": "subscribed", "chat_ref": ...}`. `POST /api/push/unsubscribe` takes `{endpoint}` and answers `{"status": "unsubscribed"}` or `{"status": "not_found"}`. It answers 400 `Push notifications not enabled` when push is off.

## Administration

Every route here is master only, and every write lands in the audit log. The concepts behind them are in [Logins, viewer accounts and share links](../viewer/access.md).

### Viewer accounts

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/admin/viewers` | List accounts: `{viewers: [{id, username, allowed_accounts, allowed_chat_refs, is_active, no_download, created_by, created_at, updated_at}]}` |
| `POST /api/admin/viewers` | Create an account |
| `PUT /api/admin/viewers/{id}` | Change an account and end its sessions |
| `DELETE /api/admin/viewers/{id}` | Delete an account and end its sessions |
| `GET /api/admin/chats` | Every chat, for picking grants: `{chats: [{id, ref, account_id, title, type, username, first_name, last_name}]}` |

The create body:

| Field | Rule |
|-------|------|
| `username` | Required, at least 3 characters. Must not match the master name. |
| `password` | Required, at least 8 characters |
| `allowed_accounts` | A list of account ids, or `null` for all |
| `allowed_chat_refs` | A list of chat refs, or `null` for all. An empty list grants nothing. |
| `is_active` | Default true |
| `no_download` | Default false |

A taken name answers 409. The old field `allowed_chat_ids` answers 400. Create a viewer who sees two chats:

```bash
curl -s -b jar.txt -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"a-long-password","allowed_chat_refs":["<ref1>","<ref2>"]}' \
  http://localhost:8000/api/admin/viewers
```

```json
{"id": 3, "username": "alice", "allowed_accounts": null, "allowed_chat_refs": ["<ref1>", "<ref2>"], "is_active": 1, "no_download": 0}
```

`PUT` takes any of the same fields. It answers 400 when the body changes nothing and 404 for an unknown id.

### Share tokens

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/admin/tokens` | List tokens: `{tokens: [{id, label, created_by, allowed_accounts, allowed_chat_refs, is_revoked, no_download, expires_at, last_used_at, use_count, created_at}]}` |
| `POST /api/admin/tokens` | Create a token |
| `PUT /api/admin/tokens/{id}` | Change `label`, `allowed_chat_refs`, `is_revoked` or `no_download` |
| `DELETE /api/admin/tokens/{id}` | Delete a token and end its sessions |

The create body takes `label`, `allowed_chat_refs`, `no_download` and `expires_at` as ISO 8601. `allowed_chat_refs` is required and must be a non-empty list: a share token always has a scope.

```bash
curl -s -b jar.txt -H 'Content-Type: application/json' \
  -d '{"label":"family","allowed_chat_refs":["<ref1>"],"no_download":true,"expires_at":"2026-12-31T23:59:59Z"}' \
  http://localhost:8000/api/admin/tokens
```

```json
{"id": 5, "label": "family", "token": "<64 hex characters>", "allowed_chat_refs": ["<ref1>"], "no_download": 1, "expires_at": "2026-12-31T23:59:59", "created_at": "2026-09-28T10:00:00"}
```

!!! warning "The token is shown once"
    Only the create response contains the plaintext `token`. The viewer stores a hash. Save it now, or revoke the token and create another.

Build a share link as `https://<viewer address>/#token=<token>`.

### End sessions

There is no route that lists or ends sessions by id. These calls end sessions:

| Call | Ends |
|------|------|
| `POST /api/logout` | The caller's own session |
| `PUT /api/admin/viewers/{id}` | Every session of that viewer, with its sockets and push subscriptions |
| `DELETE /api/admin/viewers/{id}` | The same, and removes the account |
| `PUT /api/admin/tokens/{id}` changing `is_revoked`, `allowed_chat_refs` or `no_download` | Every session opened with that token, with its sockets and push subscriptions |
| `DELETE /api/admin/tokens/{id}` | The same, and removes the token |
| `POST /api/admin/sessions/end-all` | Every session: the master login's, every viewer account's and every share token's, with their sockets and push subscriptions. `keep_current=true`, in the query string or as the JSON body `{"keep_current": true}`, keeps the caller's own session. Answers `{success, ended, current_session_ended}` and clears the cookie and sends `Clear-Site-Data: "cache"` when the caller's session ended. |

Lock a viewer out without deleting the account:

```bash
curl -s -b jar.txt -X PUT -H 'Content-Type: application/json' \
  -d '{"is_active":false}' http://localhost:8000/api/admin/viewers/3
```

Revoke a share token:

```bash
curl -s -b jar.txt -X PUT -H 'Content-Type: application/json' \
  -d '{"is_revoked":true}' http://localhost:8000/api/admin/tokens/5
```

End every session but your own, for example after changing `VIEWER_PASSWORD`:

```bash
curl -s -b jar.txt -X POST "http://localhost:8000/api/admin/sessions/end-all?keep_current=true"
```

Only `POST /api/admin/sessions/end-all` ends a session of the master login. Changing `VIEWER_PASSWORD` does not. A second viewer on the same database drops the ended sessions within 60 seconds. See [A second viewer](../viewer/access.md#a-second-viewer).

### Audit log and settings

| Method and path | Parameters | Response |
|-----------------|-----------|----------|
| `GET /api/admin/audit` | `limit` default 100, up to 500. `offset`. `username`. `action`. | `{logs, limit, offset}` |
| `GET /api/admin/settings` | None | `{settings}` |
| `PUT /api/admin/settings/{key}` | Body `{"value": ...}` | `{key, value}`. The value is stored as a string. |

## Internal endpoints

These routes serve other parts of the system. Clients should not call them.

| Route | Caller | Rules |
|-------|--------|-------|
| `POST /internal/push` | The backup container, in SQLite mode | Accepts only loopback and private source addresses. The viewer takes its secret from `INTERNAL_PUSH_SECRET`. In SQLite mode it falls back to the `.push-secret` file it creates next to the database. When a secret exists, every caller sends `Authorization: Bearer <secret>`. When none exists, only loopback callers get in; others get 403. PostgreSQL uses database notifications instead. |
| `POST /api/transcriptions/callback` | Your transcription server | Exists only when transcription is on and `TRANSCRIPTION_WEBHOOK_SECRET` holds a `whsec_` secret. Checks Standard Webhooks signatures, rejects bodies over 256 KiB and answers 204 to every genuine delivery. Not in the OpenAPI document. |

!!! warning "Keep internal routes off the internet"
    Block `/internal/` at your reverse proxy. Expose the transcription callback only if your transcription server reaches the viewer through the proxy. [Exposing the viewer safely](../viewer/exposing.md) has an nginx example.

## Errors and headers

Errors come back as `{"detail": "..."}`.

| Status | Meaning |
|--------|---------|
| 400 | Bad JSON, a missing field or an invalid parameter |
| 401 | Not logged in, or wrong credentials or token |
| 403 | Master only, `X-Viewer-Only` set, or downloads disabled for this login |
| 404 | Not found, including chats the caller may not see |
| 409 | A name clash, or a transcript that cannot be made |
| 413 | A transcription callback body over 256 KiB |
| 415 | `/media/open` on a type the viewer does not show inline |
| 429 | Login rate limit, or a transcript ask limit. Comes with `Retry-After` on transcript asks |
| 500 | Any other failure. The detail is always `Internal server error` |
| 503 | Database unreachable, or no login mode configured. `POST /auth/token` answers 500 with `Database not available` instead |

`CORS_ORIGINS` defaults to `*` with credentials off, so a browser page on another origin cannot send the cookie. List explicit origins to turn credentials on for them. Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin` and a same-origin `Content-Security-Policy`.
