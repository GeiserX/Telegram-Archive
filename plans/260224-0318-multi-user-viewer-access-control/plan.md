---
title: "Multi-User Viewer Access Control"
description: "Add per-user viewer accounts with scoped chat access managed by admin"
status: complete
priority: P1
effort: 10h
branch: feat/web-viewer-enhancements
tags: [auth, multi-user, access-control, admin-ui]
created: 2026-02-24
---

# Multi-User Viewer Access Control

## Summary

Add viewer account management so the master (env-var) user can create viewer accounts with scoped chat access. Each viewer only sees their allowed chats. Master sees all chats and has admin settings UI.

## Phases

| # | Phase | Effort | Status |
|---|-------|--------|--------|
| 1 | [DB Schema & Auth Backend](phase-01-db-schema-and-auth-backend.md) | 2.5h | complete |
| 2 | [API Endpoints & Chat Filtering](phase-02-api-endpoints-and-chat-filtering.md) | 2.5h | complete |
| 3 | [Admin Settings UI](phase-03-admin-settings-ui.md) | 3h | complete |
| 4 | [Testing & Migration](phase-04-testing-and-migration.md) | 2h | complete |

## Key Decisions

- **No new deps**: Use `hashlib.pbkdf2_hmac` (already used for master token) instead of passlib/bcrypt
- **Single cookie model**: Cookie value encodes user identity; master token stays as-is for backward compat
- **viewer_accounts table**: New SQLAlchemy model, auto-created by `create_all` (SQLite) + Alembic migration (PostgreSQL)
- **Scoped filtering**: Replace all `config.display_chat_ids` checks with per-user allowed_chat_ids resolution
- **Admin = env-var user**: No DB record for admin; always authenticated via env vars

## Dependencies

- Existing auth: `src/web/main.py` lines 365-502
- Chat filtering: `config.display_chat_ids` used in 15+ endpoints
- DB layer: `src/db/base.py` (async engine), `src/db/models.py`, `src/db/adapter.py`
- Frontend: `src/web/templates/index.html` (Vue 3 SPA)

## Files Modified

| File | Changes |
|------|---------|
| `src/db/models.py` | Add `ViewerAccount` model |
| `src/web/main.py` | Multi-user auth, admin CRUD endpoints, per-user filtering |
| `src/web/templates/index.html` | Admin settings panel, cog icon, user management UI |
| `alembic/versions/007_*.py` | New migration for `viewer_accounts` table |
| `tests/test_auth.py` | Multi-user auth tests |

## Backward Compatibility

- No viewer accounts in DB = exact current behavior
- Master cookie token unchanged; existing sessions keep working
- `DISPLAY_CHAT_IDS` deprecated — master always sees ALL chats

## Validation Log

### Session 1 — 2026-02-24
**Trigger:** Initial plan creation validation before implementation
**Questions asked:** 7

#### Questions & Answers

1. **[Scope]** Should the master user always see ALL chats, or should DISPLAY_CHAT_IDS still limit the master when no viewer accounts exist?
   - Options: Master always sees ALL chats (Recommended) | Master respects DISPLAY_CHAT_IDS when no viewers exist | DISPLAY_CHAT_IDS removed entirely
   - **Answer:** Master always sees ALL chats
   - **Rationale:** Master is admin — should have unrestricted view. DISPLAY_CHAT_IDS only useful as default suggestion for new viewer accounts.

2. **[Architecture]** In-memory session store means viewer sessions are lost on container restart (users must re-login). Acceptable, or should sessions be DB-backed?
   - Options: In-memory is fine (Recommended) | DB-backed sessions | Redis/file-backed
   - **Answer:** In-memory is fine
   - **Rationale:** Viewer app is single-instance. Re-login on restart is acceptable UX trade for simpler implementation.

3. **[Security]** Should a viewer account with the same username as the master (VIEWER_USERNAME) be allowed?
   - Options: Block it (Recommended) | Allow it, DB takes priority | Allow it, master always wins
   - **Answer:** Block it
   - **Rationale:** Prevents auth ambiguity. Admin CRUD rejects creating viewer with master's username.

4. **[Operations]** When a viewer's allowed_chat_ids is updated by admin, should their active session be updated immediately or on next login?
   - Options: Immediate update (Recommended) | Next login only | Force logout on change
   - **Answer:** Immediate update
   - **Rationale:** Admin expects changes to take effect instantly. Update in-memory session entry on PUT.

5. **[Scope]** Should viewer accounts be able to access the transaction/accounting features, or is that master-only?
   - Options: Viewers can access transactions (Recommended) | Master-only feature | Configurable per viewer
   - **Answer:** Viewers can access transactions
   - **Rationale:** Transactions scoped to allowed chats like everything else — consistent access model.

6. **[UI/UX]** How should the admin chat picker work when assigning chats to a viewer?
   - Options: Show chat titles from DB (Recommended) | Just enter chat IDs manually | Both — visual picker + manual ID input
   - **Answer:** Show chat titles from DB
   - **Rationale:** Visual picker with titles/photos is much better UX. Uses GET /api/admin/chats endpoint.

7. **[Security]** Should the master be able to see which chats each viewer has accessed (basic audit log)?
   - Options: No audit log for now (Recommended) | Basic last-login timestamp | Full audit log
   - **Answer:** Full audit log
   - **Rationale:** User wants visibility into viewer activity. Requires new audit_log table and logging middleware.

#### Confirmed Decisions
- **Master access**: Always sees ALL chats — DISPLAY_CHAT_IDS ignored for master
- **Sessions**: In-memory, lost on restart — acceptable
- **Username collision**: Blocked — admin CRUD rejects master username
- **Permission updates**: Immediate — update in-memory session on admin PUT
- **Transactions**: Available to viewers, scoped to their allowed chats
- **Chat picker**: Visual with titles from DB
- **Audit log**: Full request logging per viewer — NEW SCOPE

#### Action Items
- [ ] Update Phase 1: Add `viewer_audit_log` table to DB schema
- [ ] Update Phase 2: Add audit logging middleware for viewer requests, add GET /api/admin/audit endpoint
- [ ] Update Phase 2: Master `_get_user_chat_ids` returns None (all chats), never uses display_chat_ids
- [ ] Update Phase 2: Admin CRUD rejects username matching VIEWER_USERNAME
- [ ] Update Phase 2: PUT /api/admin/viewers/{id} updates in-memory session immediately
- [ ] Update Phase 3: Add audit log tab in admin settings UI
- [ ] Update plan.md effort estimate (audit log adds ~1.5h)

#### Impact on Phases
- Phase 1: Add `ViewerAuditLog` model (viewer_id, endpoint, chat_id, timestamp, ip_address). Add migration.
- Phase 2: Add audit logging in `require_auth` or separate middleware. Master's `_get_user_chat_ids` returns None always. Block master username in create viewer. Immediate session update on PUT. Add `GET /api/admin/audit?viewer_id=X&limit=100` endpoint.
- Phase 3: Add "Activity Log" tab in settings panel showing audit entries per viewer.
- Phase 4: Add audit log tests.
