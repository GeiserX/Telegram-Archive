---
title: "Viewer UX & Preferences System"
description: "Chat picker fix, media download restriction, audit log tab, infinite scroll, per-chat backgrounds"
status: complete
priority: P1
effort: 14h
branch: feat/web-viewer-enhancements
tags: [frontend, backend, database, auth, ux]
created: 2026-03-10
completed: 2026-03-10
---

# Viewer UX & Preferences System

## Overview

Six interconnected features: admin chat picker fix, per-token/viewer media download restriction, login audit log tab, infinite scroll overhaul, and per-chat background preferences with separate storage.

## Phases

| # | Phase | Status | Effort | Priority | Link |
|---|-------|--------|--------|----------|------|
| 1 | Fix admin chats API + picker display | Complete | 1h | P1 | [phase-01](./phase-01-fix-admin-chats-picker.md) |
| 2 | Media download restriction | Complete | 3h | P1 | [phase-02](./phase-02-media-download-restriction.md) |
| 3 | Login audit log + settings tab | Complete | 2.5h | P2 | [phase-03](./phase-03-audit-log-tab.md) |
| 4 | Infinite scroll overhaul | Complete | 2h | P1 | [phase-04](./phase-04-infinite-scroll-fix.md) |
| 5 | Per-chat background preferences | Complete | 4h | P2 | [phase-05](./phase-05-per-chat-backgrounds.md) |
| 6 | Tests | Complete | 1.5h | P2 | [phase-06](./phase-06-tests.md) |

## Dependencies

```
Phase 1 (no deps) ─────────────────────────────┐
Phase 2 (no deps) ──────────────────────────────┤
Phase 3 (no deps) ──────────────────────────────├─→ Phase 6
Phase 4 (no deps) ──────────────────────────────┤
Phase 5 (no deps, but largest scope) ──────────┘
```

Phases 1-5 are independent (different files/features). Phase 6 depends on all.

## Architecture Decisions

1. **Preferences storage**: Use localStorage (not separate DB) for per-chat backgrounds. Reasons:
   - Viewer tokens are ephemeral — no persistent user identity to key against
   - No container restart needed for preference changes
   - Zero backend complexity
   - Each browser/device gets own preferences (expected behavior)
   - Falls back gracefully if cleared

2. **Media download restriction**: Add `no_download` column to both `ViewerAccount` and `ViewerToken` tables. Frontend enforces via CSS `pointer-events: none` on media + disabled context menu download option. Not a security boundary — determined users can still use DevTools.

3. **Audit log**: Fix existing broken adapter, add login event logging to `/api/login` endpoint. Surface in a new "Activity Log" settings tab.

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Audit log adapter bugs crash queries | High | Fix model field mismatches first |
| Infinite scroll breaks existing pagination | High | Keep sentinel as fallback, add scroll listener |
| Per-chat backgrounds bloat localStorage | Low | Limit to theme name per chat, not custom images |
| Download restriction bypassable via DevTools | Low | Acceptable — it's a deterrent, not DRM |
