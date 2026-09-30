# Exposing the viewer safely

Put the viewer on a network or behind a reverse proxy without exposing data or logins. It covers cookies, client IPs, WebSockets, the routes that answer without a login and the paths to block at the proxy.

Viewer accounts, share tokens and proxy logins are covered in [Logins, viewer accounts and share links](access.md).

## Where you start

The stock `docker-compose.yml` publishes the viewer on `127.0.0.1:8000` only. Nothing outside the host can reach it until you add a proxy or change that line.

The viewer container has few privileges and no Telegram credentials. The only secrets it may hold are the viewer passwords and the internal push secret:

- It holds no Telegram credentials. The compose service has no `env_file`, so it receives only the variables listed in its `environment:` block. The image does not contain the Telegram client library.
- It runs as the non-root user `telegram`, uid 1000.
- Its root filesystem is read-only, with a tmpfs at `/tmp`.
- All Linux capabilities are dropped and `no-new-privileges` is set.

The viewer refuses to serve archive data until a login method is configured or `ALLOW_ANONYMOUS_VIEWER=true` is set. Until then, every data route answers HTTP 503.

## Checklist

Work through each item before the viewer leaves `127.0.0.1`.

| Item | Why | Setting |
|------|-----|---------|
| Terminate TLS at a reverse proxy | The viewer speaks plain HTTP on port 8000. Logins, cookies and share tokens must not cross a network in clear text. | Your proxy's TLS configuration. |
| Mark the session cookie Secure | The `viewer_auth` cookie is HttpOnly and SameSite=Lax. The Secure flag keeps browsers from sending it over plain HTTP. | `SECURE_COOKIES=true`. See [Cookies and client IPs](#cookies-and-client-ips). |
| Pass the real client IP | Login and share-token attempts share one rate limit per client IP. See [Sessions](access.md#sessions). The audit log records the same IP. Without this setting, every user shares the proxy's IP. One person's failed logins then lock everyone out, and the audit log shows only the proxy. | `TRUST_PROXY_HEADERS=true`, and only when the proxy overwrites `X-Forwarded-For`. See [Cookies and client IPs](#cookies-and-client-ips). |
| Guard the proxy login header | With `AUTH_PROXY_HEADER` set, the viewer logs in whoever sends that header as the name it carries. Anyone who can reach the proxy could claim any user, the master included. | Have the proxy strip or overwrite `AUTH_PROXY_HEADER` on every request. |
| Keep the master out of the public hostname | A request that carries the header `X-Viewer-Only: true` cannot log in as master and cannot reach master routes. Viewer accounts and share tokens still work. | Have the proxy add `X-Viewer-Only: true` on the public server block. Leave it off the one you administer from. |
| Forward the WebSocket upgrade | Live updates arrive over the WebSocket at `/ws/updates`. | Forward the `Upgrade` and `Connection` headers for that path. The socket's `Origin` must match the `Host` header the viewer receives, so pass the browser's `Host` through. |
| Set CORS only for split origins | The default `CORS_ORIGINS=*` disables credentials for cross-origin requests and admits no cross-origin WebSocket. A same-origin setup needs nothing. | When the page and the API live on different origins, list the exact origin, for example `CORS_ORIGINS=https://archive.example.com`. |
| Block `/internal/push` | This is the backup's route for pushing live events to the viewer. The viewer accepts it from any loopback or private address. A proxy on the same Docker network has a private address, so the requests it forwards pass that check. | Block it at the proxy. Also make sure a secret exists: set `INTERNAL_PUSH_SECRET` on both containers. On SQLite both containers share an automatic secret file named `.push-secret` next to the database, so the stock stack already has one. |
| Decide on the API schema pages | `/openapi.json`, `/docs` and `/redoc` need no login. The schema lists every API route except the transcription callback. | Block all three at the proxy if you do not want that list public. The `/docs` and `/redoc` pages load their scripts from a CDN, and the viewer's Content-Security-Policy blocks that, so the pages render blank. |
| Expose the transcription callback only if you use it | `/api/transcriptions/callback` receives finished transcripts from your akou transcription server. It checks the signature of every request. | The route exists only when `TRANSCRIPTION_ENABLED` is true and `TRANSCRIPTION_WEBHOOK_SECRET` holds a `whsec_` secret. Leave it reachable if your transcription server calls back through the proxy. Otherwise block it. See [Voice transcription](../configuration/transcription.md). |

!!! warning "The viewer container only sees listed variables"
    The stock compose file passes an explicit list of variables to the viewer. `SECURE_COOKIES`, `TRUST_PROXY_HEADERS`, `CORS_ORIGINS`, `INTERNAL_PUSH_SECRET` and the `AUTH_PROXY_*` variables are on that list. `TRANSCRIPTION_WEBHOOK_SECRET` is on the list but commented out, so uncomment it to install the callback route. A viewer variable you add yourself must also go in the viewer's `environment:` block, or setting it in `.env` has no effect.

## Cookies and client IPs

`SECURE_COOKIES=true` forces the Secure flag on the session cookie, and `false` turns it off. With any other value, empty included, the viewer sets the flag when `X-Forwarded-Proto` or the request scheme is `https`. The viewer reads `X-Forwarded-Proto` whatever `TRUST_PROXY_HEADERS` says, so have the proxy overwrite it.

With `TRUST_PROXY_HEADERS=true`, the viewer takes the client IP from the first `X-Forwarded-For` entry, then from `X-Real-IP`. A proxy that appends to `X-Forwarded-For` lets clients pick their own IP. Only `true`, in any letter case, turns the setting on. `1` and `yes` count as false.

## Routes that answer without a login

Every route not listed here needs a session, a share-token session or a proxy login.

| Route | What it returns |
|-------|-----------------|
| `/` | The single-page app. It shows the login form when no session exists. |
| `/sw.js` | The service worker used for notifications. |
| `/static/*` | Scripts, styles, fonts, icons and any wallpaper named in `VIEWER_CHAT_BACKGROUND`. Do not use a private photo as wallpaper. |
| `/api/health` | Whether the database answers. |
| `/api/auth/check` | Whether the caller is logged in, and whether login is configured. |
| `/api/login` | Password login. |
| `/api/logout` | Ends the current session. |
| `/auth/token` | Share token login. |
| `/api/push/config` | The notification mode and the public Web Push key. |
| `/api/notifications/settings` | Whether notifications are on. |
| `/internal/push` | Live events from the backup. Loopback and private addresses only. |
| `/api/transcriptions/callback` | Signed transcription results, when the route is installed. |
| `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` | The API schema and its viewer pages. |

## Built-in protections

Every response carries these headers:

```text
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'
```

The rest of the protection is in how the viewer serves files and errors:

- Every front-end asset is vendored into the image. The page loads nothing from a CDN.
- The viewer serves media only from inside the media root. It refuses paths with `..`, absolute paths and symlinks that resolve outside the root.
- Only image, video, audio and PDF files are served inline. Every other type, SVG included, is served as an attachment.
- Media, thumbnails and avatars are sent with `Cache-Control: private, no-cache`. Shared caches do not keep them, and the browser asks the viewer before each reuse of its copy, so a logged-out browser or a login that lost the chat cannot show them again. Logout also sends `Clear-Site-Data: "cache"`, and the page reloads when its session ends, so the next login on the same tab starts clean. A chat export is sent with `private, no-store`. See [Media](../reference/api.md#media).
- The viewer addresses every chat by its chat ref. See [Links to a message](using-the-viewer.md#links-to-a-message). An unknown ref and a forbidden ref both return the same 404.
- Unhandled errors return a generic 500 or 503. The log line shows the route pattern, such as `/api/chats/{chat_ref}/messages`, not the actual URL, so chat ids and file names stay out of the logs.

## What logged-in users can see

Chat refs keep chat ids out of URLs, but ids are not hidden from users:

- The info panel and the sender dialog show Telegram chat and user ids. API payloads include chat and sender ids.
- Message payloads include each file's path in the archive for every login except no-download logins. Only the master login displays the path, but any logged-in user can read it from the API response.

Scope viewer accounts and share tokens to the chats you mean to share. See [Logins, viewer accounts and share links](access.md).

## Web Push needs HTTPS

Web Push works only on HTTPS pages or on `localhost`. Serve the viewer over HTTPS before you turn on `PUSH_NOTIFICATIONS=full` for anyone else. See [Live updates and notifications](live-updates.md).

## Commands that run on the viewer host

`MEDIA_OPEN_CMD` and `MEDIA_OPEN_PATH_CMD` add the **Open** and **Show in folder** buttons to the info panel. They are shown only to the master login. Each click runs a shell command on the machine that serves the viewer. Before running it, the viewer removes environment variables whose names look like secrets.

They are meant for a viewer you run natively on your own computer. The stock compose file deliberately does not pass them to the container. Do not set them on a viewer that other people can reach.

## Example nginx server block

This example covers only the settings the viewer needs. It assumes nginx runs on the same host as the stock compose stack and that the certificate paths exist.

```nginx
server {
    listen 443 ssl;
    server_name archive.example.com;

    ssl_certificate     /etc/ssl/archive.example.com/fullchain.pem;
    ssl_certificate_key /etc/ssl/archive.example.com/privkey.pem;

    # Never reachable from outside
    location ~ ^/(internal/push|docs|redoc|openapi\.json) {
        deny all;
    }

    location /ws/updates {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

The block sets `X-Forwarded-For` to `$remote_addr`, which replaces whatever the client sent, so `TRUST_PROXY_HEADERS=true` is safe. Pair the block with these lines in `.env`:

```bash
SECURE_COOKIES=true
TRUST_PROXY_HEADERS=true
```

Then recreate the viewer:

```bash
docker compose up -d telegram-viewer
```

To keep the master off a public hostname, add this line to the `/ws/updates` and `/` location blocks of that hostname's server block:

```nginx
proxy_set_header X-Viewer-Only "true";
```

Administer the viewer through a second server block that does not set it, for example one reachable only from your own network.

## A native viewer behind a local proxy

When the viewer runs natively with `uvicorn` and the proxy runs on the same host, every request from the proxy comes from loopback. On PostgreSQL without `INTERNAL_PUSH_SECRET`, no secret exists. The viewer then accepts `/internal/push` from loopback with no further check. Anyone who can reach the proxy could push fake live events to open browsers. Block the route at the proxy, as in the [example](#example-nginx-server-block) and the [checklist](#checklist).

## Reporting a vulnerability

Do not open a public issue. Follow the steps in [SECURITY.md](https://github.com/GeiserX/Telegram-Archive/blob/main/SECURITY.md).
