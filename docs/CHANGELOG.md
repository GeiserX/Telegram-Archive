# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [7.7.0] - 2026-04-29

### Security

- **Viewer now fails closed when credentials are missing** — If `VIEWER_USERNAME`/`VIEWER_PASSWORD` are not configured, the HTTP API and WebSocket endpoint reject access unless `ALLOW_ANONYMOUS_VIEWER=true` is explicitly set.
- **Restricted media access is enforced consistently** — Media, thumbnails, avatars, and non-chat folders now share centralized chat ACL checks, preventing restricted users from reading `_shared` files or unrelated chat media.
- **No-download users can no longer fetch original or thumbnail bytes** — Accounts and share tokens with `no_download=true` receive metadata only; direct original media and generated thumbnail URLs return 403, while UI avatars remain available.
- **Internal push events require a secret off-loopback** — `/internal/push` requires `INTERNAL_PUSH_SECRET` for non-loopback/private-network callers, reducing spoofing risk between co-located containers.
- **WebSocket upgrades validate origin** — Cross-origin WebSocket connections must be same-origin or explicitly allowed by `CORS_ORIGINS`.
- **Non-interactive auth hash files are owner-only** — Persisted `phone_code_hash` sidecar files are now created with `0600` permissions.

### Fixed

- **Scheduled backups no longer overlap** — The scheduler uses a backup lock so initial and cron-triggered jobs cannot run concurrently.
- **FloodWait handling is explicit and bounded** — One-shot Telegram API calls now retry through shared helpers and abort instead of sleeping when Telegram asks for waits above `MAX_FLOOD_WAIT_SECONDS`.
- **FloodWait env parsing is resilient** — Invalid `MAX_FLOOD_RETRIES` and `MAX_FLOOD_WAIT_SECONDS` values fall back to safe defaults instead of crashing imports.
- **Media downloads finalize atomically** — Temporary `.part` files are moved into place only when an actual file exists, preserving Telethon-selected extensions and avoiding bogus stored paths.
- **Telegram contact, geo, and poll media are metadata-only** — These message types no longer trigger file download attempts.
- **Database URL precedence is consistent** — Entrypoint migrations and realtime notifier/listener mode detection now honor `DATABASE_URL` before `DB_TYPE`, including `postgres://`, `postgresql://`, `postgresql+asyncpg://`, and SQLite URLs.
- **Database migration coverage includes app-state tables** — SQLite-to-PostgreSQL migration now includes viewer accounts, sessions, tokens, folders, forum topics, push subscriptions, and settings.
- **Share token URLs avoid query-string leakage** — Generated links use `#token=` fragments and preserve subpath deployments.

### Changed

- **Deletion listening is safer by default** — `LISTEN_DELETIONS` now defaults to `false` so archives do not mirror Telegram deletions unless explicitly configured.
- **Docker examples pin the 7.7.0 release** — Compose and README snippets now reference `drumsergio/telegram-archive:7.7.0` and `drumsergio/telegram-archive-viewer:7.7.0`.
- **Viewer compose binds to localhost by default** — The example viewer service binds `127.0.0.1:8000:8000` and documents reverse-proxy/auth requirements before public exposure.
- **CI and release checks are stricter** — Docker publish workflows run ruff and pytest before publishing, shellcheck tracks `main`, Docker Hub description sync covers both images, and release checks match the documented local test command.

### Documentation

- **Viewer authentication setup is documented** — README and `.env.example` now show required viewer credentials and the explicit anonymous opt-in.
- **Chat include filters are documented as allow-lists** — Examples now correctly show `CHAT_TYPES=groups,channels` when including one specific channel alongside groups.
- **Operational safety docs were refreshed** — README and `.env.example` now describe deletion mirroring, flood-wait controls, proxy header trust, and internal push secrets.

### Tests

- Added regression coverage for fail-closed viewer auth, no-download media restrictions, thumbnail ACLs, WebSocket subscription filtering, internal push auth, scheduler locking, flood-wait aborts, atomic downloads, `DATABASE_URL` behavior, non-interactive auth hash reuse, and migration model enumeration.

## [7.6.4] - 2026-04-25

### Fixed

- **Improved General topic test suite** — Renamed unprofessional test data, removed redundant `@pytest.mark.asyncio` decorators (project uses `asyncio_mode = "auto"`), converted setup to a proper pytest fixture, and added edge case tests for nonexistent topics, `topic_id=0`, and topic+search filter interaction. Contributed by @tondeaf in #122 (follow-up).

## [7.6.3] - 2026-04-25

### Fixed

- **Edit notifications no longer silently dropped on long messages** — The 500-char truncation guard only protected `data["message"]["text"]` (new_message path), leaving `data["new_text"]` (edit path) unprotected. A 4096-char emoji edit could produce a 16KB payload exceeding PostgreSQL's 8KB NOTIFY limit, causing a silent `pg_notify` error. Both paths are now truncated via a shared `_truncate_notify_data()` helper. (#123 follow-up)
- **Use `pg_notify()` with bound parameters for PostgreSQL NOTIFY** — Replaces f-string SQL interpolation that was vulnerable to asyncpg `$N` placeholder parsing and fragile manual single-quote escaping. Contributed by @tondeaf in #123.
- **Push secret comparison is now timing-safe** — `/internal/push` endpoint used `!=` for bearer token comparison; switched to `secrets.compare_digest()` consistent with the rest of the auth layer.
- **Test assertions use stable `TextClause.text` attribute** — Replaced `str(stmt)` with `stmt.text` for SQLAlchemy SQL assertions, avoiding reliance on undocumented `__str__` behavior.

## [7.6.2] - 2026-04-25

### Fixed

- **FloodWaitError no longer crashes `get_dialogs()` or `get_me()`** — PR #124 set `flood_sleep_threshold=0` globally but only wrapped 2 of ~20 API call sites. The unwrapped `get_dialogs()` and `get_me()` calls could crash the entire backup or prevent startup. Both are now wrapped with bounded flood-wait retry logic.
- **Negative `e.seconds` from Telegram no longer causes zero-delay retry storms** — Sleep duration is now clamped to `max(0, ...)` on both the iterator wrapper and the new one-shot retry helper.
- **Invalid `FLOOD_WAIT_LOG_THRESHOLD` env var no longer crashes mid-backup** — Bare `int()` parsing replaced with defensive `try/except` that falls back to the default of 10 seconds.
- **`iter_messages_with_flood_retry` now rejects `reverse=False`** — The resume tracking (`max(resume_from, msg.id)`) is only correct for ascending iteration. A `ValueError` is now raised if `reverse=True` is not passed, preventing silent data corruption from future misuse.
- **Documented `FLOOD_WAIT_LOG_THRESHOLD`** — Added to `.env.example` alongside the other logging variables.

## [7.6.1] - 2026-04-19

### Fixed

