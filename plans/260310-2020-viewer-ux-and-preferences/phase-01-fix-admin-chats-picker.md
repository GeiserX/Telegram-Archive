# Phase 1: Fix Admin Chats API + Chat Picker Display

## Overview
- **Priority:** P1
- **Status:** Complete
- **Effort:** 1h

The `/api/admin/chats` endpoint strips `username`, `first_name`, `last_name` from response. Frontend chat pickers reference these fields but they're always undefined. Fix the API and update picker display to show "User alias : @username" format.

## Root Cause

`src/web/main.py:1496-1500` — endpoint maps only `id`, `title`, `type`:
```python
{
    "id": c["id"],
    "title": c.get("title") or c.get("first_name") or str(c["id"]),
    "type": c.get("type", ""),
}
```

But `get_all_chats()` returns full Chat model including `username`, `first_name`, `last_name`.

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `src/web/main.py` | Modify | Add `username`, `first_name`, `last_name` to `/api/admin/chats` response |
| `src/web/templates/index.html` | Modify | Update chat picker item layout: "Title : @username" format |

## Implementation Steps

1. **Fix `/api/admin/chats` response** (`main.py:1493-1503`)
   - Add `username`, `first_name`, `last_name` fields to response mapping
   ```python
   {
       "id": c["id"],
       "title": c.get("title") or c.get("first_name") or str(c["id"]),
       "type": c.get("type", ""),
       "username": c.get("username"),
       "first_name": c.get("first_name"),
       "last_name": c.get("last_name"),
   }
   ```

2. **Update chat picker display** in all 4 chat pickers (create viewer, edit viewer, create token, edit token)
   - Current: `{{ chat.title }}` + small `@username` below
   - New: `{{ chat.title }}  :  @{{ chat.username }}` on same line, with `@username` in muted color
   - Format for items with no username: just show title
   - Format for selected pills: `Title (@username)` for disambiguation

## Todo List

- [ ] Add `username`, `first_name`, `last_name` to `/api/admin/chats` response
- [ ] Update chat picker list item layout to "Title : @username"
- [ ] Update selected pill layout for disambiguation
- [ ] Verify search filtering still works with @ prefix stripping

## Success Criteria

- Chat picker shows `Chat Title  :  @username` for chats that have usernames
- Private chats show `First Last  :  @username`
- Selected pills show `Title (@username)` to disambiguate same-name entries
- Search by @username still works
