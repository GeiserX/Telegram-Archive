# Phase 1: Username/Alias Search Display

**Priority:** High | **Effort:** Small | **Backend:** None

## Context

- Backend already searches `Chat.title`, `Chat.first_name`, `Chat.last_name`, AND `Chat.username` via `ilike`
- API already returns `username` field per chat
- Users just can't see usernames, so they don't know they can search by them

## Key Insight

This is purely a frontend display change. Zero backend work.

## Requirements

- Show `@username` in chat list items (subtitle line next to type)
- Username should be styled as muted/secondary text
- When searching, highlight matching text in both title and username

## Related Code Files

- `src/web/templates/index.html` lines 827-845 (chat list item template)
- `src/web/templates/index.html` line 2302 (`onSearchInput` method)

## Implementation Steps

1. In the chat list item template (after the chat type span), add:
   ```html
   <span v-if="chat.username" class="text-xs opacity-60 ml-1">@{{ chat.username }}</span>
   ```

2. For private chats (type=user), show display name + @username in the subtitle:
   ```html
   <span v-if="chat.type === 'user' && chat.username" class="text-xs opacity-60">
     @{{ chat.username }}
   </span>
   ```

3. Test with search — verify typing `@username` filters correctly (should already work since backend handles it)

## Success Criteria

- [x] Usernames visible in chat list items
- [x] Search by @username works and shows matching chats
- [x] Existing chat title search still works
- [x] Visual hierarchy preserved (username is secondary to title)

## Risk: None

This is the simplest change in the entire plan.
