# Event webhook

The event webhook sends an HTTP request to another service each time the real-time listener applies an edit or a deletion to the archive.

## What fires

There are two events:

| Event | Fires when |
|-------|------------|
| `message_edited` | The listener applied an edit and the message text changed. |
| `message_deleted` | The listener processed a deletion for a message that is in the archive and not already marked deleted. Each archived message fires at most once. |

The webhook never fires for:

- new messages, reactions, pins or chat actions such as joins and title changes
- edits and deletions that the listener skipped, and deletions that its [mass-operation protection](listener.md#mass-operation-protection) blocked because too many arrived at once
- an edit that only changes formatting
- changes found by the scheduled `SYNC_DELETIONS_EDITS` sweep

## Prerequisites

The webhook runs inside the [real-time listener](listener.md), so it needs:

- `ENABLE_LISTENER=true`
- `LISTEN_EDITS=true` for `message_edited`. This is the default.
- `LISTEN_DELETIONS=true` for `message_deleted`. This is off by default.

When a prerequisite is missing, startup logs the matching warning or warnings and the backup keeps running. The `ENABLE_LISTENER=false` warning is logged on its own. Otherwise startup logs one line for each selected event whose `LISTEN_*` flag is off. These warnings appear only when the webhook settings themselves are valid.

```text
EVENT_WEBHOOK_ENABLED has no effect: ENABLE_LISTENER=false
message_deleted webhooks will never fire: LISTEN_DELETIONS=false
message_edited webhooks will never fire: LISTEN_EDITS=false
```

!!! warning "Message content leaves the archive"
    The request body can carry message text, chat titles and sender names to the target. Point the webhook only at a service you trust. The backup never logs the URL, the headers or the body, so a token in the URL or in a header stays out of the logs.

## Settings

Set these in the backup service's environment, for example in `.env`.

| Variable | Default | What it does |
|----------|---------|--------------|
| `EVENT_WEBHOOK_ENABLED` | `false` | Turns the webhook on. Accepts `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`. Any other value stops startup with an error. |
| `EVENT_WEBHOOK_URL` | empty | Target URL. Required. Must be `http://` or `https://` with a hostname. With `http://` the message text and your headers, tokens included, travel in cleartext. Use it only on a trusted, isolated network, and `https://` everywhere else. |
| `EVENT_WEBHOOK_METHOD` | `POST` | `POST` or `PUT`, in any case. |
| `EVENT_WEBHOOK_HEADERS` | empty | A JSON object whose values are all strings, sent unchanged on every request. When the object has no `Content-Type`, the backup adds `Content-Type: application/json; charset=utf-8`. |
| `EVENT_WEBHOOK_EVENTS` | `message_edited,message_deleted` | Which events fire, comma-separated, in any case. |
| `EVENT_WEBHOOK_CHAT_IDS` | empty | Comma-separated chat ids to fire for. Empty means every chat the listener processes. |
| `EVENT_WEBHOOK_BODY_TEMPLATE` | empty | Request body with placeholders. Empty means the [default body](#default-body). |

`EVENT_WEBHOOK_CHAT_IDS` does not add the `-100` prefix for you, unlike the backup's chat filters. Write each id the way the archive stores it. A channel or supergroup id starts with `-100`, for example `-1001234567890`.

The URL and the headers are static. Only the body is templated.

!!! warning "A bad setting turns the webhook off quietly"
    Only an invalid `EVENT_WEBHOOK_ENABLED` stops startup. A bad URL, method, headers, events or chat id list logs one warning that names the variable and ends with `event webhook disabled`. The backup keeps running without the webhook. Check the logs after every change to an `EVENT_WEBHOOK_*` value.

When the configuration is valid, startup logs the method and the selected events, for example:

```text
EVENT_WEBHOOK enabled - POST on message_deleted, message_edited
```

When `EVENT_WEBHOOK_CHAT_IDS` is set, the line ends with `, restricted to N chat(s)`, where N is the number of ids parsed.

## Placeholders

| Placeholder | `message_edited` | `message_deleted` |
|-------------|------------------|-------------------|
| `event` | `message_edited` | `message_deleted` |
| `account_id` | Archive account id | Archive account id |
| `chat_id` | Chat id as the archive stores it | Chat id as the archive stores it |
| `chat_title` | Chat title from the archive | Chat title from the archive |
| `message_id` | Telegram message id | Telegram message id |
| `sender_id` | Sender's Telegram id | Sender's Telegram id |
| `sender_name` | Sender name from the archive | Sender name from the archive |
| `date` | Telegram's edit time, with an offset, such as `2026-09-28T10:00:00+00:00`. Blank when the edit event carries no edit time, which the archive accepts only for a message that was never edited before. | Time the deletion was observed, in UTC with no offset and with microseconds, such as `2026-09-28T10:00:00.400369` |
| `text` | The new text | The archived text of the deleted message |
| `old_text` | The previous archived text | Blank |
| `new_text` | The new text | Blank |
| `media_type` | Media type of the edited message | Archived media type |

`chat_title` always comes from the archived chat. For a private chat it is `First Last (@username)` when a name is stored, the bare username when only the username is stored, and blank when the archive holds neither. A chat the archive does not hold also renders blank.

## Template syntax

- A placeholder is `{name}` or `{name|filter}`.
- Every other brace passes through unchanged, so a JSON template keeps its own braces.
- Substitution runs in one pass. A value that contains `{event}` stays as the literal text `{event}`.
- A missing value renders as an empty string. So does an unknown placeholder name.
- Dates render as ISO-8601. A deletion date carries microseconds and an edit date does not, so a receiver that parses `date` should accept both forms.

The filters are:

| Filter | Effect |
|--------|--------|
| `jsonescape` | Escapes the value for use inside a JSON string. Adds no quotes. UTF-8 and emoji stay as they are. |
| `urlencode` | Percent-encodes every character that is not a letter, a digit or one of `_.-~`. |
| `raw` | Inserts the value unchanged. |

A placeholder without a filter, or with an unknown filter, gets the automatic filter. It follows the `Content-Type` header:

| Content-Type | Automatic filter |
|--------------|------------------|
| `application/json` or any `+json` type | `jsonescape` |
| `application/x-www-form-urlencoded` | `urlencode` |
| anything else | `raw` |

`jsonescape` adds no quotes, so write them yourself around string values: `"message":"{text}"`, not `"message":{text}`. With `raw`, keeping the body valid is up to you.

## Default body

With `EVENT_WEBHOOK_BODY_TEMPLATE` empty, the body is this JSON:

```json
{"event":"{event}","account_id":{account_id},"chat_id":{chat_id},"chat_title":"{chat_title}","message_id":{message_id},"sender_id":"{sender_id}","sender_name":"{sender_name}","date":"{date}","text":"{text}","old_text":"{old_text}","new_text":"{new_text}","media_type":"{media_type}"}
```

`account_id`, `chat_id` and `message_id` are JSON numbers. `sender_id` is a string. A field with no value renders as `""`.

A deletion rendered with the default body looks like this. The values are invented:

```json
{"event":"message_deleted","account_id":1,"chat_id":-1001234567890,"chat_title":"Team \"chat\"","message_id":42,"sender_id":"777","sender_name":"Sender A","date":"2026-09-28T10:00:00.400369","text":"hello\nworld {event}","old_text":"","new_text":"","media_type":""}
```

The `jsonescape` filter escaped the quotes in the title and the line break in the text. The `{event}` inside the message text stayed as literal text.

## Recipes

Each recipe goes in `.env` next to the listener settings. Wrap any value that contains JSON in single quotes, so the `.env` parser keeps it exactly as written. Every recipe assumes the prerequisites:

```bash
ENABLE_LISTENER=true
LISTEN_DELETIONS=true
EVENT_WEBHOOK_ENABLED=true
```

### Generic JSON receiver

Leave the template empty to get the default body.

```bash
EVENT_WEBHOOK_URL=https://hooks.example.com/telegram
```

### ntfy, JSON body to the server root

The topic is part of an ntfy URL, and the URL cannot change per event. Publishing JSON to the server root lets the body set the topic and a title that names the event and the chat.

```bash
EVENT_WEBHOOK_URL=https://ntfy.example.com
EVENT_WEBHOOK_BODY_TEMPLATE='{"topic":"archive-events","title":"{event} in {chat_title}","message":"{text}"}'
```

### ntfy, plain text to a topic URL

The `text/plain` type selects the `raw` filter, so the text goes out as it is.

```bash
EVENT_WEBHOOK_URL=https://ntfy.example.com/archive-events
EVENT_WEBHOOK_HEADERS='{"Content-Type":"text/plain; charset=utf-8","Title":"Telegram archive"}'
EVENT_WEBHOOK_BODY_TEMPLATE={event} in {chat_title}: {text}
```

### ntfy with an access token

Add the token as a header to either ntfy recipe. `EVENT_WEBHOOK_HEADERS` holds every header in one JSON object. With the plain-text recipe, add `Authorization` to that recipe's object. On its own, the backup adds the JSON `Content-Type` for you.

```bash
EVENT_WEBHOOK_HEADERS='{"Authorization":"Bearer <token>"}'
```

### Form-encoded receiver

The form type selects the `urlencode` filter for every placeholder.

```bash
EVENT_WEBHOOK_URL=https://hooks.example.com/telegram
EVENT_WEBHOOK_HEADERS='{"Content-Type":"application/x-www-form-urlencoded"}'
EVENT_WEBHOOK_BODY_TEMPLATE=event={event}&chat={chat_title}&text={text}
```

## Delivery

The listener hands each request to a background task and never waits for it.

- Each attempt has a 5-second timeout.
- There are up to 3 attempts. The second waits 1 second and the third waits 4 seconds.
- The backup retries three kinds of failure: transport errors, such as a timeout or a refused connection, HTTP 429, and any 5xx response.
- Any status below 300 counts as success.
- A 3xx or any other 4xx response is a permanent failure. Redirects are never followed, so set `EVENT_WEBHOOK_URL` to the final URL.
- When 100 deliveries are already in flight, new events are dropped.
- Nothing is stored for redelivery. A failed or dropped event is gone, and the archive itself is unaffected.
- Shutdown cancels any delivery still in flight.

After the last attempt fails, the log shows one line with the event and the HTTP status or the error type:

```text
Event webhook delivery failed (<event>: <reason>)
```

An unexpected error inside the delivery task, one that is not an HTTP failure or a transport error, logs this line instead and also counts as a failure:

```text
Event webhook delivery task failed: <error type>
```

The listener keeps sent, failed and dropped counters in memory only. They are never logged or exposed. At the default log level the failure line is the only record. With `LOG_LEVEL=DEBUG`, each delivery also logs `Event webhook delivered (<event>)` and each drop logs `Event webhook dropped: <n> deliveries already in flight`. See [Monitoring and troubleshooting](../operations/troubleshooting.md) for reading the logs.
