# Live updates and notifications

New messages, edits and deletions can reach a viewer tab that is already open. This page also shows how to turn on browser notifications and Web Push, and what each one sends.

## Where updates come from

Live updates come from the real-time listener, which runs inside the backup container and is off by default. [Real-time listener](../configuration/listener.md) lists its switches. Voice transcript events are the exception. The backup's transcription run and the viewer's own transcription callback route also send them, so they arrive without the listener.

Without the listener, the viewer still shows new messages, but only after a scheduled backup has written them. The open chat polls its newest 50 messages every 3 seconds and picks them up that way.

Instant new messages, and the notifications for them, need both of these on the backup side:

```bash
ENABLE_LISTENER=true
LISTEN_NEW_MESSAGES=true   # the default
```

Edits, deletions, pins, reactions and transcripts still arrive live when `LISTEN_NEW_MESSAGES` is off. The table below shows which of them need a switch of their own.

The viewer forwards these event types to open tabs:

| Event | Sent when |
|-------|-----------|
| `new_message` | The listener saves a new message. |
| `edit` | The listener applies a text edit. Needs `LISTEN_EDITS=true`, the default. |
| `delete` | The listener applies a deletion. Needs `LISTEN_DELETIONS=true`, off by default. |
| `pin` | The listener sees a message pinned or unpinned. |
| `reaction` | Reaction counts on a message change. Needs `LISTEN_REACTIONS=true`, off by default. |
| `transcript` | A voice transcript changes status, from the listener, a backup run, or the viewer's own transcription callback route. |

The text of a new message or an edit is cut to 500 characters in the event. The full text is always in the database.

## How updates reach the viewer

The backup and the viewer are separate processes, and events travel between them differently on each database.

### PostgreSQL

The backup sends each event with PostgreSQL `LISTEN/NOTIFY` on the channel `telegram_updates`. The viewer listens on that channel through its own database connection. If the viewer cannot connect, it retries 5 seconds later. A connection that drops after it was established is not detected, so restart the viewer after a database restart.

There is nothing to configure. `VIEWER_HOST` and `VIEWER_PORT` are ignored on PostgreSQL.

### SQLite

The backup POSTs each event to `http://VIEWER_HOST:VIEWER_PORT/internal/push` with a 5-second timeout.

| Setting | Code default | Stock compose |
|---------|--------------|---------------|
| `VIEWER_HOST` | `localhost` | `telegram-viewer` |
| `VIEWER_PORT` | `8080` | `8000` |

The viewer listens on port 8000, so the code default of 8080 misses it. The stock compose file already sets both values. A native install must set the port on the backup side:

```bash
VIEWER_PORT=8000
```

The viewer accepts `/internal/push` from loopback addresses such as 127.0.0.1 and from private network addresses. Private addresses include Docker's internal networks and the 10.x, 172.16.x and 192.168.x ranges. A caller on a public address is refused.

A caller that is not on loopback must send a bearer secret. Once a secret exists, every caller must send it, loopback included. The secret comes from one of two places:

- `INTERNAL_PUSH_SECRET`, when it is set.
- Otherwise, a file named `.push-secret` with mode 0600, created next to the SQLite database. The stock compose file mounts the same directory into the backup and the viewer. Both read the same file, so nothing needs configuring.

When the two processes run on different hosts or do not share that directory, set the same value on both:

```bash
INTERNAL_PUSH_SECRET=<a long random string>
```

When the viewer refuses a push, its log shows a line that starts with `Rejected /internal/push`. The rest of the line says whether the address or the secret was wrong.

!!! warning "Block `/internal/push` at your reverse proxy"
    A reverse proxy on the same Docker network has a private address, so the viewer treats everything it forwards as internal. [Exposing the viewer safely](exposing.md) shows how to block `/internal/push` at the proxy.

## In the browser

Each viewer tab opens one WebSocket to `/ws/updates`. The page subscribes it only to the chat that is open. When you switch chats, the subscription switches too. If the socket closes, the page reconnects after 5 seconds.

The viewer sends an event only to sockets that are subscribed to that chat and whose login may see it. Events for chats outside `DISPLAY_CHAT_IDS`, or outside the user's grant, are dropped.

Two limits protect the viewer:

| Variable | Default | Effect |
|----------|---------|--------|
| `MAX_WS_CONNECTIONS` | `200` | Sockets across all clients. Extra sockets are closed with code 1013, which tells the browser to try again later. |
| `MAX_WS_SUBSCRIPTIONS_PER_CONNECTION` | `16` | Chats one socket may subscribe to. |

Both must be plain integers. A typo stops the viewer from starting. Under the stock compose file, the viewer receives only the variables listed in its `environment` block, so add these there if you change them:

```yaml
services:
  telegram-viewer:
    environment:
      MAX_WS_CONNECTIONS: "400"
      MAX_WS_SUBSCRIPTIONS_PER_CONNECTION: "16"
```