- **Forwarded media from private channels no longer creates broken placeholders** — When a message forwarded from a private channel contains a document with an inaccessible file reference (`media.document=None`), `_get_media_type()` now correctly returns `None` instead of `"document"`. Previously this caused a broken `telegram_file_id` of `"None"`, a failed download attempt, and a misleading "Will download on next backup" placeholder that would never resolve. Applies to both scheduled backup and real-time listener (#125)

## [7.6.0] - 2026-04-18

### Added

- **Topic filtering for forum supergroups** — New `SKIP_TOPIC_IDS` environment variable to exclude specific topics from backup while keeping the rest of the chat. Format: `chat_id:topic_id,...`. Works in both scheduled backup and real-time listener flows (#117)

### Fixed

- **Dangling dedup symlinks no longer cause infinite redownload loops** — When `DEDUPLICATE_MEDIA` is enabled and `VERIFY_MEDIA` runs, dangling symlinks (where the target was renamed by Telethon) are now detected via `os.path.lexists()` instead of `os.path.exists()`, which follows symlinks. The download return value is now captured to use the actual on-disk filename for symlink targets. Stale symlinks are removed before recreation to prevent `Errno 17` (file exists) errors. Applies to both scheduled backup and real-time listener (#115)

## [7.5.0] - 2026-04-13

### Added

- **SOCKS5 proxy support** — Route all Telegram connections through a SOCKS5 proxy, useful in regions where Telegram is blocked or behind corporate firewalls. New env vars: `TELEGRAM_PROXY_TYPE`, `TELEGRAM_PROXY_ADDR`, `TELEGRAM_PROXY_PORT`, `TELEGRAM_PROXY_USERNAME`, `TELEGRAM_PROXY_PASSWORD`, `TELEGRAM_PROXY_RDNS` (#104)
- **Validation hardening** — Port range (1-65535), username/password pairing, boolean RDNS parsing, and case-insensitive proxy type
- **Dependency** — Added `python-socks[asyncio]>=2.7.1` (required by Telethon for SOCKS5 transport)

### Security

- **Proxy endpoint details** — Proxy configuration logged at DEBUG (not INFO) to avoid exposing infrastructure topology

### Contributors

- Thanks to [@samnyan](https://github.com/samnyan) for the proxy feature contribution!

## [7.4.2] - 2026-03-31

### Fixed

- **Listener shutdown KeyError** — `_log_stats()` referenced non-existent keys from `MassOperationProtector.get_stats()`. A clean shutdown would raise `KeyError`. Fixed to use actual keys (`rate_limits_triggered`, `operations_blocked`, `chats_rate_limited`)
- **Pin/unpin realtime** — Full pipeline now works end-to-end: listener emits `PIN` -> notifier delivers -> `handle_realtime_notification()` forwards to WebSocket -> browser reloads pinned messages. Previously the relay in `main.py` was missing
- **pyproject.toml version sync** — Was stuck at `7.2.0` since v7.2.0. Now synced with `__init__.py` at `7.4.2`
- **WebSocket subscribe ACL** — Server now sends `subscribe_denied` (instead of `subscribed`) when a restricted user attempts to subscribe to a chat outside their allowed list

## [7.4.1] - 2026-03-31

### Security

- **Avatar ACL bypass** — Restricted users can no longer access avatars outside their allowed chats. `serve_media()` and `serve_thumbnail()` now extract `chat_id` from avatar filenames and enforce per-chat scoping
- **Push endpoint spoofing** — `/internal/push` now supports an optional `INTERNAL_PUSH_SECRET` env var as a bearer token. Prevents co-tenant containers from spoofing live events
- **Reaction recovery data loss** — `insert_reactions()` now retries ALL reactions after a sequence reset, not just the row that triggered the duplicate-key error
- **Push unsubscribe ownership** — `POST /api/push/unsubscribe` is now scoped to the requesting user's `username`, preventing cross-user endpoint removal

### Added

- **`INTERNAL_PUSH_SECRET` env var** — Optional shared secret for `/internal/push` endpoint in multi-tenant Docker environments

## [7.4.0] - 2026-03-31

### Security

- **XSS fix** — `linkifyText()` now percent-encodes raw `"` and `'` in URLs before inserting into `href` attributes

### Fixed

- **Stats filter** — Fixed JSON string-key vs `int` type mismatch that caused per-chat filtering to silently fail. Also removes `media_files`/`total_size_mb` for restricted users
- **Deletion path** — Unknown-chat deletions now resolve the chat ID from DB first, apply rate limiting, skip ambiguous message IDs, and send viewer notifications
- **Folders** — Restricted users no longer see empty folder names/emoticons for folders with 0 accessible chats
- **Push endpoint** — `/internal/push` accepts loopback + RFC1918/Docker private IPs to support split-container SQLite mode

### Changed

- **`delete_message_by_id_any_chat()` replaced** — Replaced by `resolve_message_chat_id()` in the database adapter. The old method deleted from ALL chats with a matching message ID; the new approach resolves to a single chat first and skips ambiguous cases

## [7.3.2] - 2026-03-26

### Fixed

- **Album caption display** — Captions now display correctly for album posts with grouped messages in the viewer

### Contributors

- Thanks to [@vadimvolk](https://github.com/vadimvolk) for the contribution!

## [7.3.1] - 2026-03-25

### Fixed

- **Skip `get_dialogs()` in whitelist mode** — Prevents backup from hanging when `CHAT_IDS` whitelist is configured, by skipping the full dialog enumeration that is unnecessary in whitelist mode (#96)

## [7.3.0] - 2026-03-15

### Added

- **Gap-fill recovery** — Detects gaps in message ID sequences using SQL `LAG()` window function and recovers skipped messages from Telegram API automatically. Available as CLI subcommand (`fill-gaps --chat-id --threshold`) and scheduler option (`FILL_GAPS=true`). Respects all backup config rules
- **Token URL auto-login** — Shareable links with `?token=XXX` parameter for direct viewer access. Token is stripped from URL after login via `history.replaceState`
- **@username display** — Usernames now shown in chat list and message headers
- **Shareable link generation UI** — New controls in admin panel for generating share links

## [7.2.1] - 2026-03-13

### Fixed

- **Login with unreachable database** — Login endpoint now falls through to master env var credentials instead of returning a generic "Unexpected error". Viewer-only users see a clear "Database temporarily unavailable" message (HTTP 503)
- **All data endpoints** — Connection errors now return HTTP 503 "Database temporarily unavailable" instead of generic HTTP 500
- **Audit log resilience** — Audit log writes in the login flow are wrapped in try/except so they never crash the response

### Added

- **Health endpoint** — `GET /api/health` returns `{"status": "ok", "database": "connected"}` (200) or `{"status": "degraded", "database": "unreachable"}` (503). Useful for Docker healthchecks and monitoring
- **Global exception handler** — Catches unhandled DB connection errors across all endpoints and returns 503

## [7.2.0] - 2026-03-10

### Added

- **Share tokens** — Admins can create link-shareable tokens scoped to specific chats. Recipients authenticate via token without needing an account. Tokens support expiry dates, revocation, and use tracking
- **Download restrictions** — `no_download` flag on both viewer accounts and share tokens. Restricted users can still view media inline but cannot explicitly download files or export chat history. Download buttons hidden in the UI for restricted users
- **On-demand thumbnails** — WebP thumbnail generation at whitelisted sizes (200px, 400px) with disk caching under `{media_root}/.thumbs/`. Includes Pillow decompression bomb protection and path traversal guards
- **App settings** — Key-value `app_settings` table for cross-container configuration, with admin CRUD endpoints
- **Audit log improvements** — Action-based filtering in admin panel (prefix match for suffixed events like `viewer_updated:username`), token auth events tracked (`token_auth_success`, `token_auth_failed`, `token_created`, etc.)
- **Admin chat picker metadata** — Chat picker now returns `username`, `first_name`, `last_name` for better display
- **Token management UI** — New "Share Tokens" tab in admin panel with create, revoke, and delete controls. Plaintext token shown once at creation with copy button
- **Token login UI** — Login page has a "Share Token" tab for token-based authentication

### Security

- **Token revocation enforced on active sessions** — Revoking, deleting, or changing scope/permissions of a share token immediately invalidates all sessions created from that token. Sessions track `source_token_id` for precise invalidation
- **Session persistence includes restrictions** — `no_download` and `source_token_id` are now persisted in `viewer_sessions` table, surviving container restarts. Previously `no_download` was lost after restart, silently granting download access
- **Export endpoint respects no_download** — The `GET /api/chats/{chat_id}/export` endpoint now returns 403 for restricted users

### Fixed

- **Create viewer passes all flags** — `is_active` and `no_download` from the admin form are now correctly passed through to `create_viewer_account()`. Previously both flags were silently ignored on creation
- **Token expiry timezone handling** — Frontend now converts local datetime to UTC ISO before sending to the backend, fixing early/late expiry for non-UTC admins
- **Audit filter matches suffixed actions** — Filter now uses prefix matching so "viewer_updated" catches "viewer_updated:username"
- **Migration stamping checks all artifacts** — Entrypoint now checks `viewer_tokens`, `app_settings`, AND `viewer_accounts.no_download` before stamping migration 010 as complete

### Changed

- **Migration 010** — Consolidated idempotent migration creates `viewer_tokens`, `app_settings` tables and adds `no_download` column to `viewer_accounts`. Also adds `no_download` and `source_token_id` columns to `viewer_sessions`
- **Entrypoint stamping** — Updated both PostgreSQL and SQLite stamping blocks to detect all migration 010 artifacts
- **Dockerfile.viewer** — Added Pillow system dependencies (libjpeg, libwebp) for thumbnail generation
- **Version declarations** — `pyproject.toml` and `src/__init__.py` both set to 7.2.0
- **SECURITY.md** — Added 7.x.x as a supported version
- **pyproject.toml** — Added `viewer` optional dependency group for Pillow

## [7.1.7] - 2026-03-08

### Fixed

- **Missing `beautifulsoup4` in Docker image** — `beautifulsoup4` was declared in `pyproject.toml` but missing from `requirements.txt` (used by Docker builds), causing `No module named 'bs4'` when running HTML imports

## [7.1.6] - 2026-03-08

### Fixed

- **Idempotent migrations 007-009** — When `create_all()` runs before Alembic (fresh SQLite databases), tables and columns may already exist. Migrations now inspect the schema before altering, preventing "duplicate column name: username" crashes on upgrade. Fixes #81

## [7.1.5] - 2026-03-08

### Fixed

- **Duplicate messages in real-time viewer** — Race condition in 3-second polling (`checkForNewMessages`) allowed concurrent async calls to both add the same message. Added concurrency guard and deduplication
- **Missing `chat_id` in WebSocket broadcast** — The `new_message` payload was missing `chat_id`, making client-side real-time message insertion a silent no-op. Messages only appeared via polling
- **WebSocket new message handler deduplication** — Added `messages.some()` check to prevent duplicates when both WebSocket and polling deliver the same message

## [7.1.4] - 2026-03-05

### Security

- **Media path injection hardening** — Early rejection of `..` traversal and absolute paths before filesystem operations. Uses `resolve(strict=True)` to prevent TOCTOU race conditions with symlinks. Existing `is_relative_to` check retained as defense-in-depth (CodeQL alerts #12, #13, #14)

## [7.1.3] - 2026-03-05

### Fixed

- **Alembic stamping detects all migrations** — The entrypoint's pre-Alembic database stamping logic now detects migrations 008 (`push_subscriptions.username` column) and 009 (`viewer_sessions` table). Previously it only checked up to 007, causing `CREATE TABLE` failures when `Base.metadata.create_all()` had already created newer tables (e.g. SQLite containers crash-looping on `viewer_accounts already exists`)

## [7.1.2] - 2026-03-05

### Fixed

- **Two-tier session protection** — Replaces the single-backup approach from v7.1.1 with a robust two-tier system:
  - **Golden backup** (`.session.authenticated`) — only written after a successful login, guarantees a known-good recovery point that crash-loops can never corrupt
  - **Pre-connect snapshot** (`.session.bak`) — taken before every connect attempt as a secondary fallback
  - On auth failure, restores from golden backup first, then snapshot. Prevents Telethon's silent DH key renegotiation from permanently destroying authenticated sessions during crash-loops.
  - Uses raw `sqlite3` to verify `auth_key` presence before deciding whether to back up or restore, avoiding false positives from empty/corrupted session files
  - Flushes WAL checkpoint before creating golden backup to ensure file completeness

## [7.1.1] - 2026-03-05

### Added

- **Non-interactive auth script** — `scripts/auth_noninteractive.py` for authenticating Telegram sessions without a TTY (useful for SSH automation, CI pipelines)

### Fixed

- **Session file protection** — Telethon session files are now backed up before each connect attempt. If the container crash-loops (e.g. due to database permission errors), the authenticated session is preserved and restored instead of being overwritten with an empty one
- **Duplicate session_path assignment** in config.py removed

## [7.1.0] - 2026-03-05

### Added

- **Persistent sessions** — Viewer sessions now survive container restarts. Sessions are backed by a `viewer_sessions` database table with an in-memory write-through cache for zero-latency lookups. On startup, active sessions are restored from the database so users stay logged in across restarts, Docker updates, and server reboots. Closes [#84](https://github.com/GeiserX/Telegram-Archive/issues/84).
  - **Alembic migration 009** — Creates `viewer_sessions` table (auto-applied on container startup for both SQLite and PostgreSQL)
  - Graceful degradation: if the database is unavailable, sessions fall back to in-memory only (same behavior as v7.0.x)

### Security

- **Corrupted chat permissions denial** — Sessions with corrupted `allowed_chat_ids` JSON now deny access instead of silently granting access to all chats

## [7.0.3] - 2026-02-27

### Added

- **Viewer-only mode** — When a reverse proxy sets `X-Viewer-Only: true`, master/admin login and all admin API endpoints are blocked. Allows sharing the same backend instance across domains with different access levels.

### Fixed

- **Chat names in admin panel** — Private chats now show `first_name last_name` instead of numeric IDs in the chat picker and viewer list
- **Viewer list shows chat names** — The viewer account list now displays assigned chat titles instead of just a count

## [7.0.2] - 2026-02-27

### Security

- **Per-user push notifications** — Push subscriptions now store the subscriber's `username` and `allowed_chat_ids`. Notifications are only sent to users who have access to the chat where the message was posted. Prevents restricted viewers from receiving push notifications for chats outside their whitelist.
- **Alembic migration 008** — Adds `username` and `allowed_chat_ids` columns to `push_subscriptions` table

### Fixed

- **Stale template cache** — Index HTML now served with `Cache-Control: no-cache, must-revalidate` to prevent browsers from serving outdated templates after upgrades

## [7.0.1] - 2026-02-27

### Fixed

- **Stale template cache** — Added `Cache-Control: no-cache, must-revalidate` header to index.html to prevent browsers from serving stale templates after version upgrades

## [7.0.0] - 2026-02-27

### Added

- **Multi-user viewer access control** — Viewer accounts with per-user chat whitelists. Master (env var) account manages viewer accounts via admin UI. Each viewer sees only their assigned chats across all endpoints and WebSocket. Backward compatible: existing single-user setups work unchanged.
  - `POST /api/admin/viewers` — Create viewer account with username, password, allowed chat IDs
  - `PUT /api/admin/viewers/{id}` — Update viewer account (invalidates sessions)
  - `DELETE /api/admin/viewers/{id}` — Delete viewer account
  - `GET /api/admin/audit` — Paginated audit log
- **Admin settings panel** — Gear icon in sidebar (master only) opens account management UI with viewer CRUD, multi-select chat picker, and activity log
- **Session-based authentication** — Random session tokens replace deterministic PBKDF2 token. Enables real logout, session invalidation, and per-user session limits (max 10)
- **Login rate limiting** — 15 attempts per IP per 5 minutes to prevent brute-force attacks
- **Audit logging** — All login attempts (success/failure), admin actions, and logouts are recorded with IP address and user agent
- **Logout endpoint** — `POST /api/logout` invalidates session and clears cookie (works for both master and viewer)
- **Alembic migration 007** — Creates `viewer_accounts` and `viewer_audit_log` tables

### Security

- **Authenticated media serving** — `/media/*` now requires authentication and validates per-user chat permissions. Previously served via unauthenticated `StaticFiles` mount
- **Path traversal protection** — Media endpoint validates resolved paths stay within the media directory
- **XSS fix** — `linkifyText()` now escapes HTML entities before linkifying URLs, preventing script injection via message text
- **Constant-time token comparison** — All credential comparisons use `secrets.compare_digest`
- **LIKE wildcard escaping** — Search queries no longer treat `%` and `_` as SQL wildcards
- **Generic error messages** — 500 responses no longer leak internal exception details
- **WebSocket per-user enforcement** — Broadcasts now enforce per-connection `allowed_chat_ids`, preventing restricted viewers from receiving messages from unauthorized chats
- **Push notification chat access** — `/api/push/subscribe` validates `chat_id` against user permissions before allowing subscription
- **Media chat-level authorization** — `/media/*` endpoint checks that the requested file belongs to a chat the user has access to
- **Trusted proxy rate limiting** — `X-Forwarded-For` is only trusted from private/Docker IPs, preventing header spoofing to bypass rate limits
- **Stats refresh restricted** — `/api/stats/refresh` now requires master role (was accessible to all authenticated users)
- **Internal push hardened** — `/internal/push` no longer accepts requests when `client_host` is `None`
- **Master username collision** — Creating a viewer account with the same username as the master is rejected

### Changed

- **Auth check endpoint** — `/api/auth/check` now returns `role` ("master"/"viewer") and `username` fields
- **Per-user chat filtering** — All API endpoints and WebSocket subscriptions respect viewer-level `allowed_chat_ids`
- **WebSocket auth** — Validates session cookie during upgrade handshake and enforces per-user chat access

### Contributors

- Thanks to [@PhenixStar](https://github.com/PhenixStar) for the initial concept and discussion in [PR #80](https://github.com/GeiserX/Telegram-Archive/pull/80)

## [6.5.0] - 2026-02-27

### Added

- **Import Telegram Desktop chat exports** — New `telegram-archive import` CLI command reads Telegram Desktop exports (`result.json` + media folders) and inserts them into the database. Imported chats appear in the web viewer like any other backed-up chat. Supports both single-chat and full-account exports. Closes [#81](https://github.com/GeiserX/Telegram-Archive/issues/81).
  - `--path` — Path to export folder containing `result.json`
  - `--chat-id` — Override chat ID (marked format)
  - `--dry-run` — Validate without writing to DB or copying media
  - `--skip-media` — Import only messages/metadata
  - `--merge` — Allow importing into a chat that already has messages
- Handles text messages, photos, videos, documents, voice messages, stickers, and service messages (pins, group actions, etc.)
- Forwards, replies, and edited messages are preserved with full metadata
- Media files are copied into the standard media directory structure

## [6.4.0] - 2026-02-27

### Added

- **`bots` chat type** — New `bots` option for `CHAT_TYPES` to back up bot conversations. Previously, bot chats were silently skipped because they didn't match any chat type (`private`, `groups`, `channels`). Add `bots` to your `CHAT_TYPES` to include them. Bots share `PRIVATE_INCLUDE/EXCLUDE_CHAT_IDS` lists for per-type filtering. Backward compatible — existing configs without `bots` are unaffected.

## [6.3.2] - 2026-02-17

### Fixed

- **Empty chat blank screen** — Chats with no backed-up messages now show a "No messages backed up for this chat yet" empty state instead of a blank screen. Fixes [#78](https://github.com/GeiserX/Telegram-Archive/issues/78).

## [6.3.1] - 2026-02-16

### Fixed

- **Backup resume after crash/restart** — `sync_status` is now updated after every `CHECKPOINT_INTERVAL` batch inserts (default: 1) instead of only at the end of each chat. On crash or power outage, backup resumes from the last committed batch rather than re-fetching all messages for the current chat. Fixes [#76](https://github.com/GeiserX/Telegram-Archive/issues/76).
- **Reduced memory usage on large chats** — Removed in-memory accumulation of all messages per chat; only the current batch is held in memory.

### Added

- **`CHECKPOINT_INTERVAL` environment variable** — Controls how often backup progress is saved (every N batch inserts). Default: `1` (safest). Higher values reduce database writes but increase re-work on crash.

### Refactored

- **Batch commit logic extracted** — Duplicated batch insert code consolidated into `_commit_batch()` helper method.

## [6.3.0] - 2026-02-16

### Added

- **Skip media downloads for specific chats** — New `SKIP_MEDIA_CHAT_IDS` environment variable to skip media downloads for selected chats while still backing up message text. Useful for high-volume media chats where you only need text content. Messages, reactions, and all other data are still fully backed up.
- **Automatic media cleanup for skipped chats** — When `SKIP_MEDIA_DELETE_EXISTING` is `true` (default), existing media files and database records are deleted for chats in the skip list, reclaiming disk space. Set to `false` to keep previously downloaded media while skipping future downloads.
- **Per-chat media control in real-time listener** — The listener now respects `SKIP_MEDIA_CHAT_IDS`, skipping media downloads for new incoming messages in skipped chats.

### Fixed

- **Freed-bytes reporting for deduplicated media** — Media cleanup now correctly reports freed bytes: symlink removals (from deduplicated media) no longer inflate the freed storage count. Only actual file deletions count toward reclaimed space.
- **Empty media directories cleaned up** — After media cleanup, empty per-chat media directories are automatically removed.

### Changed

- **Media cleanup runs once per session** — The cleanup check for skipped chats now uses a session-level cache, avoiding redundant database queries on subsequent backup cycles.

### Contributors

- [@Farzadd](https://github.com/Farzadd) — Initial implementation of `SKIP_MEDIA_CHAT_IDS` ([#74](https://github.com/GeiserX/Telegram-Archive/pull/74))

## [6.2.16] - 2026-02-15

### Fixed

- **Messages intermittently fail to load when clicking chats** — Race condition in `selectChat`: if a previous message load was still in-flight (from another chat, scroll pagination, or auto-refresh), the `loading` gate caused `loadMessages()` to silently return without fetching. Added a version counter to invalidate stale requests and reset the loading gate on chat switch. Also fixes stale auto-refresh results from a previous chat bleeding into the current view.

## [6.2.15] - 2026-02-15

### Fixed

- **Chat search broken (silent 422 error)** — The search bar sent `limit=1000` but the API enforced `le=500`, causing FastAPI to reject every search request with a 422 validation error. The frontend silently swallowed the error, making search appear to return no results. Raised the API limit to 1000 to match the frontend.
- **Chat search ignored in DISPLAY_CHAT_IDS mode** — When `DISPLAY_CHAT_IDS` was configured, the search query was never passed to the database, so typing in the search bar had no effect on the displayed chats.

## [6.2.14] - 2026-02-13

### Fixed

- **PostgreSQL migrations silently rolled back** — The advisory lock used to serialize concurrent migrations was acquired before Alembic's `context.configure()`, triggering SQLAlchemy's autobegin. Alembic detected this as an external transaction and skipped its own transaction management, so DDL changes (new columns, tables) were never committed. Switched to `pg_advisory_xact_lock()` inside the transaction block so Alembic properly commits. Fixes [#70](https://github.com/GeiserX/Telegram-Archive/issues/70).

## [6.2.13] - 2026-02-11

### Fixed

- **Push notifications requiring re-enable** — Push subscriptions can expire (browser push service decides when), causing notifications to silently stop working. The viewer now auto-resubscribes on page load when the browser permission is still granted but the subscription was lost. A `localStorage` flag remembers the user's opt-in preference across subscription losses.
- **Push subscription renewal while tab closed** — Added `pushsubscriptionchange` handler in the service worker so the browser can auto-renew the push subscription even when no tab is open, keeping notifications working indefinitely.

### Changed

- **Refactored push subscription sync** — Extracted `syncSubscriptionToServer()` helper to share logic between initial subscribe, auto-resubscribe, and subscription renewal flows.

## [6.2.12] - 2026-02-09

### Fixed

- **Forum topics always showing same messages** — The auto-refresh (every 3s) was fetching messages without the `topic_id` filter, immediately replacing topic-specific messages with all chat messages. Now properly passes `topic_id` during refresh.
- **"Deleted Account" shown as group name in forum chats** — Clicking a topic passed a minimal object (only `id` and `is_forum`) to the message view, causing `getChatName()` to fall through to "Deleted Account". Now stores and passes the full chat object with title/name fields.

## [6.2.11] - 2026-02-08

### Fixed

- **Backup summary showing zero stats** — The backup completion summary (`Total chats: 0`, `Total messages: 0`, etc.) now calculates statistics directly instead of reading cached values from the viewer. This also pre-populates the stats cache for the viewer on first startup.

### Security

- **Redacted database URL in logs** — The `_safe_url()` method now reconstructs the logged URL entirely from non-sensitive environment variables, ensuring no credential leakage even when `DATABASE_URL` contains a password (CodeQL `py/clear-text-logging-sensitive-data`).

## [6.2.10] - 2026-02-07

### Changed

- **`SECURE_COOKIES` auto-detection** — Default changed from `true` to auto-detect. The viewer now inspects the `X-Forwarded-Proto` header and request scheme to set the `Secure` cookie flag automatically. Behind HTTPS reverse proxies it is `Secure`; over plain HTTP it is not. Explicit `true`/`false` override still works. This fixes silent login failures for users accessing the viewer over HTTP without setting the env var.

### Fixed

- **Archived chats visible in restricted viewers** — The `/api/archived/count` endpoint now respects `DISPLAY_CHAT_IDS`, so the "Archived Chats" row only appears if there are actually archived chats visible to the viewer instance.
- **Doubled archived chats on first click** — Fixed an infinite scroll race condition where navigating to the archived view could trigger a concurrent append fetch (stale `hasMoreChats` from the previous view), duplicating all chat entries on first visit.

## [6.2.9] - 2026-02-07

### Fixed

- **Viewer blank blue page** — Vue.js 3 in-browser template compiler requires `'unsafe-eval'` in the CSP `script-src` directive (it uses `new Function()` internally). Without it, Vue loads but silently fails to compile templates, leaving a blank page. Added `'unsafe-eval'` to fix rendering. Bug present since v6.2.3.

## [6.2.8] - 2026-02-07

### Fixed

- **Viewer CSS/JS broken since v6.2.3** — Content-Security-Policy header blocked all CDN resources (Tailwind CSS, Vue.js, Google Fonts, FontAwesome, Flatpickr), causing the viewer to render without styling or interactivity. Added required CDN domains to `script-src`, `style-src`, and `font-src` directives.

## [6.2.7] - 2026-02-07

### Changed

- **Python 3.14 base image** — Bumped Docker base from `python:3.11-slim` to `python:3.14-slim` in both `Dockerfile` and `Dockerfile.viewer`. All dependencies have pre-built cp314 wheels.
- **Python 3.14 type annotations** — Removed string quotes from forward references (PEP 649 deferred evaluation), replaced `Optional[X]` with `X | None`, simplified `AsyncGenerator` type args (PEP 585).
- **PEP 758 except formatting** — Unparenthesized except clauses now used where applicable.
- **CI updated to Python 3.14** — Tests and lint workflows now run on Python 3.14.
- **Dependabot dev image builds skipped** — `docker-publish-dev` workflow no longer fails on Dependabot PRs (they lack Docker Hub secrets).

## [6.2.6] - 2026-02-07

### Fixed

- **SQLite viewer crash** — Viewer container failed to start when using SQLite because `PRAGMA journal_mode=WAL` requires write access to create `.db-wal` and `.db-shm` sidecar files. WAL and `create_all` are now wrapped in try/except so the viewer degrades gracefully to default journal mode instead of crashing. (#61)
- **Read-only volume mount** — Removed `:ro` from the viewer volume in `docker-compose.yml` since SQLite WAL needs write access. Added comment explaining when `:ro` is safe (PostgreSQL only).

## [6.2.5] - 2026-02-07

### Fixed

- **CodeQL security alerts resolved** — Replaced weak SHA256 auth token with PBKDF2-SHA256 (600k iterations), fixed stack trace exposure in `/internal/push`, and eliminated clear-text password logging by constructing log-safe strings from non-sensitive env vars.
- **CORS credentials with wildcard origins** — Disabled `allow_credentials` when `CORS_ORIGINS=*` (browser security requirement).
- **Auth cookie `Secure` flag** — Cookie now sets `Secure=true` by default, configurable via `SECURE_COOKIES` env var.
- **`/internal/push` access control** — Endpoint restricted to private IPs only (loopback + RFC 1918).
- **Dependabot config** — Removed invalid duplicate Docker ecosystem entry.

### Changed

- **Roadmap updated** — Reflects current v6.x implementation, reordered milestones, added new feature ideas.

## [6.2.4] - 2026-02-07

### Changed

- **Unified environment variables reference** — Consolidated 8+ scattered subsections into one comprehensive table with Scope column (B=backup, V=viewer, B/V=both) and bold category separators.
- **Documented missing env vars** — Added `CORS_ORIGINS`, `SECURE_COOKIES`, and `MASS_OPERATION_BUFFER_DELAY` to the reference table.
- **`ENABLE_LISTENER` master switch** — Prominently documented that `ENABLE_LISTENER=false` disables all `LISTEN_*` and `MASS_OPERATION_*` variables.
- **docker-compose.yml** — Added all missing env vars to both backup and viewer services (listener sub-settings, mass operation, CORS, secure cookies, notifications).
- **.env.example** — Complete rewrite with all variables organized into clear sections.

## [6.2.3] - 2026-02-07

### Added

- **Dependabot configuration** — Automated dependency updates for pip (weekly), GitHub Actions (monthly), and Docker base images (weekly). Groups minor/patch updates, ignores major bumps.
- **Ruff linter and formatter** — Configured in `pyproject.toml` with CI workflow. Replaces flake8/black/isort with a single fast tool. Entire codebase auto-formatted.
- **Pre-commit hooks** — `.pre-commit-config.yaml` with Ruff + standard hooks (check-yaml, trailing-whitespace, etc.).
- **CodeQL security scanning** — Weekly SAST analysis plus on every PR.
- **SECURITY.md** — Responsible disclosure policy with supported versions and scope.
- **CONTRIBUTING.md** — Developer setup guide, branch naming, commit conventions, and testing instructions.
- **PR template** — Checklists for type of change, database changes, data consistency, testing, and security.
- **CODEOWNERS** — Routes all PR reviews to @GeiserX.
- **`.editorconfig`** — Consistent formatting across editors (UTF-8, LF, Python 4-space, YAML 2-space).
- **Content-Security-Policy headers** — CSP, X-Frame-Options, X-Content-Type-Options, and Referrer-Policy on all responses.
- **`CORS_ORIGINS` environment variable** — Configure allowed CORS origins (default: `*` without credentials).
- **`SECURE_COOKIES` environment variable** — Control `secure` flag on auth cookie (default: `true`; set `false` for local HTTP development).

### Fixed

- **CORS misconfiguration** — Removed `allow_credentials=True` when using wildcard origins (browser security requirement). Restricted allowed methods to GET/POST.
- **`/internal/push` access control** — Endpoint now enforces private IP allowlist (loopback + RFC 1918 ranges) instead of silently allowing all requests.
- **Auth cookie missing `secure` flag** — Cookie now sets `secure=True` by default, preventing transmission over plain HTTP.

### Changed

- **Docker Compose security hardening** — Both services now use `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and `tmpfs: [/tmp]`. Viewer volume mounted read-only.
- **GitHub Actions bumped** — `docker/build-push-action` v5→v6, `codecov/codecov-action` v4→v5.
- **Removed `.cursor/rules/project.mdc`** — Redundant with `CLAUDE.md` which is the single source of truth for AI assistant configuration.

## [6.2.2] - 2026-02-07

### Fixed

- **Migration 006 stamping for `create_all()` databases** — SQLite databases created by `create_all()` already include all v6.2.0 schema (forum_topics, is_forum, etc.) but had no `alembic_version`. The stamping logic only detected up to 005, so on restart it tried to re-run migration 006 and failed with `duplicate column name: is_forum`. Now detects the `forum_topics` table as a marker for migration 006

## [6.2.1] - 2026-02-07

### Fixed

- **SQLite migration error on upgrade** — Existing SQLite databases created before Alembic was introduced had no `alembic_version` table. On upgrade to v6.2.0, the entrypoint ran all migrations from scratch, causing `table chats already exists` error. Now detects pre-Alembic SQLite databases and stamps the correct migration version before upgrading (#61)
- **PostgreSQL stamping improvement** — Added migration 005 detection to the PostgreSQL stamping logic (previously only detected up to 004)

## [6.2.0] - 2026-02-06

### Added

- **Forum topics** — Detect forum-enabled channels and extract topic threading (`reply_to_top_id`). Fetch topic metadata via `GetForumTopicsRequest` with fallback inference. Resolve custom emoji document IDs to real unicode emojis. Viewer shows topic list with emoji icons, color indicators, and per-topic message drill-down
- **Chat folders** — Sync user-created Telegram folders via `GetDialogFiltersRequest`. Folder tab bar in viewer sidebar with dynamic filtering
- **Archived chats** — Fetch archived dialogs via `get_dialogs(folder=1)` with clean separation from regular dialogs. Apply same INCLUDE/EXCLUDE/CHAT_TYPES filters. Archived section in viewer with count badge
- **Viewer navigation** — Navigation stack for smart back-button across all views, Telegram-like back navigation preserving main panel content
- **API additions** — `GET /api/folders`, `GET /api/chats/{id}/topics`, `GET /api/archived/count`, plus `archived`, `folder_id`, `topic_id` query params on existing endpoints

### Fixed

- **iOS/mobile scroll** — Fix scroll not working until a programmatic scroll activated it

### Changed

- **Database stability** — PostgreSQL advisory lock to prevent migration deadlocks with concurrent containers. Skip `create_all()` for PostgreSQL (Alembic manages schema exclusively)
- **Migration 006** — Adds `is_forum`, `is_archived` columns to `chats`; `reply_to_top_id` column to `messages`; new tables: `forum_topics`, `chat_folders`, `chat_folder_members`

## [6.1.1] - 2026-02-06

### Fixed

- **Critical: `schedule` command would silently do nothing** - The `run_schedule` function in the CLI called the async `scheduler.main()` without `asyncio.run()`, causing the scheduler to never actually start. This affected all Docker deployments using `python -m src schedule`.

### Changed

- **Removed `:latest` tag from CLI help text** - Docker examples in `--help` output now use `<version>` placeholder instead of `:latest`, following the project convention of always using specific version tags.

## [6.1.0] - 2026-02-06

### Community Contributions

This release includes a major contribution from **[@yarikoptic](https://github.com/yarikoptic)** (Yaroslav Halchenko) - thank you for this substantial improvement to the project!

### Added

- **Unified CLI interface** (`python -m src <command>`) - All operations now route through a single entry point with intuitive subcommands: `auth`, `backup`, `schedule`, `export`, `stats`, `list-chats`. Includes comprehensive `--help` with workflow guidance. (contributed by @yarikoptic, PR #57)

- **Python packaging with `pyproject.toml`** - Proper PEP 621 package definition with centralized dependencies. Install locally with `pip install -e .` to get the `telegram-archive` command. (contributed by @yarikoptic, PR #57)

- **`--data-dir` option for local development** - Override the default `/data` directory to avoid permission issues when developing outside Docker:
  ```bash
  telegram-archive --data-dir ./data list-chats
  python -m src --data-dir ./data backup
  ```

- **`telegram-archive` executable script** - Direct execution without installation (`./telegram-archive --help`). (contributed by @yarikoptic, PR #57)

- **Smart database migrations in entrypoint** - Migrations now skip for `auth` command (no DB needed yet) and check database existence before running SQLite migrations. (contributed by @yarikoptic, PR #57)

### Changed

- **Dockerfile default CMD now shows help** - Running the container without an explicit command displays help instead of silently starting the scheduler. The `docker-compose.yml` explicitly runs `schedule`. This is a behavioral change for users running `docker run` without a command - add `python -m src schedule` to your command.

- **Unified command syntax** - Old module-based commands (`python -m src.telegram_backup`, `python -m src.export_backup stats`) are replaced by `python -m src backup`, `python -m src stats`, etc.

## [6.0.3] - 2026-02-02

### Community Contributions

This release includes contributions from **[@yarikoptic](https://github.com/yarikoptic)** - welcome to the project! 🎉

### Improved

- **Better error messages for permission issues** (#54, #55) - Authentication setup now provides clear troubleshooting guidance when encountering permission errors (common with Podman or Docker UID mismatches):
  ```
  PERMISSION ERROR - Unable to write to session directory
  
  For Podman users:
    Add --userns=keep-id to your run command
  
  For Docker users:
    mkdir -p data && sudo chown -R 1000:1000 data
  ```

### Changed

- **Standardized on `docker compose` (v2) syntax** - All documentation and scripts now use the modern `docker compose` command instead of the deprecated `docker-compose` (v1). Docker Compose v2 has been built into Docker CLI since mid-2021, and v1 was deprecated in July 2023. (contributed by @yarikoptic)

- **`init_auth.sh` is now executable by default** - No need to manually run `chmod +x init_auth.sh` before using the script. (contributed by @yarikoptic)

### Added

- **Shellcheck CI workflow** - Added GitHub Actions workflow to lint shell scripts on push/PR, improving code quality for bash scripts. (contributed by @yarikoptic)

## [6.0.2] - 2026-02-02

### Fixed
- **Reduced Telethon disconnect warnings** (#50) - Added graceful disconnect handling to reduce "Task was destroyed but it is pending" asyncio warnings during shutdown or reconnection. These warnings are caused by a [known Telethon issue](https://github.com/LonamiWebs/Telethon/issues/782) and don't affect functionality.

### Technical
- Added small delay after `client.disconnect()` to allow internal task cleanup
- Wrapped disconnect in try/except to handle cleanup errors gracefully

## [6.0.1] - 2026-01-30

### Fixed
- **Graceful handling of inaccessible chats** (fixes #49) - When you lose access to a channel/group (kicked, banned, left, or it went private), the backup now logs a clean warning instead of a full error traceback:
  ```
  WARNING - → Skipped (no access): ChannelPrivateError
  ```
  Previously this would show a confusing multi-line error that looked like a bug.

### Technical
- Added specific error handling for `ChannelPrivateError`, `ChatForbiddenError`, and `UserBannedInChannelError`
- These Telegram API responses are now treated as expected conditions, not application errors

## [6.0.0] - 2026-01-28

### ⚠️ Breaking Changes

This is a major release with breaking schema changes. **Backup your database before upgrading.**

#### Normalized Media Storage

Media metadata is now stored exclusively in the `media` table instead of being duplicated in the `messages` table.

**Removed columns from `messages` table:**
- `media_type`
- `media_id`
- `media_path`

**API response format changed:**

Before (v5.x):
```json
{
  "id": 123,
  "media_type": "photo",
  "media_path": "/data/backups/media/123/file.jpg",
  "media_file_name": "photo.jpg",
  "media_mime_type": "image/jpeg"
}
```

After (v6.0.0):
```json
{
  "id": 123,
  "media": {
    "type": "photo",
    "file_path": "/data/backups/media/123/file.jpg",
    "file_name": "photo.jpg",
    "file_size": 12345,
    "mime_type": "image/jpeg",
    "width": 1920,
    "height": 1080
  }
}
```

#### Service Messages and Polls

- Service messages: Now detected by `raw_data.service_type === 'service'` instead of `media_type === 'service'`
- Polls: Now detected by presence of `raw_data.poll` instead of `media_type === 'poll'`

### Added

#### Simple Whitelist Mode with `CHAT_IDS` (fixes #48)

New `CHAT_IDS` environment variable provides a simple way to backup only specific chats:

```bash
# Backup ONLY these 2 channels - nothing else
CHAT_IDS=-1001234567890,-1009876543210
```

**Two filtering modes:**

| Mode | When | How it works |
|------|------|--------------|
| **Whitelist** | `CHAT_IDS` is set | Backup ONLY the listed chats. All other settings ignored. |
| **Type-based** | `CHAT_IDS` not set | Use `CHAT_TYPES` + `INCLUDE`/`EXCLUDE` filters (existing behavior). |

This solves the common confusion where users expected `CHANNELS_INCLUDE_CHAT_IDS` to act as a whitelist, but it was actually additive.

#### Removed `LISTEN_ALBUMS` Setting (fixes #46)

The `LISTEN_ALBUMS` setting was redundant and has been removed. Albums are now automatically handled via `grouped_id` in the NewMessage handler. The viewer groups messages by `grouped_id` to display albums correctly.

#### Foreign Key Constraints
- `media(message_id, chat_id)` → `messages(id, chat_id)` (ON DELETE CASCADE)
- `reactions.user_id` → `users.id` (nullable, ON DELETE SET NULL)

**Note:** `messages.sender_id` does NOT have a FK constraint because sender_id can contain channel/group IDs that aren't in the users table. The relationship is maintained at ORM level only.

#### New Indexes
- `idx_messages_reply_to` - Fast reply message lookups
- `idx_media_downloaded` - Find undownloaded media by chat
- `idx_media_type` - Filter media by type
- `idx_reactions_user` - User reaction queries
- `idx_chats_username` - Chat username lookups
- `idx_users_username` - User username lookups

### Changed

- **Media file_path column type**: Changed from `String(500)` to `Text` to support longer paths
- **Media relationship**: Messages now have a `media_items` relationship for direct access

### Migration Guide

The Alembic migration handles data migration automatically:

1. **Backup your database** before upgrading
2. The migration will:
   - Copy any missing media data from `messages` to `media` table
   - Create a backup table `_messages_media_backup` for rollback
   - Drop the `media_type`, `media_id`, `media_path` columns
   - Add foreign key constraints
   - Create new indexes

**Run the migration:**
```bash
# If using Docker
docker exec telegram-backup alembic upgrade head

# If running locally
alembic upgrade head
```

**Rollback if needed:**
```bash
alembic downgrade 004
```

### Technical Notes

- SQLite: Uses table recreation for schema changes (SQLite doesn't support DROP COLUMN in older versions)
- PostgreSQL: Uses direct ALTER TABLE operations
- Migration is reversible - downgrade restores columns from backup table

## [5.4.9] - 2026-01-28

### Added

- **Notification deep links** — Clicking a push notification now opens the viewer directly at the relevant chat

## [5.4.8] - 2026-01-27

### Fixed

- **Migration retry logic** — Added retry logic for PostgreSQL connection during migrations, handling transient connection failures on startup

## [5.4.7] - 2026-01-26

### Fixed

- **Push notifications respect `DISPLAY_CHAT_IDS`** — Push notifications now filter by the viewer's `DISPLAY_CHAT_IDS` configuration, preventing notifications for chats not shown in the viewer

## [5.4.6] - 2026-01-26

### Fixed

- **Auto-stamp pre-Alembic databases** — Existing databases created before Alembic was introduced are now automatically detected and stamped with the correct migration version on startup

## [5.4.5] - 2026-01-26

### Fixed

- **PWA icon backgrounds** — Added dark background to PWA icons for better visibility on light home screens

## [5.4.4] - 2026-01-26

### Added

- **PWA manifest and dark logo** — Proper PWA manifest with dark logo for installable web app experience

## [5.4.3] - 2026-01-26

### Fixed

- **VAPID push headers** — Use `py_vapid sign()` for VAPID headers, fixing push notification delivery failures

## [5.4.2] - 2026-01-26

### Fixed

- **Service worker scope** — Serve service worker from root with correct scope, fixing push notification registration failures

## [5.4.1] - 2026-01-25

### Fixed
- **Scroll-to-bottom button not appearing** - Fixed detection logic for `flex-col-reverse` containers where `scrollTop` is negative when scrolled up

## [5.4.0] - 2026-01-25

### Added

#### Multiple Pinned Messages Support
- **Pinned message banner** - Shows currently pinned message at the top of the chat, matching Telegram's UI
- **Pin navigation** - Click the message content to scroll to that pinned message and cycle through others
- **Pin count indicator** - Shows "(1 of N)" when multiple messages are pinned
- **Pinned Messages view** - Click the list icon to view all pinned messages in a dedicated view
- **Real-time pin sync** - Listener now catches pin/unpin events when `ENABLE_LISTENER=true`
- **Automatic pin sync** - Pinned messages are synced on every backup (no manual migration needed)
- **API endpoint** - `GET /api/chats/{chat_id}/pinned` returns all pinned messages

#### Database
- **`is_pinned` column** - New column on messages table to track pinned status
- **Alembic migration** - Migration `004` adds the column and index automatically

### Fixed
- **Auto-load older messages** - Replaced manual "Load older messages" button with automatic Intersection Observer loading
- **Telegram-style loading spinner** - Shows spinning indicator while fetching older messages
- **Alembic migrations auto-run** - Docker image now includes Alembic and runs migrations automatically on startup for PostgreSQL

### Upgrade Notes

**Database Migration Required:**

The migration runs automatically on startup. If you're using PostgreSQL, ensure the backup container has write access.

After upgrading, pinned messages will be populated on the next backup run. If you want to populate them immediately without waiting for the next backup:

```bash
# Trigger a manual backup to sync pinned messages
docker exec telegram-backup python -m src backup
```

If using the real-time listener (`ENABLE_LISTENER=true`), pin/unpin events will be captured automatically going forward.

## [5.3.7] - 2026-01-22

### Fixed
- **Avatar filename mismatch** (#35, #41) - Avatars are now saved as `{chat_id}_{photo_id}.jpg` to match what the viewer expects. Previously saved as `{chat_id}.jpg` which caused avatars to not display.

### Added
- **`scripts/cleanup_legacy_avatars.py`** - Utility script to remove old `{chat_id}.jpg` avatar files after they've been replaced by the new format. Run with `--dry-run` to preview changes.

### Changed
- **Shared avatar utility** - Avatar path generation moved to `src/avatar_utils.py` for consistency between backup and listener
- **Skip redundant downloads** - Avatars are only downloaded when the file doesn't exist or is empty

### Upgrade Notes
Legacy avatar files (`{chat_id}.jpg`) are still supported via fallback. To clean up old files after new-format avatars are downloaded:
```bash
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py --dry-run  # Preview
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py            # Apply
```

## [5.3.6] - 2026-01-21

### Fixed

- **Avatar download type check** — Avatar download now uses photo type check instead of `photo_id`, fixing cases where avatars failed to download

## [5.3.5] - 2026-01-21

### Fixed

- **Avatar download on `photo_changed` event** — Avatars are now downloaded when a `photo_changed` chat action event is detected by the listener

## [5.3.4] - 2026-01-21

### Fixed

- **Push notification session factory** — Corrected session factory access in push notifications, fixing notification delivery failures

## [5.3.3] - 2026-01-20

### Fixed
- **Listener media deduplication** - Real-time listener now uses the same deduplication logic as scheduled backups, creating symlinks to `_shared` directory instead of downloading duplicates

## [5.3.2] - 2026-01-20

### Added
- **Forwarded message info** - Shows the original sender's name for forwarded messages (resolved from Telegram when possible)
- **Channel post author** - Shows the post author (signature) for channel messages when enabled in the channel

### Fixed
- **Avatar refresh not working** (#35) - Simplified avatar logic to always update on each backup. Removed `AVATAR_REFRESH_HOURS` config (was unreliable)

### Removed
- `AVATAR_REFRESH_HOURS` environment variable - Avatars now update on every backup run automatically

## [5.3.1] - 2026-01-20

### Fixed
- **Album duplicates showing** - Fixed `grouped_id` comparison (string vs integer) causing albums to show duplicate placeholder messages. Added `getGroupedId()` helper that converts to string for consistent comparison.

### Added
- **Service messages** - Chat actions (photo changed, title changed, user joined/left) now display as centered service messages in the viewer, like the real Telegram client
- **`scripts/normalize_grouped_ids.py`** - Migration script to normalize old `grouped_id` values to strings. Run with `--dry-run` to preview changes.

### Upgrade Notes
If you have existing albums showing as duplicates, run the migration script:
```bash
docker exec telegram-backup python scripts/normalize_grouped_ids.py --dry-run  # Preview
docker exec telegram-backup python scripts/normalize_grouped_ids.py            # Apply
```

## [5.3.0] - 2026-01-19

### Fixed

#### Bug Fixes
- **Long message notification error** (#36) - Truncate notification payload to avoid PostgreSQL NOTIFY 8KB limit
- **Non-Latin export encoding** (#34) - JSON export now uses UTF-8 encoding with RFC 5987 filename encoding
- **ChatAction photo_removed error** (#28) - Fixed `AttributeError: 'Event' object has no attribute 'photo_removed'`
- **Album grouping flaky** (#29) - Albums now save correct media_type (photo/video) instead of generic 'album'
- **Album media not downloading** (#31) - Album handler now downloads media when `LISTEN_NEW_MESSAGES_MEDIA=true`
- **Sender name position** - Fixed sender names appearing at bottom instead of top with flex-col-reverse layout

### Changed
- Improved documentation for chat filtering options (`GLOBAL_INCLUDE_CHAT_IDS` vs type-specific) (#33)

## [5.2.0] - 2026-01-18

### Fixed

#### Critical Bug Fixes
- **`get_statistics` missing** - Fixed `AttributeError: 'DatabaseAdapter' object has no attribute 'get_statistics'` at end of backup (#23)
- **FK violation on new chats** - Listener now creates chat record before inserting messages, fixing foreign key violations when adding new `PRIORITY_CHAT_IDS` (#25)
- **VIEWER_TIMEZONE not applied** - Times were showing in UTC instead of configured timezone; now properly converts from UTC to viewer timezone (#24)
- **LOG_LEVEL=WARN not working** - Added alias mapping from `WARN` to `WARNING` for Python compatibility (#26)
- **Date separators position** - Fixed date separators appearing at wrong position with flex-col-reverse layout

#### Mobile UI Improvements (iOS/Android)
- **Avatar distortion** - Chat avatars were rendering as ellipsoids on mobile; now perfectly round with `aspect-square` and `shrink-0`
- **Chat name overflow** - Long channel names caused massive header bars; now truncated with `max-width` on mobile
- **Search bar too wide** - Reduced from fixed 256px to responsive `w-28 sm:w-48 md:w-64`
- **Export button hidden** - Was pushed off-screen on small devices; now always visible with compact sizing
- **White status bar strips** - Added `theme-color` meta tag and safe area insets for proper iOS status bar theming

### Added

#### Integrated Media Lightbox
- **Image lightbox** - Click images to view fullscreen instead of opening new tab
- **Video lightbox** - Videos now open in integrated player with autoplay
- **Media navigation** - Navigate between all media (photos, videos, GIFs) with arrow keys or buttons
- **Keyboard shortcuts** - `←`/`→` to navigate, `Esc` to close
- **Play button overlay** - Video thumbnails show play button for clear affordance
- **Download button** - Download media directly from lightbox

#### Performance & UX
- **flex-col-reverse scroll** - Messages container uses CSS-based instant scroll-to-bottom (no JS hacks, better mobile performance)
- iOS Safe Area support (`env(safe-area-inset-*)`) for notch/Dynamic Island devices
- `apple-mobile-web-app-capable` meta tag for PWA-like experience
- Responsive header padding (`px-2 py-2` on mobile, `px-4 py-3` on desktop)

## [5.1.0] - 2026-01-18

### Fixed

#### iOS Safari / In-App Browser Compatibility
- **Critical**: Fixed JavaScript crash when `Notification` API is undefined (iOS Safari, in-app browsers)
  - The Vue app would crash before auth check could run, showing "Authentication is disabled"
  - Now uses `typeof Notification !== 'undefined'` check instead of optional chaining
- **Fixed**: Auth check returning `null` instead of `false` when cookie is missing
  - Python's `None and X` returns `None`, not `False` - now wrapped in `bool()`
- Added `authCheckFailed` state with helpful message for in-app browser users

#### Notification Improvements
- Added "Notifications blocked" banner when push is subscribed but browser has denied permission
- Users can unsubscribe from push directly from the banner

### Added
- **`AUTH_SESSION_DAYS`** - Configure authentication session duration (default: 30 days)
- Auth test page at `/static/test-auth.html` for debugging (temporary)

### Documentation
- Added missing env vars: `AUTH_SESSION_DAYS`, `BATCH_SIZE`, `DATABASE_TIMEOUT`, `SESSION_NAME`
- Updated mass operation protection docs to reflect actual behavior (rate limiting, not zero-footprint)

## [5.0.0] - 2026-01-18

### ⚠️ Major Release - Real-time Sync & Media Path Changes

This release introduces **real-time message sync**, **zero-footprint mass operation protection**, and **consistent media path naming**. Migration scripts are provided for existing installations.

### Added

#### Real-time Listener Mode
- **`ENABLE_LISTENER`** - Background listener for instant sync (no waiting for scheduled backup)
- **`LISTEN_EDITS`** - Apply text edits to backed up messages in real-time
- **`LISTEN_DELETIONS`** - Mirror deletions from Telegram (with protection, see below)
- **`LISTEN_NEW_MESSAGES`** - Save new messages immediately (default: true)
- **`LISTEN_NEW_MESSAGES_MEDIA`** - Download media in real-time (default: false)
- **`LISTEN_CHAT_ACTIONS`** - Track chat photo/title changes, member joins/leaves
- **`LISTEN_ALBUMS`** - Detect and group album uploads together

#### Mass Operation Rate Limiting
- **Sliding-window rate limiter** protects against mass edit/deletion attacks
- **`MASS_OPERATION_THRESHOLD`** - Max operations per chat before blocking (default: 10)
- **`MASS_OPERATION_WINDOW_SECONDS`** - Time window for counting operations (default: 30)
- First N operations are applied, then chat is blocked for remainder of window
- To prevent ANY deletions from affecting your backup, set `LISTEN_DELETIONS=false`

#### Priority Chats
- **`PRIORITY_CHAT_IDS`** - Process these chats FIRST in all backup/sync operations
- Useful for ensuring important chats are always backed up before others

#### Viewer Enhancements
- **WebSocket real-time updates** - New messages appear instantly without refresh
- **Infinite scroll** - Cursor/keyset pagination for large chats
- **Album grid display** - Photo/video albums shown as grids like Telegram
- **Compact stats dropdown** - Stats moved to dropdown next to header
- **Per-chat stats** - Message count, media count, total size per chat
- **"Real-time sync" indicator** - Shows when listener is active
- **`SHOW_STATS`** - Hide stats dropdown for restricted viewers (default: true)

#### Web Push Notifications
- **`PUSH_NOTIFICATIONS`** - Notification mode: `off`, `basic`, `full` (default: basic)
  - `off` - No notifications at all
  - `basic` - In-browser notifications (tab must be open)
  - `full` - **Persistent Web Push** (works even when browser is closed!)
- **Auto-generated VAPID keys** - Stored in database, persist across restarts
- **Subscription management** - Subscriptions survive container restarts and updates
- **Automatic cleanup** - Expired subscriptions removed automatically
- **Optional custom VAPID keys** via `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT`

#### Migration Scripts
- **`scripts/migrate_media_paths.py`** - ⚠️ **REQUIRED** - Normalizes media folder names to use marked IDs
- **`scripts/update_media_sizes.py`** - ⚠️ **REQUIRED** - Populates file_size for accurate stats
- **`scripts/detect_albums.py`** - ⚠️ **HIGHLY RECOMMENDED** - Detect albums in existing backups for album grid display
- **`scripts/deduplicate_media.py`** - ⚠️ **HIGHLY RECOMMENDED** - Global deduplication using symlinks (saves disk space)
- **`scripts/restore_chat.py`** - Repost archived messages to Telegram

### Changed
- **Shared Telethon client** - Backup and listener share connection (avoids session DB locks)
- **WAL mode for session DB** - Better concurrency for Telethon session
- **Media folder naming** - Groups/channels now use marked IDs (e.g., `-35258041/` not `35258041/`)
- **Bulk SQL operations** - Migration scripts use single queries per batch (10-100x faster)

### Fixed
- Media 404s due to inconsistent folder naming (positive vs negative IDs)
- Audio files served with wrong Content-Type (now audio/ogg, audio/mp3, etc.)
- Stats calculation error with Decimal types (JSON serialization)
- Session DB locking when running backup and listener simultaneously

### ⚠️ Migration Required

**If upgrading from v4.x with existing data:**

1. **Run migration scripts** (inside Docker container):
   ```bash
   # 1. Normalize media paths (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.migrate_media_paths
   
   # 2. Update file sizes for accurate stats (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.update_media_sizes
   
   # 3. Detect albums for grid display (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.detect_albums
   
   # 4. Deduplicate media files (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.deduplicate_media
   ```

2. **Update docker-compose.yml** with new env variables (see README)

See [Upgrading to v5.0.0](#upgrading-to-v500-from-v4x) below for detailed instructions.

### Related Issues
- Fixes #12 - Timezone-aware datetime sorting
- Fixes #20 - Real-time sync for edits/deletions
- Fixes #21 - Mass operation protection
- Fixes #22 - Media path consistency

## [4.1.5] - 2026-01-15

### Improved
- **Quick Start guide** - Expanded with step-by-step instructions for beginners
- **Database configuration** - Added prominent warning about viewer needing same DB path
- **Troubleshooting table** - Common permission and setup issues
- **docker-compose.yml** - Clearer comments about matching DB settings

### Added
- `scripts/release.sh` - Validates changelog entry before allowing tag creation

## [4.1.4] - 2026-01-15

### Changed
- Moved all upgrade notices from README to `docs/CHANGELOG.md`
- README now references CHANGELOG for upgrade instructions

### Improved
- Release workflow now extracts changelog notes for GitHub releases
- Added release guidelines to CLAUDE.md
- Documented chat ID format requirements

## [4.1.3] - 2026-01-15

### Added
- Prominent startup banner showing SYNC_DELETIONS_EDITS status
- Makes it clear why backup re-checks all messages from the start

## [4.1.2] - 2026-01-15

### Fixed
- **PostgreSQL reactions sequence out of sync** - Auto-detect and recover from sequence drift
- Prevents `UniqueViolationError` on reactions table after database restores

### Added
- `scripts/fix_reactions_sequence.sql` - Manual fix script for affected users
- Troubleshooting section in README for this issue

## [4.1.1] - 2026-01-15

### Added
- **Auto-correct DISPLAY_CHAT_IDS** - Viewer automatically corrects positive IDs to marked format (-100...)
- Helps users who forget the -100 prefix for channels/supergroups

## [4.1.0] - 2026-01-14

### Added
- **Real-time listener** for message edits and deletions (`ENABLE_LISTENER=true`)
- Catches changes between scheduled backups
- `SYNC_DELETIONS_EDITS` option for batch sync of all messages

### Fixed
- Timezone handling for `edit_date` field (PostgreSQL compatibility)
- Tests updated for pytest compatibility

## [4.0.7] - 2026-01-14

### Fixed
- Strip timezone from `edit_date` before database insert/update
- Prevents `asyncpg.DataError` with PostgreSQL TIMESTAMP columns

## [4.0.6] - 2026-01-14

### Fixed
- **CRITICAL: Chat ID format mismatch** - Use marked IDs consistently
- Chats now stored with proper format (-100... for channels/supergroups)

### ⚠️ Breaking Change
**Database migration required if upgrading from v4.0.5!**

See [Upgrading to v4.0.6](#upgrading-to-v406-from-v405) below.

## [4.0.5] - 2026-01-13

### Added
- CI workflow for dev builds on PRs
- Tests for timezone and ID format handling

### Known Issues
- Chat ID format bug (fixed in v4.0.6)

## [4.0.4] - 2026-01-12

### Fixed
- `CHAT_TYPES=` (empty string) now works for whitelist-only mode
- Previously caused ValueError due to incorrect env parsing

## [4.0.3] - 2026-01-11

### Fixed
- Environment variable parsing for empty CHAT_TYPES

## [4.0.2] - 2026-01-05

### Changed

- **Viewer title** — Renamed viewer browser title to "Telegram Archive"
- **PostgreSQL version** — Updated docker-compose example to PostgreSQL 18

## [4.0.1] - 2026-01-05

### Fixed

- **Timezone stripping for PostgreSQL** — Strip timezone from datetimes for PostgreSQL compatibility
- **Async merge fix** — Fixed async database merge operations

### Added

- **Migration script** — Added migration script for v3.x to v4.0 database upgrade
- **Upgrade guide** — Added v3.x to v4.0 upgrade documentation and updated docker-compose.yml with new image names

## [4.0.0] - 2026-01-10

### ⚠️ Breaking Change
**Docker image names changed!**

| Old (v3.x) | New (v4.0+) |
|------------|-------------|
| `drumsergio/telegram-backup-automation` | `drumsergio/telegram-archive` |
| Same image with command override | `drumsergio/telegram-archive-viewer` |

See [Upgrading from v3.x to v4.0](#upgrading-from-v3x-to-v40) below.

### Changed
- Split into two Docker images (backup + viewer)
- Viewer image is smaller (~150MB vs ~300MB)

## [3.0.5] - 2025-12-31

### Fixed

- **Empty `CHAT_TYPES` for whitelist-only mode** — Allow empty `CHAT_TYPES` for users who only want to back up explicitly listed chats

### Added

- **GitHub issue templates** — Bug report, feature request, and question templates
- **FUNDING.yml** — GitHub Sponsors configuration
- **Roadmap** — Added roadmap section with planned features including multi-tenancy, OAuth, and magic links

## [3.0.4] - 2025-12-19

### Changed

- **Documentation update** — Updated README and `.env.example` with v2 backward compatibility information

## [3.0.3] - 2025-12-19

### Fixed

- **v2 backward compatibility** — Added backward compatibility for v2 `DATABASE_PATH` and `DATABASE_DIR` environment variables, so upgrades from v2 work without changing configuration

## [3.0.2] - 2025-12-19

### Fixed

- **`create_all` idempotency** — Use `checkfirst=True` in `create_all()` to skip existing tables, preventing errors when restarting with an existing database

## [3.0.1] - 2025-12-19

### Fixed

- **Reaction model foreign key** — Added `ForeignKeyConstraint` to Reaction model for composite key, fixing database integrity issues with reaction storage

## [3.0.0] - 2025-12-19

### Added
- PostgreSQL support
- Async database operations with SQLAlchemy
- Alembic migrations

### Changed
- Database layer rewritten for async

## [2.x] - 2025-XX-XX

### Features
- SQLite database
- Web viewer
- Media download support

---

# Upgrading

## Upgrading to v5.0.0 (from v4.x)

> ⚠️ **Migration Scripts Recommended**

v5.0.0 changes media folder naming to use marked IDs consistently. While the backup will work without migration, **running the migration scripts is highly recommended** for:
- Correct media display in viewer (no 404s)
- Accurate file size statistics
- Album grid display for existing photos/videos

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker compose stop telegram-backup
   ```

2. **Pull the new image:**
   ```bash
   docker compose pull
   ```

3. **Run migration scripts** (one at a time, wait for each to finish):

   ```bash
   # Replace with your actual values
   NETWORK=telegram-backup_default
   DB_HOST=your-postgres-container
   DB_PASS=your-password
   BACKUP_PATH=/path/to/backups
   
   # 1. Media path migration (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.migrate_media_paths
   
   # 2. Update file sizes (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.update_media_sizes
   
   # 3. Detect albums (optional but recommended)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.detect_albums
   ```

4. **Update docker-compose.yml** with new env variables:
   ```yaml
   environment:
     # ... existing vars ...
     # Real-time listener (recommended)
     ENABLE_LISTENER: true
     LISTEN_EDITS: true
     LISTEN_DELETIONS: true  # ⚠️ Will delete from backup!
     LISTEN_NEW_MESSAGES: true
     # Mass operation protection
     MASS_OPERATION_THRESHOLD: 10
     MASS_OPERATION_WINDOW_SECONDS: 30
     # Optional: Priority chats (processed first)
     # PRIORITY_CHAT_IDS: -1002240913478,-1001234567890
   ```

5. **Start the new version:**
   ```bash
   docker compose up -d
   ```

**If starting fresh:** No migration needed, just use the new image.

---

## Upgrading to v4.0.6 (from v4.0.5)

> 🚨 **Database Migration Required**

v4.0.5 had a bug where chats were stored with positive IDs while messages used negative (marked) IDs, causing foreign key violations.

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker compose stop telegram-backup
   ```

2. **Run the migration script:**

   **PostgreSQL:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids.sql
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < migrate_to_marked_ids.sql
   ```

   **SQLite:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids_sqlite.sql
   sqlite3 /path/to/telegram_backup.db < migrate_to_marked_ids_sqlite.sql
   ```

3. **Pull and restart:**
   ```bash
   docker compose pull
   docker compose up -d
   ```

**If upgrading from v4.0.4 or earlier:** No migration needed.
**If starting fresh:** No migration needed.

---

## Upgrading from v3.x to v4.0

> ⚠️ **Docker image names changed**

### Update your docker-compose.yml:

```yaml
# Before (v3.x)
telegram-backup:
  image: drumsergio/telegram-backup-automation:latest

telegram-viewer:
  image: drumsergio/telegram-backup-automation:latest
  command: uvicorn src.web.main:app --host 0.0.0.0 --port 8000

# After (v4.0+)
telegram-backup:
  image: drumsergio/telegram-archive:latest

telegram-viewer:
  image: drumsergio/telegram-archive-viewer:latest
  # No command needed
```

Then:
```bash
docker compose pull
docker compose up -d
```

**Your data is safe** - no database migration needed.

---

## Upgrading from v2.x to v3.0

Transparent upgrade - just pull and restart:
```bash
docker compose pull
docker compose up -d
```

Your existing SQLite data works automatically. v3 detects v2 environment variables for backward compatibility.

**Optional:** Migrate to PostgreSQL - see README for instructions.

---

## Chat ID Format (Important!)

Since v4.0.6, all chat IDs use Telegram's "marked" format:

| Entity Type | Format | Example |
|-------------|--------|---------|
| Users | Positive | `123456789` |
| Basic groups | Negative | `-123456789` |
| Supergroups/Channels | -100 prefix | `-1001234567890` |

**Finding Chat IDs:** Forward a message to @userinfobot on Telegram.

When configuring `GLOBAL_EXCLUDE_CHAT_IDS`, `DISPLAY_CHAT_IDS`, etc., use the marked format.
