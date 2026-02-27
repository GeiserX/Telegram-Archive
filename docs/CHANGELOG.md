# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [Unreleased]

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

...