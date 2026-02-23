# Phase 1: Search Enhancement

## Context Links

- [plan.md](plan.md)
- [Research: Modern Chat UI](research/researcher-01-modern-chat-ui.md)
- Backend: `src/web/main.py` (lines 588-637 - get_messages endpoint)
- DB Adapter: `src/db/adapter.py` (lines 1005-1140 - get_messages_paginated)
- Frontend: `src/web/templates/index.html` (search UI ~line 371, ~line 713)

## Overview

- **Priority:** HIGH
- **Status:** complete
- **Effort:** ~3h
- **Description:** Add advanced search filters, result highlighting, and global cross-chat search.

## Key Insights

- Current search: basic `ILIKE` on `Message.text` within a single chat
- Backend already supports `search` param on `get_messages_paginated` - need to add `sender_id`, `media_type`, `date_from`/`date_to` params
- DB has indexes on `sender_id`, `date`, `media.type` - filters will be performant
- Global search needs new endpoint querying across all chats (respect `DISPLAY_CHAT_IDS`)
- SQLite FTS5 not assumed; use `LIKE` with existing indexes (sufficient for archive sizes)

## Requirements

### Functional
1. Filter messages by sender name/ID within a chat
2. Filter messages by date range (from/to)
3. Filter messages by media type (photo, video, document, audio)
4. Highlight search terms in message text with `<mark>` tags
5. Global cross-chat search returning results grouped by chat
6. Click search result to jump to message in context (load surrounding messages)

### Non-Functional
- Search response < 500ms for typical queries
- Filter UI collapsible to not clutter mobile view
- Maintain existing per-chat search behavior as default

## Architecture

### Backend Changes (`src/web/main.py`)

New endpoint:
```
GET /api/search?q=text&sender=name&media_type=photo&date_from=2025-01-01&date_to=2025-12-31&limit=50&offset=0
```
Returns: `{ results: [{chat_id, chat_title, message_id, text_snippet, sender_name, date, media_type}], total }`

Modified endpoint:
```
GET /api/chats/{id}/messages?search=text&sender_id=123&media_type=photo&date_from=...&date_to=...
```

### DB Adapter Changes (`src/db/adapter.py`)

- Extend `get_messages_paginated()` with optional `sender_id`, `media_type`, `date_from`, `date_to` params
- New method `search_messages_global()` querying across chats with same filters

### Frontend Changes (`src/web/templates/index.html`)

- Filter bar below chat search input (collapsible)
- Filter chips: sender dropdown, date range picker (reuse Flatpickr), media type buttons
- `highlightSearchText()` computed method wrapping matches in `<mark>`
- Global search mode toggle in sidebar search
- Search results panel showing matches grouped by chat

## Related Code Files

### Modify
- `src/web/main.py` - add global search endpoint, extend messages endpoint params
- `src/db/adapter.py` - extend `get_messages_paginated`, add `search_messages_global`
- `src/web/templates/index.html` - filter UI, highlighting, global search panel

### Create
- None

## Implementation Steps

### Backend (main.py + adapter.py)

1. **Extend `get_messages_paginated` signature** in `src/db/adapter.py`:
   - Add params: `sender_id: int | None`, `media_type: str | None`, `date_from: datetime | None`, `date_to: datetime | None`
   - Add `WHERE` clauses: `Message.sender_id == sender_id`, `Media.type == media_type`, `Message.date >= date_from`, `Message.date <= date_to`

2. **Add `search_messages_global` method** in `src/db/adapter.py`:
   - Query across all chats (or filtered by `display_chat_ids`)
   - Join Message + User + Media + Chat (for chat title)
   - Return list of dicts with: `chat_id`, `chat_title`, `message_id`, `text` (truncated snippet), `sender_name`, `date`, `media_type`
   - Apply same filters (sender, media_type, date range)
   - Limit 50, offset-based pagination

3. **Add `/api/search` endpoint** in `src/web/main.py`:
   - Accept query params: `q`, `sender`, `media_type`, `date_from`, `date_to`, `limit`, `offset`
   - Call `db.search_messages_global()`
   - Respect `config.display_chat_ids`

4. **Extend `/api/chats/{id}/messages` endpoint** in `src/web/main.py`:
   - Add `sender_id`, `media_type`, `date_from`, `date_to` query params
   - Pass through to `get_messages_paginated`

### Frontend (index.html)

5. **Add search filter UI** below message search input:
   - Collapsible filter bar with toggle button (filter icon)
   - Sender input (text, matches against first_name/username)
   - Date range: two Flatpickr inputs (from/to)
   - Media type: button group (All | Photos | Videos | Docs | Audio)
   - Apply/Clear buttons

6. **Implement search highlighting**:
   - New method `highlightText(text, query)` - escape HTML, wrap matches in `<mark class="bg-yellow-400/30 text-inherit rounded">`
   - Modify `linkifyText()` call to chain with highlight when `messageSearchQuery` is active
   - Handle regex-special characters in search query

7. **Add global search mode**:
   - Toggle in sidebar: "Search all chats" checkbox/button
   - When active, sidebar search calls `/api/search` instead of `/api/chats`
   - Results panel replaces chat list showing: chat avatar + title, message snippet, date
   - Click result: select chat, then jump to message by loading messages around that ID

8. **Jump-to-message from search result**:
   - New method `jumpToSearchResult(chatId, messageId)`
   - Select chat, load messages with `before_id` cursor centered on target message
   - Scroll to and highlight target message briefly (CSS animation)

## Todo List

- [ ] Extend `get_messages_paginated` with filter params
- [ ] Add `search_messages_global` method to adapter
- [ ] Add `/api/search` endpoint
- [ ] Extend `/api/chats/{id}/messages` with filter params
- [ ] Build collapsible filter bar UI
- [ ] Implement `highlightText` method
- [ ] Chain highlighting with `linkifyText`
- [ ] Add global search toggle in sidebar
- [ ] Build search results panel (grouped by chat)
- [ ] Implement `jumpToSearchResult` navigation
- [ ] Test with large chat histories (1000+ messages)
- [ ] Test mobile responsive filter bar

## Success Criteria

- Can filter messages by sender, date range, and media type within a chat
- Search terms highlighted in yellow in message bubbles
- Global search returns results from all accessible chats
- Clicking a global search result navigates to the exact message
- Filter bar collapses cleanly on mobile (<768px)
- No performance regression on message loading

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Slow global search on large DBs | Medium | Add LIMIT, use existing indexes, paginate results |
| Search highlighting breaks HTML links | Medium | Apply highlight AFTER linkify, use text nodes only |
| Filter UI clutters mobile | Low | Collapsible filter bar, hidden by default |
| `LIKE` query slow without FTS | Low | Existing `idx_messages_chat_date_desc` helps; archive DBs typically <1M messages |

## Security Considerations

- Global search must respect `DISPLAY_CHAT_IDS` restriction
- Sanitize search input to prevent SQL injection (SQLAlchemy parameterized queries handle this)
- Rate-limit `/api/search` to prevent abuse (same as other endpoints)
- Search highlight must escape HTML to prevent XSS

## Next Steps

- Phase 2 can build on search highlighting for reply-to text display
- Global search results UI can be reused for media gallery browsing (Phase 4)
