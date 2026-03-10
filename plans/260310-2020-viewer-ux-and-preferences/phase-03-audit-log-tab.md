# Phase 3: Login Audit Log + Settings Tab

## Overview
- **Priority:** P2
- **Status:** Complete
- **Effort:** 2.5h

Fix broken audit log adapter, add login event logging, surface audit data in a new "Activity Log" tab in admin settings.

## Key Insights — Existing Bugs

The audit log adapter has field mismatches with the model:

1. **`create_audit_log()`** takes `viewer_id` param but `ViewerAuditLog` has no `viewer_id` column. It has: `username`, `role`, `action`, `endpoint`, `chat_id`, `ip_address`, `user_agent`, `created_at`.

2. **`get_audit_log()`** references `ViewerAuditLog.timestamp` but model uses `created_at`. Also references `ViewerAuditLog.viewer_id` which doesn't exist.

3. **Login events not logged** — the `/api/login` endpoint doesn't call `create_audit_log()` or `_log_viewer_audit()`.

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `src/db/adapter.py` | Modify | Fix `create_audit_log()` and `get_audit_log()` to match model fields |
| `src/web/main.py` | Modify | Add login/logout event logging, expose audit log UI endpoint |
| `src/web/templates/index.html` | Modify | Add "Activity Log" tab in settings panel |

## Implementation Steps

### Backend Fixes

1. **Fix `create_audit_log()` in adapter.py** (~line 1986)
   - Remove `viewer_id` param (doesn't exist on model)
   - Accept `role`, `action`, `ip_address`, `user_agent` params that match model columns
   ```python
   async def create_audit_log(
       self, username: str, role: str, action: str,
       endpoint: str | None = None, chat_id: int | None = None,
       ip_address: str | None = None, user_agent: str | None = None
   ):
       entry = ViewerAuditLog(
           username=username, role=role, action=action,
           endpoint=endpoint, chat_id=chat_id,
           ip_address=ip_address, user_agent=user_agent,
       )
   ```

2. **Fix `get_audit_log()` in adapter.py** (~line 2002)
   - Change `ViewerAuditLog.timestamp` → `ViewerAuditLog.created_at`
   - Remove `viewer_id` filter (use `username` instead)
   - Add `action` filter for filtering by event type
   ```python
   async def get_audit_log(self, username: str | None = None,
                           action: str | None = None,
                           limit: int = 100, offset: int = 0) -> list[dict]:
       query = select(ViewerAuditLog).order_by(ViewerAuditLog.created_at.desc())
       if username:
           query = query.where(ViewerAuditLog.username == username)
       if action:
           query = query.where(ViewerAuditLog.action == action)
   ```

3. **Fix `_log_viewer_audit()` call in main.py** (~line 480)
   - Update to pass correct params matching fixed adapter
   ```python
   async def _log_viewer_audit(request: Request, action: str = "api_access", chat_id: int | None = None):
       user = request.state.user
       await db.create_audit_log(
           username=user["username"], role=user["role"], action=action,
           endpoint=str(request.url.path), chat_id=chat_id,
           ip_address=request.client.host if request.client else None,
           user_agent=request.headers.get("user-agent"),
       )
   ```

4. **Add login event logging** in login handler (`main.py:569`)
   - After successful login (before return): log "login_success" event
   - After failed login (before raise): log "login_failed" event
   ```python
   # After successful login:
   if db:
       await db.create_audit_log(
           username=username, role=user_info["role"], action="login_success",
           endpoint="/api/login",
           ip_address=request.client.host if request.client else None,
           user_agent=request.headers.get("user-agent"),
       )

   # After failed login:
   if db:
       await db.create_audit_log(
           username=username or "(empty)", role="unknown", action="login_failed",
           endpoint="/api/login",
           ip_address=request.client.host if request.client else None,
           user_agent=request.headers.get("user-agent"),
       )
   ```

5. **Add token auth logging** — when a share token is used to authenticate, log it too.

6. **Update `/api/admin/audit` endpoint** — fix params to match new adapter signature (use `username` not `viewer_id`).

### Frontend

7. **Add "Activity Log" tab** in admin settings panel
   - New tab alongside existing tabs (Configuration, Users, Tokens, etc.)
   - Table showing: timestamp, username, role, action, IP address, user agent
   - Filter by: action type (login_success, login_failed, api_access), username
   - Paginated with "Load more" button
   - Color code: green for success, red for failed, gray for access

8. **Vue state additions**
   ```javascript
   const auditEntries = ref([])
   const auditLoading = ref(false)
   const auditFilter = ref({ action: '', username: '' })
   const auditOffset = ref(0)
   const auditHasMore = ref(true)
   ```

9. **loadAuditLog() function**
   ```javascript
   async function loadAuditLog(append = false) {
       auditLoading.value = true
       const params = new URLSearchParams({ limit: '50', offset: auditOffset.value })
       if (auditFilter.value.action) params.set('action', auditFilter.value.action)
       if (auditFilter.value.username) params.set('username', auditFilter.value.username)
       const res = await fetch(`/api/admin/audit?${params}`, { credentials: 'include' })
       if (res.ok) {
           const data = await res.json()
           if (append) auditEntries.value.push(...data.entries)
           else auditEntries.value = data.entries
           auditHasMore.value = data.entries.length === 50
       }
       auditLoading.value = false
   }
   ```

## Todo List

- [ ] Fix `create_audit_log()` adapter — remove `viewer_id`, use correct model fields
- [ ] Fix `get_audit_log()` adapter — `.timestamp` → `.created_at`, add `username`/`action` filter
- [ ] Fix `_log_viewer_audit()` in main.py — match fixed adapter signature
- [ ] Add login success logging in `/api/login`
- [ ] Add login failure logging in `/api/login`
- [ ] Add token auth event logging
- [ ] Fix `/api/admin/audit` endpoint params
- [ ] Add "Activity Log" tab UI in settings
- [ ] Add table with columns: time, user, role, action, IP, user agent
- [ ] Add filter dropdowns (action type, username)
- [ ] Add pagination / "Load more"
- [ ] Color-code rows by action type

## Success Criteria

- Login events (success + failure) logged with IP and user agent
- Audit log tab shows all events in reverse chronological order
- Filterable by action type and username
- No adapter crashes from field mismatches
