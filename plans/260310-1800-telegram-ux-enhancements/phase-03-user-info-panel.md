# Phase 3: User Info Slide-In Panel

**Priority:** High | **Effort:** Medium | **Backend:** New endpoint + DB method

## Context

- Messages carry `sender_id`, `first_name`, `last_name`, `username` from User table JOIN
- No dedicated `/api/users/{id}` endpoint exists
- No user info panel/popup exists
- Avatars stored at `{media_path}/avatars/users/{id}_*.jpg`

## Requirements

- Click sender name or avatar in message → slide-in panel from right
- Panel shows: profile photo, display name, @username, user ID, phone (admin only), bot status, message count in current chat
- Panel has dark backdrop overlay, closes on backdrop click or X button
- Panel slides in/out with CSS transition (0.3s ease)
- "View in Telegram" link: `https://t.me/{username}` if username exists

## Architecture

### New API Endpoint: `GET /api/users/{user_id}`

Query params: `chat_id` (optional, for context-specific message count)

Response:
```json
{
  "id": 123456,
  "first_name": "John",
  "last_name": "Doe",
  "username": "johndoe",
  "phone": "hidden",
  "is_bot": false,
  "avatar_url": "/media/avatars/users/123456_xxx.jpg",
  "message_count": 42,
  "first_seen": "2024-01-15T10:30:00",
  "last_seen": "2026-03-09T14:20:00"
}
```

- `phone` returned only for admin sessions (check auth level)
- `message_count` scoped to `chat_id` if provided, otherwise total
- `first_seen` / `last_seen` = min/max `date` from messages WHERE `sender_id = user_id`

### New DB Method: `get_user_info(user_id, chat_id=None)`

```python
async def get_user_info(self, user_id: int, chat_id: int = None) -> dict | None:
    async with self.db_manager.get_session() as session:
        # Get user record
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return None

        # Message count + first/last seen
        count_q = select(func.count(), func.min(Message.date), func.max(Message.date)).where(
            Message.sender_id == user_id
        )
        if chat_id:
            count_q = count_q.where(Message.chat_id == chat_id)

        stats = (await session.execute(count_q)).one()

        return {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "username": user.username,
            "phone": user.phone,
            "is_bot": bool(user.is_bot),
            "message_count": stats[0],
            "first_seen": stats[1].isoformat() if stats[1] else None,
            "last_seen": stats[2].isoformat() if stats[2] else None,
        }
```

## Related Code Files

- `src/db/adapter.py` — add `get_user_info()` method
- `src/web/main.py` — add `GET /api/users/{user_id}` endpoint
- `src/web/templates/index.html` — slide-in panel template + JS + CSS
- `tests/test_admin_settings.py` — add route existence test

## Implementation Steps

### Backend (adapter.py + main.py)

1. Add `get_user_info(user_id, chat_id)` to `DatabaseAdapter`
2. Add endpoint in main.py:
   ```python
   @app.get("/api/users/{user_id}", dependencies=[Depends(require_auth)])
   async def get_user_info_endpoint(user_id: int, request: Request):
       chat_id = request.query_params.get("chat_id")
       info = await db.get_user_info(user_id, int(chat_id) if chat_id else None)
       if not info:
           raise HTTPException(404, "User not found")
       # Hide phone for non-admin
       if not _is_admin_session(request):
           info["phone"] = None
       # Resolve avatar
       info["avatar_url"] = _find_user_avatar(user_id)
       return info
   ```

3. Add `_find_user_avatar(user_id)` helper — look up `avatars/users/{user_id}_*.jpg`

### Frontend (index.html)

4. Vue state:
   ```javascript
   const userInfoPanel = ref({ visible: false, loading: false, user: null })
   ```

5. Method `openUserInfo(senderId)`:
   - Set loading state
   - Fetch `/api/users/{senderId}?chat_id={currentChat.id}`
   - Set user data, show panel

6. Make sender names clickable:
   ```html
   <span class="sender-name cursor-pointer hover:underline"
         @click="openUserInfo(msg.sender_id)">
     {{ getSenderName(msg) }}
   </span>
   ```

7. Slide-in panel template (Teleport to body):
   ```html
   <div class="user-info-backdrop" @click="userInfoPanel.visible = false">
     <div class="user-info-panel" @click.stop>
       <div class="user-info-header">
         <img :src="userInfoPanel.user.avatar_url" />
         <h3>{{ userInfoPanel.user.first_name }} {{ userInfoPanel.user.last_name }}</h3>
         <span>@{{ userInfoPanel.user.username }}</span>
       </div>
       <div class="user-info-stats">
         <div>Messages: {{ userInfoPanel.user.message_count }}</div>
         <div>First seen: {{ formatDate(userInfoPanel.user.first_seen) }}</div>
         <div>Last seen: {{ formatDate(userInfoPanel.user.last_seen) }}</div>
       </div>
       <a v-if="userInfoPanel.user.username"
          :href="'https://t.me/' + userInfoPanel.user.username" target="_blank">
         View in Telegram
       </a>
     </div>
   </div>
   ```

8. CSS: slide-in from right with backdrop

### Test

9. Add to TestEndpointRoutes:
   ```python
   def test_user_info_route_exists(self):
       assert ("/api/users/{user_id}", "GET") in self.route_methods
   ```

## Success Criteria

- [ ] Click sender name → panel slides in from right
- [ ] Panel shows: photo, name, username, message count, first/last seen
- [ ] Phone visible for admin only
- [ ] "View in Telegram" link works
- [ ] Panel closes on backdrop click or X button
- [ ] Loading state shown while fetching
- [ ] Works across all 6 themes

## Risk

- **Avatar resolution**: User avatars may not exist for all users. Fallback to initials/placeholder.
- **Performance**: COUNT query on messages table. Indexed on `sender_id` so should be fast. Test with large chats (100k+ messages).
