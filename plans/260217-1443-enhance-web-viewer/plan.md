---
title: "Enhance Telegram Archive Web Viewer"
description: "Improve search, message display, performance, media gallery, and add accounting/transaction view"
status: complete
priority: P2
effort: 14h
branch: viewer-enhancement
tags: [web-viewer, ui-enhancement, search, performance, accounting]
created: 2026-02-17
completed: 2026-02-17
---

# Enhance Telegram Archive Web Viewer

## Summary

Improve the web viewer across five areas: advanced search, message display enrichment, rendering performance, media browsing, and a new accounting/transaction view for tracking credit/debit across chat conversations. Split into Vue components for maintainability.

## Phases

| # | Phase | Priority | Effort | Status |
|---|-------|----------|--------|--------|
| 1 | [Search Enhancement](phase-01-search-enhancement.md) | HIGH | ~3h | complete |
| 2 | [Message Display Improvements](phase-02-message-display-improvements.md) | MEDIUM | ~3h | complete |
| 3 | [Performance & UX](phase-03-performance-and-ux.md) | HIGH | ~2h | complete |
| 4 | [Media Gallery](phase-04-media-gallery-and-theme.md) | LOW | ~2h | complete |
| 5 | [Accounting / Transaction View](phase-05-accounting-transaction-view.md) | HIGH | ~4h | complete |

## Key Dependencies

- **Backend**: `src/web/main.py` (1084 lines) - new API endpoints for global search, sender filter, media, transactions
- **Frontend**: `src/web/templates/index.html` (3284 lines) → split into Vue components
- **DB Adapter**: `src/db/adapter.py` - new query methods for filtered search, transactions
- **DB Models**: `src/db/models.py` - new `transactions` table for accounting feature

## Architecture Decisions

1. **No build step** - keep Vue 3 CDN, Tailwind CDN setup
2. **Split into components** - extract new features as separate `.js` files loaded via `<script>`, diverging from upstream for maintainability
3. **Virtual scroll** - pure Vue computed-range approach (no external lib)
4. **Search highlighting** - client-side `<mark>` tag injection via computed
5. **Dark theme only** - skip light theme, avoid risky CSS variable migration
6. **Global search** - new backend endpoint querying across all chats with LIKE
7. **Accounting** - new `transactions` table in SQLite, pattern detection for amounts with manual override
8. **Git branches** - viewer enhancements on `viewer-enhancement` branch, accounting on `branch-als-accounting`

## Constraints

- Python 3.13 compat (`from __future__ import annotations`)
- SQLite primary DB (no FTS5 extension assumed; use LIKE fallback)
- Push to user's own repo (not upstream GeiserX)
- Keep existing auth system intact

## Research

- [Modern Chat UI Patterns](research/researcher-01-modern-chat-ui.md)

## Validation Log

### Session 1 — 2026-02-17
**Trigger:** Initial plan creation validation
**Questions asked:** 7

#### Questions & Answers

1. **[Scope]** The plan includes 4 phases totaling ~10h. Which phases do you actually want implemented?
   - Options: Phase 1: Search | Phase 2: Message Display | Phase 3: Performance | Phase 4: Gallery & Theme
   - **Answer:** All four phases + new Phase 5 (Accounting/Transaction View)
   - **Custom input:** "can we add spreadsheet like 2 cells on the left of the messages, basically these chat reflect transaction history and record that need to be verified where it is credit and debit between me and one party, (optional) specific feature --branch-Al's Accounting (the whole revamp on viewer can be pushed to my own repo under new branch --viewer-enhancement)"
   - **Rationale:** All phases confirmed. New Phase 5 is the most business-critical — chats represent financial transactions that need credit/debit tracking. Separate branches requested.

2. **[Architecture]** Phase 3 proposes custom virtual scrolling. Is scroll performance currently a problem?
   - Options: Skip | Yes, implement | Simple DOM limit
   - **Answer:** Yes, implement it
   - **Rationale:** With 92K messages growing, virtual scroll prevents future perf issues.

3. **[Architecture]** The 3,284-line single HTML template will grow with these features. How should we handle this?
   - Options: Keep single file | Split into components | Split only new code
   - **Answer:** Split into components
   - **Rationale:** Diverging from upstream is acceptable since pushing to own repo. Maintainability > upstream compatibility.

4. **[Tradeoff]** Light theme requires migrating ~50 hardcoded color values to CSS variables. Worth the risk?
   - Options: Skip light theme | Implement | Accent color picker
   - **Answer:** Skip light theme
   - **Rationale:** Dark-only is fine for private archive viewer. Avoids high-risk CSS migration.

5. **[Scope]** For the accounting/transaction view: how should credit/debit be determined?
   - Options: Manual tagging | Pattern detection | Keyword rules
   - **Answer:** Pattern detection
   - **Rationale:** Auto-detect amounts from message text (e.g., "sent 500", "received 1000") with manual override capability.

6. **[Scope]** For the accounting spreadsheet view: what columns?
   - Options: Basic (Date, Message, Credit, Debit, Balance) | Detailed (+ Sender, Category, Notes) | Minimal (Credit/Debit only)
   - **Answer:** Detailed: Date, Sender, Message, Category, Credit, Debit, Balance, Notes
   - **Rationale:** Full accounting with categorization and notes needed for proper financial tracking.

7. **[Architecture]** Should accounting data be stored in existing SQLite DB or exported separately?
   - Options: New DB table | LocalStorage | Separate CSV/Excel export
   - **Answer:** New DB table
   - **Rationale:** Persistent, queryable, syncs with backup. Links to message_id.

#### Confirmed Decisions
- All 4 original phases: proceed as planned
- Phase 5 (Accounting): new feature with pattern detection + new DB table
- File structure: split Vue components into separate .js files
- Theme: dark-only, skip light theme migration
- Branches: `viewer-enhancement` for viewer, `branch-als-accounting` for accounting
- Push to own repo, not upstream

#### Action Items
- [ ] Create Phase 5 plan file for accounting/transaction view
- [ ] Update Phase 4 to remove light theme, keep media gallery only
- [ ] Update Phase 3 to note component splitting strategy
- [ ] Create `viewer-enhancement` branch before implementation

#### Impact on Phases
- Phase 3: Add component extraction strategy (splitting index.html)
- Phase 4: Remove light theme, rename to "Media Gallery" only. Reduce effort to ~1.5h.
- Phase 5 (NEW): Accounting/Transaction View — pattern detection, new DB table, spreadsheet UI. ~4h effort.
