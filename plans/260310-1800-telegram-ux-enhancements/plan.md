# Telegram Archive Viewer — UX Enhancements Plan

**Date:** 2026-03-10
**Status:** Draft
**Scope:** 5 UX features inspired by Telegram Web

## Overview

Enhance the Telegram Archive Viewer with Telegram Web-inspired UX: username search visibility, custom context menu, user info panel, animated login character, and visual polish.

**Constraint:** Single-file Vue 3 SPA (~4850 lines). No build step. CDN-loaded deps only.

## Phases

| # | Phase | Priority | Effort | Backend | Status |
|---|-------|----------|--------|---------|--------|
| 1 | Username/Alias Search Display | High | Small | None | Pending |
| 2 | Custom Right-Click Context Menu | High | Medium | None | Pending |
| 3 | User Info Slide-In Panel | High | Medium | New endpoint | Pending |
| 4 | Animated Login Character (SVG) | Medium | Medium | None | Pending |
| 5 | Visual Polish & Transitions | Medium | Medium | Minor | Pending |

## Key Dependencies

- Phase 3 depends on a new `GET /api/users/{user_id}` endpoint + DB method
- Phase 5 (last message preview) depends on extending `get_all_chats` query
- Phases 1, 2, 4 are frontend-only — no backend changes
- Phase 2 context menu's "View user info" item links to Phase 3

## Architecture Decisions

1. **No external animation libraries** — SVG + CSS keyframes only for login character
2. **Teleport pattern** for overlays (context menu, user info panel) to avoid z-index/overflow issues
3. **Roboto font** replaces Inter (matching Telegram Web)
4. **Theme-aware patterns** via CSS variables — no per-theme duplication
5. **New endpoint** `GET /api/users/{user_id}?chat_id=X` returns user info + message count in context

## Files Modified

| File | Changes |
|------|---------|
| `src/web/templates/index.html` | All 5 phases (template, CSS, JS) |
| `src/web/main.py` | Phase 3 (new endpoint), Phase 5 (last_message_text) |
| `src/db/adapter.py` | Phase 3 (get_user_info method) |
| `tests/test_admin_settings.py` | Phase 3 (route existence test) |

## Risk Assessment

- **File size**: ~400-500 lines added → ~5300 total. Acceptable but approaching limit for future modularization.
- **Performance**: Context menu on 1000s of messages is fine (Vue event delegation). User info COUNT query uses existing `idx_messages_sender_id` index.
- **Visual quality**: SVG character must be simple/geometric — better minimal than ugly.

## Detailed Phase Files

- [Phase 1: Username Search Display](./phase-01-username-search-display.md)
- [Phase 2: Custom Context Menu](./phase-02-custom-context-menu.md)
- [Phase 3: User Info Panel](./phase-03-user-info-panel.md)
- [Phase 4: Login Character Animation](./phase-04-login-character-animation.md)
- [Phase 5: Visual Polish](./phase-05-visual-polish.md)
