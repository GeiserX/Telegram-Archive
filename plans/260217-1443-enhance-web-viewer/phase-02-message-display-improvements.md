# Phase 2: Message Display Improvements

## Context Links

- [plan.md](plan.md)
- [Phase 1: Search Enhancement](phase-01-search-enhancement.md)
- Frontend: `src/web/templates/index.html` (lines 818-832 - reply/forward rendering)
- DB Adapter: `src/db/adapter.py` (lines 1116-1138 - reply text + reactions loading)
- Models: `src/db/models.py` (lines 63-113 - Message model with reply_to, forward_from)

## Overview

- **Priority:** MEDIUM
- **Status:** complete
- **Effort:** ~3h
- **Description:** Enhance reply-to previews, improve reactions display, add forward source info, and enable message deep linking.

## Key Insights

- **Reply-to**: Backend already fetches `reply_to_text` (truncated to 100 chars). Frontend shows it but lacks sender name of replied message.
- **Reactions**: Backend aggregates by emoji with count. Frontend renders them. Enhancement: show who reacted (tooltip with names).
- **Forward**: `forward_from_id` stored in DB. `raw_data.forward_from_name` available. Frontend shows basic "Forwarded from" but only ID or raw_data name.
- **Deep links**: No URL-based message navigation exists. Need `#chat={id}&msg={id}` hash routing.

## Requirements

### Functional
1. Reply preview shows sender name + media type indicator (not just text)
2. Reactions tooltip shows reactor names on hover
3. Forward indicator shows resolved sender name (lookup from users table)
4. Message deep links via URL hash: `#chat=123&msg=456`
5. Copy message link button (long-press/right-click context)

### Non-Functional
- Reply sender name lookup must not add N+1 queries (batch in existing query)
- Deep link loading < 2s for any message position
- Tooltip positioning avoids viewport overflow

## Architecture

### Backend Changes

**Extend reply-to data in `get_messages_paginated`** (`src/db/adapter.py`):
- When fetching `reply_to_text`, also fetch sender's `first_name` for the replied message
- Return `reply_to_sender_name` alongside `reply_to_text`
- Also fetch `reply_to_media_type` from media table for the replied message

**Resolve forward_from_id to name** (`src/db/adapter.py`):
- In message dict construction, if `forward_from_id` exists, look up User or Chat by that ID
- Return `forward_from_name` field (already partially done via raw_data)

**New endpoint for message deep link** (`src/web/main.py`):
```
GET /api/chats/{chat_id}/messages/{message_id}/context?limit=25
```
Returns messages surrounding the target message (25 before, 25 after) for context loading.

### Frontend Changes

- Enhanced reply block with sender name + media indicator icon
- Reactions hover tooltip with user names
- Forward block with resolved name
- URL hash routing: parse on load, update on chat/message navigation
- Copy link button in message hover actions

## Related Code Files

### Modify
- `src/db/adapter.py` - enrich reply-to data, add context endpoint query
- `src/web/main.py` - add message context endpoint
- `src/web/templates/index.html` - reply UI, reactions tooltip, forward display, hash routing

### Create
- None

## Implementation Steps

### Backend

1. **Enrich reply-to data** in `get_messages_paginated` (adapter.py):
   - After fetching `reply_to_text`, also query `Message.sender_id` for the reply target
   - Join to User to get `first_name` -> set `msg["reply_to_sender_name"]`
   - Query Media.type for reply target -> set `msg["reply_to_media_type"]`
   - Batch: collect all `reply_to_msg_id` values, do single query for all

2. **Resolve forward names** in `_message_to_dict` or post-processing:
   - If `forward_from_id` present and `raw_data.forward_from_name` missing
   - Look up in User table first, then Chat table as fallback
   - Set `msg["forward_from_name"]` field

3. **Add `/api/chats/{chat_id}/messages/{message_id}/context` endpoint** (main.py):
   - Query messages where `id <= message_id + 25` and `id >= message_id - 25` in same chat
   - Use date-based windowing: find target message date, get 25 messages before and after
   - Return same format as `get_messages`

### Frontend

4. **Enhanced reply preview block**:
   - Show `reply_to_sender_name` in bold blue text (replace generic "Reply to")
   - Add media type icon if `reply_to_media_type` exists (camera for photo, film for video, etc.)
   - Keep truncated text preview

5. **Reactions tooltip**:
   - On hover/tap of reaction pill, show tooltip with reactor names
   - Need to resolve `user_ids` to names - add lightweight endpoint or include in reaction data
   - Fallback: show user IDs if names not cached
   - Use CSS-only tooltip (no JS library) with `position: absolute`

6. **Forward display enhancement**:
   - Use `forward_from_name` from backend (resolved name)
   - Fallback chain: `forward_from_name` -> `raw_data.forward_from_name` -> `ID: {forward_from_id}`

7. **URL hash routing**:
   - On app mount: parse `window.location.hash` for `chat` and `msg` params
   - If found, auto-select chat and load message context
   - On chat select: update hash to `#chat={id}`
   - On search result click: update hash to `#chat={id}&msg={id}`
   - Use `hashchange` event listener for back/forward navigation

8. **Copy message link**:
   - Add share/link icon on message hover (desktop) or long-press menu (mobile)
   - Copy `{window.location.origin}/#chat={chatId}&msg={msgId}` to clipboard
   - Show brief toast "Link copied"

## Todo List

- [ ] Batch-fetch reply sender names in adapter
- [ ] Fetch reply media type indicator
- [ ] Resolve forward_from_id to name in adapter
- [ ] Add message context endpoint
- [ ] Update reply preview UI with sender name + media icon
- [ ] Add CSS-only reaction tooltip with user names
- [ ] Enhance forward display with resolved name
- [ ] Implement URL hash routing (parse + update)
- [ ] Add copy message link button
- [ ] Add toast notification for link copy
- [ ] Test deep link loading for messages at various positions
- [ ] Test reaction tooltip on mobile (tap instead of hover)

## Success Criteria

- Reply block shows "Alice" instead of "Reply to", plus media icon if applicable
- Hovering reaction shows "Alice, Bob" tooltip
- Forward shows resolved name from DB, not just raw ID
- Opening `/#chat=123&msg=456` navigates directly to that message
- Copy link button works and toast confirms

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| N+1 queries for reply sender names | High | Batch all reply_to_msg_ids into single query |
| Reaction user name resolution expensive | Medium | Include names in reaction aggregation query; cache user names |
| Hash routing conflicts with SPA | Low | Simple hash-based, no router lib needed |
| Tooltip overflow on mobile | Low | CSS `max-width` + viewport bounds check |

## Security Considerations

- Deep link must respect auth: if not authenticated, redirect to login first
- Message context endpoint must check `DISPLAY_CHAT_IDS`
- Clipboard API requires HTTPS (or localhost) - document this

## Next Steps

- Deep link support enables Phase 1 search result navigation
- Reaction tooltip user resolution can share cache with sender name display