The viewer accepts a WebSocket from the same origin as the page. A socket from another origin is accepted only when that exact origin is listed in `CORS_ORIGINS`. The default value `*` does not allow WebSockets from any other origin. List each origin explicitly.

## Notification modes

`PUSH_NOTIFICATIONS` on the viewer picks the mode.

| Value | What you get |
|-------|--------------|
| `off` | The viewer does not ask for notification permission and does not register the service worker. |
| `basic` | The default. A browser notification for the open chat while its tab is hidden. |
| `full` | Web Push. Notifications arrive for every chat you may see, even with the browser closed. |

With `off`, a browser that already granted permission for this origin still shows a basic notification for the open chat while the tab is hidden. Revoke the permission in the browser to stop them.

The `PUSH_NOTIFICATIONS` value is lowercased but not trimmed. Any other value, including one with a stray space, silently becomes `basic`. `ENABLE_NOTIFICATIONS` is an older switch and is not needed. Setting it to `true` while `PUSH_NOTIFICATIONS=off` turns the notifications switch and basic notifications back on. Under the stock compose file the viewer does not receive it. Add it to the viewer's `environment` block if you want that.

When notifications are on and the browser has not decided yet, the top of the chat list shows a row, **Turn on notifications**. Click it and allow notifications when the browser asks, or dismiss it with its cross; the browser remembers either.

The main menu's **Notifications** row has a switch that turns them on and off at any time. On subscribes to Web Push when the viewer offers it, off unsubscribes. When the browser blocked notifications, the row says "Blocked in the browser settings" and the switch is disabled: allow them again in the browser's site settings. If this browser still holds a push subscription, the row reads "Blocked in the browser. Turn off to stop push." and the switch stays on, so you can turn it off.

Basic mode uses the page's WebSocket. It fires only when a new message arrives in the chat that is open and the tab is hidden. It shows the chat title and the first 100 characters of the text. A message without text shows `New message received`. When the browser has a Web Push subscription, the page skips basic notifications so you do not get two.

Full mode uses Web Push. The browser's push service delivers the notification, so the viewer tab does not need to be open.

## Set up Web Push

1. Set the mode in the viewer's environment:

    ```bash
    PUSH_NOTIFICATIONS=full
    ```

2. Serve the viewer over https. Browsers register service workers and push subscriptions only on https pages or on `localhost`. See [Exposing the viewer safely](exposing.md).

3. Keep the VAPID keys stable. These are the key pair the viewer uses to sign every Web Push message. On first start the viewer generates a pair and stores it in the database. A database reset loses the stored keys, and every existing subscription then stops working. To avoid that, generate a pair yourself and set both keys:

    ```bash
    npx web-push generate-vapid-keys
    ```

    ```bash
    VAPID_PUBLIC_KEY=<Public Key from the output>
    VAPID_PRIVATE_KEY=<Private Key from the output>
    VAPID_CONTACT=mailto:you@example.com
    ```

    The viewer uses the environment keys only when both are set. One key alone is ignored. `VAPID_CONTACT` defaults to `mailto:admin@example.com` and is sent with every push, so set a real address.

4. Restart the viewer, open it in the browser and click **Enable** in the sidebar banner.

A subscription made from the viewer covers every chat that user may see.

## What a push contains

Only new messages send a push. Each push has:

- a title, which is the chat title, or `Telegram` when the chat has none;
- a body of `sender: text`, or just the text when no sender name is known;
- `[Media]` as the text for a message without text.

The text is cut to 100 characters.

A newer push for the same chat replaces the older one. Clicking a notification opens the chat at that message, at `/?chat=<ref>&msg=<id>`.

!!! note "The push text leaves your server"
    Web Push is delivered through the push service of the browser vendor. The chat title, the sender name and the first 100 characters of each message pass through that service. The content is encrypted for the browser, but the vendor still carries it. Use `basic` or `off` if that is not acceptable.

## Subscription lifecycle

- A subscription endpoint must be a public https push service. The viewer refuses endpoints on private, loopback and reserved addresses.
- The viewer sends pushes 8 at a time, each with a 10-second timeout.
- When a push service answers 404, 410 or 403, the viewer deletes that subscription.
- Each subscription keeps a snapshot of its owner's account and chat grants, and gets pushes only for those chats. It is skipped when its owner is disabled, or when the share token behind it is revoked or expired.
- Logging out removes every subscription of that username. Other browsers of the same user subscribe again the next time they load the viewer.
- When a browser has blocked notifications, the main menu's **Notifications** row says so. Turning the switch off removes that browser's subscription.
- When the push service replaces a subscription, the service worker subscribes again and sends the new one to the viewer.
- The service worker at `/sw.js` is registered only when notifications are on. It handles pushes and notification clicks. It has no offline cache, so the viewer does not work without a connection.
