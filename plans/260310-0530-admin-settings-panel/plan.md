# Admin Settings Panel — Implementation Plan

**Date:** 2026-03-10
**Feature:** Admin settings UI with account, user/token management, themes, timezone, backup control
**Branch:** `feat/web-viewer-enhancements` (`repo/dev/`)
**Version:** v7.3.0

---

## Summary

Add a full admin settings panel to the viewer. Admin (master role) gets a gear icon in the header opening a multi-tab settings modal. Covers: password change, user/token management with chat-scope pickers, CSS-variable-based theme presets, browser-auto timezone, and backup schedule control via a shared DB settings table.

---

## Phases

| # | Phase | Status | Effort | Files Modified |
|---|-------|--------|--------|----------------|
| 1 | [Settings Panel Shell & Theme System](phase-01-settings-panel-and-themes.md) | TODO | Medium | `index.html` |
| 2 | [Account & Password Management](phase-02-account-and-password.md) | TODO | Small | `main.py`, `index.html` |
| 3 | [User & Token Management UI](phase-03-user-and-token-management-ui.md) | TODO | Large | `index.html` |
| 4 | [Timezone Auto-detect & Display](phase-04-timezone-auto-detect.md) | TODO | Small | `index.html`, `main.py` |
| 5 | [App Settings Model & Backup Control](phase-05-app-settings-and-backup-control.md) | TODO | Large | `models.py`, `adapter.py`, `main.py`, `scheduler.py`, `index.html`, migration |
| 6 | [Testing](phase-06-testing.md) | TODO | Medium | `tests/test_admin_settings.py` |

---

## Dependencies

- Phase 1 first (shell needed for all tabs)
- Phases 2, 3, 4 can run in parallel (independent tabs, different endpoints)
- Phase 5 depends on Phase 1 (needs settings tab UI)
- Phase 6 depends on all prior

---

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Settings panel type | Full-screen modal with tabs | Matches existing modal patterns (date picker, lightbox). Full-screen for enough room. |
| Theme system | CSS variables + Tailwind `var()` refs | Swap `--tg-*` variables per theme. No Tailwind rebuild needed. localStorage persistence. |
| Theme presets | 6 presets: Midnight, Dark, Nord, Solarized, OLED, Light | Cover most preferences. Modern, proven palettes. |
| Timezone approach | Browser `Intl` auto-detect → localStorage override | No DB change needed. Per-browser, not per-account. Server timezone stays for stats calc. |
| Backup control | `app_settings` DB table polled by backup container | Both containers share DB. Backup polls every 60s. Simplest cross-container communication. |
| Active-user backup | Viewer writes `active_viewers` count to DB, backup checks | If active_viewers > 0, backup runs every 5 min instead of cron schedule. |
| Self password change | New `PUT /api/auth/password` endpoint | Any authenticated user can change their own password. Master changes env-var-derived password via DB override. |
| Chat picker widget | Multi-select with search, uses `/api/admin/chats` | Reusable for both viewer account creation and token creation. |

---

## Theme Presets

| Theme | Background | Sidebar | Hover | Active | Accent | Text |
|-------|-----------|---------|-------|--------|--------|------|
| Midnight (current) | `#0f172a` | `#1e293b` | `#334155` | `#2b5278` | `#3b82f6` | `#e2e8f0` |
| Dark | `#111827` | `#1f2937` | `#374151` | `#4b5563` | `#6366f1` | `#f3f4f6` |
| Nord | `#2e3440` | `#3b4252` | `#434c5e` | `#5e81ac` | `#88c0d0` | `#eceff4` |
| Solarized | `#002b36` | `#073642` | `#586e75` | `#268bd2` | `#2aa198` | `#eee8d5` |
| OLED | `#000000` | `#0a0a0a` | `#1a1a1a` | `#1d4ed8` | `#3b82f6` | `#e5e5e5` |
| Light | `#f8fafc` | `#ffffff` | `#f1f5f9` | `#dbeafe` | `#2563eb` | `#1e293b` |

---

## Cross-Container Communication (Backup Control)

```
Viewer (settings UI)              Shared SQLite DB              Backup (scheduler.py)
─────────────────────            ──────────────────            ────────────────────
User changes schedule  ──────►  app_settings table  ◄──────  Polls every 60s
  POST /api/admin/settings         key: backup_schedule          Reads schedule value
                                   key: active_viewers           Reschedules if changed
                                   key: last_backup_at

User actively browsing ──────►  active_viewers = 1  ◄──────  If active_viewers > 0
  WebSocket heartbeat              (TTL: 2 min)                  → run backup every 5 min
  POST /api/activity/ping                                      If active_viewers == 0
                                                                 → use normal cron schedule
```

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Settings panel too large for single HTML file | Medium | Keep components minimal. CSS variables for themes = small footprint. |
| Backup container doesn't read settings | High | Phase 5 adds polling loop. Test with both containers. |
| Theme flash on page load | Low | Read localStorage before Vue mount, apply class in `<head>` script. |
| Master password stored in env AND DB | Medium | DB override takes precedence. Clear documentation. |
| SQLite lock contention from settings polling | Low | Polling is read-only SELECT every 60s. Minimal impact. |

---

## Success Criteria

- [ ] Gear icon visible only to master/admin users
- [ ] Settings modal with tabbed navigation (Account, Users, Tokens, Appearance, General)
- [ ] Admin can change own password
- [ ] Admin can CRUD viewer accounts with chat scope picker
- [ ] Admin can CRUD share tokens with chat scope picker
- [ ] 6 theme presets work and persist across page reloads
- [ ] Timezone auto-detects from browser, manual override available
- [ ] Backup schedule configurable from UI (cron expression or interval picker)
- [ ] Active-user mode triggers 5-min backup intervals
- [ ] All existing auth flows unaffected
