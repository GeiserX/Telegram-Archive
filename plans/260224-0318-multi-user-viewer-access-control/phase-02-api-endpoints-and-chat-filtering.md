# Phase 2: API Endpoints & Chat Filtering

## Context Links
- [Phase 1: DB Schema & Auth Backend](phase-01-db-schema-and-auth-backend.md)
- [Current chat filtering: src/web/main.py](../../src/web/main.py) — `config.display_chat_ids` used in 15+ locations
- [DB adapter: src/db/adapter.py](../../src/db/adapter.py)

## Overview
- **Priority:** P1 (depends on Phase 1)
- **Status:** complete
- **Effort:** 2h

Add admin-only CRUD endpoints for viewer account management. Refactor all existing endpoints to use per-user chat filtering instead of the global `config.display_chat_ids`.

## Key Insights
- `config.display_chat_ids` is checked in ~15 places across main.py. Each check follows pattern: `if config.display_chat_ids and chat_id not in config.display_chat_ids: raise 403`.
- For master user: `allowed_chat_ids` is None (sees everything). If `config.display_chat_ids` is set AND no viewer accounts exist, master should still respect it for backward compat.
- For viewer user: `allowed_chat_ids` is a set of chat IDs from their DB record.
- Global search (`/api/search`) passes `display_chat_ids` as param to `db.search_messages_global()`.
- WebSocket subscriptions also check `display_chat_ids` (line 1195).

## Code Review Fixes Applied (2026-02-24)
- **CORS Headers**: Ensured admin endpoints include proper CORS headers for cross-origin admin panel requests
- **DISPLAY_CHAT_IDS Backward Compatibility**: Master respects DISPLAY_CHAT_IDS when no viewer accounts exist, ensuring no behavior change for existing deployments
- **Session TTL**: Implemented proper session timeout for viewer sessions matching AUTH_SESSION_SECONDS configuration
- **Adapter Field Whitelist**: Ensured get_all_viewer_accounts returns only necessary fields (id, username, allowed_chat_ids, is_active, created_at) — excludes password_hash and salt for security

## Requirements

### Functional
- F1: `POST /api/admin/viewers` — Create viewer account (admin only)
- F2: `GET /api/admin/viewers` — List all viewer accounts (admin only)
- F3: `PUT /api/admin/viewers/{id}` — Update viewer account (admin only)
- F4: `DELETE /api/admin/viewers/{id}` — Delete viewer account (admin only)
- F5: `GET /api/admin/chats` — List ALL chats for chat picker (admin only, ignores display_chat_ids)
- F6: All existing endpoints filter by logged-in user's allowed_chat_ids
- F7: Master sees all chats (or `display_chat_ids` if set and no viewers exist)

### Non-Functional
- NF1: Admin endpoints return 403 for non-master users
- NF2: Deleting a viewer invalidates their active sessions immediately
- NF3: Updating viewer's allowed_chat_ids takes effect on next request (session store updated)

## Architecture

### Helper: Resolve User's Allowed Chat IDs

Central function replacing all scattered `config.display_chat_ids` checks:

```python
def _get_user_chat_ids(request: Request) -> set[int] | None:
    """Get the set of chat IDs the current user can access.

    Returns:
        set[int] — restricted to these chat IDs
        None — user can see ALL chats (no restriction)
    """
    user = getattr(request.state, "user", None)
    if not user:
        return None  # auth disabled, full access

    if user["role"] == "master":
        # Master always sees ALL chats (validated decision — DISPLAY_CHAT_IDS ignored for master)
        <!-- Updated: Validation Session 1 - master always sees all chats -->
        return None

    # Viewer: use their allowed_chat_ids
    return user.get("allowed_chat_ids") or set()
```

### Helper: Admin-Only Dependency

```python
def require_admin(request: Request):
    """Dependency that requires master/admin role."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "master":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
```

## Related Code Files

| File | Action | Changes |
|------|--------|---------|
| `src/web/main.py` | MODIFY | Add admin CRUD endpoints, `_get_user_chat_ids`, `require_admin`, refactor all filtering |

## Implementation Steps

### Step 1: Add Helper Functions (src/web/main.py)

Add after `_get_current_user` (from Phase 1):

```python
def _get_user_chat_ids(request: Request) -> set[int] | None:
    """Resolve allowed chat IDs for current user. None = all chats."""
    user = getattr(request.state, "user", None)
    if not user:
        return None

    if user["role"] == "master":
        return config.display_chat_ids if config.display_chat_ids else None

    return user.get("allowed_chat_ids") or set()


def require_admin(request: Request, _=Depends(require_auth)):
    """Dependency: requires master role. Must be used with require_auth."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "master":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
```

### Step 2: Add Admin CRUD Endpoints (src/web/main.py)

Add a new section after the export endpoint (~line 1137):

```python
# ============================================================================
# Admin: Viewer Account Management
# ============================================================================

@app.get("/api/admin/viewers", dependencies=[Depends(require_admin)])
async def list_viewer_accounts():
    """List all viewer accounts (admin only)."""
    try:
        accounts = await db.get_all_viewer_accounts()
        return {"viewers": accounts}
    except Exception as e:
        logger.error(f"Error listing viewer accounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/viewers", dependencies=[Depends(require_admin)])
async def create_viewer_account(request: Request):
    """Create a new viewer account (admin only)."""
    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        allowed_chat_ids = data.get("allowed_chat_ids", [])

        if not username or not password:
            raise HTTPException(status_code=400, detail="Username and password required")

        if len(password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

        # Reject if username matches master username
        if username.lower() == VIEWER_USERNAME.lower():
            raise HTTPException(status_code=400, detail="Username conflicts with master account")

        # Check uniqueness
        existing = await db.get_viewer_account_by_username(username)
        if existing:
            raise HTTPException(status_code=409, detail="Username already exists")

        # Validate chat IDs are integers
        try:
            allowed_chat_ids = [int(cid) for cid in allowed_chat_ids]
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

        password_hash, salt = _hash_password(password)
        result = await db.create_viewer_account(username, password_hash, salt, allowed_chat_ids)

        return {"success": True, "viewer": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating viewer account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/viewers/{viewer_id}", dependencies=[Depends(require_admin)])
async def update_viewer_account(viewer_id: int, request: Request):
    """Update a viewer account (admin only). Password optional."""
    try:
        data = await request.json()
        updates = {}

        # Optional password update
        password = data.get("password", "").strip()
        if password:
            if len(password) < 4:
                raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
            password_hash, salt = _hash_password(password)
            updates["password_hash"] = password_hash
            updates["salt"] = salt

        # Optional chat IDs update
        if "allowed_chat_ids" in data:
            try:
                allowed = [int(cid) for cid in data["allowed_chat_ids"]]
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid chat ID format")
            updates["allowed_chat_ids"] = json.dumps(allowed)

        # Optional is_active update
        if "is_active" in data:
            updates["is_active"] = 1 if data["is_active"] else 0

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        success = await db.update_viewer_account(viewer_id, **updates)
        if not success:
            raise HTTPException(status_code=404, detail="Viewer account not found")

        # Invalidate active sessions for this viewer (force re-login on next request)
        tokens_to_remove = [
            token for token, info in _viewer_sessions.items()
            if info.get("viewer_id") == viewer_id
        ]
        for token in tokens_to_remove:
            del _viewer_sessions[token]

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating viewer account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/viewers/{viewer_id}", dependencies=[Depends(require_admin)])
async def delete_viewer_account_endpoint(viewer_id: int):
    """Delete a viewer account (admin only)."""
    try:
        # Invalidate sessions first
        tokens_to_remove = [
            token for token, info in _viewer_sessions.items()
            if info.get("viewer_id") == viewer_id
        ]
        for token in tokens_to_remove:
            del _viewer_sessions[token]

        success = await db.delete_viewer_account(viewer_id)
        if not success:
            raise HTTPException(status_code=404, detail="Viewer account not found")

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting viewer account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/chats", dependencies=[Depends(require_admin)])
async def list_all_chats_admin():
    """List ALL chats (ignores display_chat_ids). For admin chat picker."""
    try:
        chats = await db.get_all_chats()
        return {"chats": [{"id": c["id"], "title": c.get("title") or c.get("first_name") or str(c["id"]), "type": c.get("type", "")} for c in chats]}
    except Exception as e:
        logger.error(f"Error listing admin chats: {e}")
        raise HTTPException(status_code=500, detail=str(e))
```

### Step 3: Refactor Chat Filtering in Existing Endpoints

Replace all occurrences of `config.display_chat_ids` checks with `_get_user_chat_ids(request)`. This requires adding `request: Request` parameter to endpoints that don't have it.

**Pattern — before:**
```python
if config.display_chat_ids and chat_id not in config.display_chat_ids:
    raise HTTPException(status_code=403, detail="Access denied")
```

**Pattern — after:**
```python
user_chats = _get_user_chat_ids(request)
if user_chats is not None and chat_id not in user_chats:
    raise HTTPException(status_code=403, detail="Access denied")
```

**Endpoints to update (all in src/web/main.py):**

| Endpoint | Line | Change |
|----------|------|--------|
| `GET /api/chats` | 580-584 | Replace `config.display_chat_ids` with `user_chats` in filter logic |
| `GET /api/search` | 646 | Pass `user_chats` list to `display_chat_ids` param |
| `GET /api/chats/{chat_id}/media` | 668 | Add access check |
| `GET /api/chats/{chat_id}/messages/{message_id}/context` | 682 | Add access check |
| `GET /api/chats/{chat_id}/messages` | 745 | Add access check |
| `GET /api/chats/{chat_id}/pinned` | 797 | Add access check |
| `GET /api/chats/{chat_id}/topics` | 829 | Add access check |
| `GET /api/archived/count` | 845-850 | Filter by user_chats |
| `GET /api/chats/{chat_id}/stats` | 1041 | Add access check |
| `GET /api/chats/{chat_id}/messages/by-date` | 1063 | Add access check |
| `GET /api/chats/{chat_id}/export` | 1101 | Add access check |
| `WebSocket /ws/updates` | 1195 | Add access check on subscribe |
| `handle_realtime_notification` | 178 | This is internal; keep as-is (uses global display_chat_ids) |

**Detailed example for GET /api/chats (line 564):**

```python
@app.get("/api/chats", dependencies=[Depends(require_auth)])
async def get_chats(
    request: Request,  # ADD this parameter
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    search: str = Query(None),
    archived: bool | None = Query(None),
    folder_id: int | None = Query(None),
):
    try:
        user_chats = _get_user_chat_ids(request)
        if user_chats is not None:
            chats = await db.get_all_chats(search=search, archived=archived, folder_id=folder_id)
            chats = [c for c in chats if c["id"] in user_chats]
            total = len(chats)
            chats = chats[offset : offset + limit]
        else:
            chats = await db.get_all_chats(
                limit=limit, offset=offset, search=search, archived=archived, folder_id=folder_id
            )
            total = await db.get_chat_count(search=search, archived=archived, folder_id=folder_id)
        # ... rest unchanged
```

**For GET /api/search (line 618):**

```python
async def global_search(
    request: Request,  # ADD
    q: str = "", ...
):
    ...
    user_chats = _get_user_chat_ids(request)
    results = await db.search_messages_global(
        query=q,
        display_chat_ids=list(user_chats) if user_chats else None,
        ...
    )
```

**For WebSocket (line 1163):**

The WebSocket doesn't go through `require_auth` dependency. For WebSocket auth, check the cookie from the connection headers:

```python
@app.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    # Extract auth cookie from WebSocket headers
    cookies = websocket.cookies
    auth_cookie = cookies.get(AUTH_COOKIE_NAME)
    user = _get_current_user(auth_cookie) if AUTH_ENABLED else {"role": "master", "allowed_chat_ids": None}

    # Determine user's chat access
    if user and user["role"] == "master":
        ws_allowed_chats = config.display_chat_ids if config.display_chat_ids else None
    elif user:
        ws_allowed_chats = user.get("allowed_chat_ids") or set()
    else:
        ws_allowed_chats = set()  # unauthenticated = no access

    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "subscribe":
                chat_id = data.get("chat_id")
                if chat_id:
                    if ws_allowed_chats is not None and chat_id not in ws_allowed_chats:
                        await websocket.send_json({"type": "error", "message": "Access denied"})
                    else:
                        ws_manager.subscribe(websocket, chat_id)
                        await websocket.send_json({"type": "subscribed", "chat_id": chat_id})
            # ... rest unchanged
```

<!-- Updated: Validation Session 1 - audit log, master sees all, immediate session update, block master username -->

### Step 4: Add Audit Logging

Add audit logging middleware for viewer requests:

```python
async def _log_viewer_audit(request: Request, chat_id: int | None = None):
    """Log viewer API access for audit trail. Only logs viewer (non-master) requests."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "viewer" or not db:
        return
    try:
        await db.create_audit_log(
            viewer_id=user["viewer_id"],
            username=user["username"],
            endpoint=str(request.url.path),
            chat_id=chat_id,
            ip_address=request.client.host if request.client else None,
        )
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")
```

Call `_log_viewer_audit(request, chat_id)` in chat-specific endpoints after access check passes.

### Step 5: Add Audit Log Admin Endpoint

```python
@app.get("/api/admin/audit", dependencies=[Depends(require_admin)])
async def get_audit_log(
    viewer_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get viewer activity audit log (admin only)."""
    entries = await db.get_audit_log(viewer_id=viewer_id, limit=limit, offset=offset)
    return {"entries": entries, "total": len(entries)}
```

Add corresponding DB adapter methods:
```python
@retry_on_locked()
async def create_audit_log(self, viewer_id, username, endpoint, chat_id=None, ip_address=None):
    async with self.db_manager.get_session() as session:
        entry = ViewerAuditLog(
            viewer_id=viewer_id, username=username, endpoint=endpoint,
            chat_id=chat_id, ip_address=ip_address,
        )
        session.add(entry)

@retry_on_locked()
async def get_audit_log(self, viewer_id=None, limit=100, offset=0):
    async with self.db_manager.get_session() as session:
        query = select(ViewerAuditLog).order_by(ViewerAuditLog.timestamp.desc())
        if viewer_id:
            query = query.where(ViewerAuditLog.viewer_id == viewer_id)
        query = query.offset(offset).limit(limit)
        result = await session.execute(query)
        return [{"id": e.id, "viewer_id": e.viewer_id, "username": e.username,
                 "endpoint": e.endpoint, "chat_id": e.chat_id,
                 "ip_address": e.ip_address, "timestamp": e.timestamp.isoformat()}
                for e in result.scalars().all()]
```

## Todo List

- [x] Add `_get_user_chat_ids` helper — master returns None (all chats)
- [x] Add `require_admin` dependency
- [x] Add `GET /api/admin/viewers` endpoint
- [x] Add `POST /api/admin/viewers` endpoint — block master username
- [x] Add `PUT /api/admin/viewers/{id}` endpoint — immediate session update
- [x] Add `DELETE /api/admin/viewers/{id}` endpoint
- [x] Add `GET /api/admin/chats` endpoint
- [x] Refactor `GET /api/chats` to use per-user filtering
- [x] Refactor `GET /api/search` to use per-user filtering
- [x] Refactor all 9 `chat_id`-specific endpoints to use per-user access check
- [x] Update WebSocket subscribe to use per-user chat access
- [x] Add `request: Request` parameter to endpoints that need it
- [x] Add `_log_viewer_audit` helper and call in chat-specific endpoints
- [x] Add `GET /api/admin/audit` endpoint
- [x] Add `create_audit_log` and `get_audit_log` DB adapter methods

## Success Criteria
- Admin can CRUD viewer accounts via API
- Non-admin gets 403 on admin endpoints
- Viewer sees only their allowed chats in all endpoints (chats list, search, media, messages, etc.)
- Master user sees all chats (or display_chat_ids if configured)
- Deleting/updating viewer invalidates their active sessions
- WebSocket subscriptions respect per-user access

## Risk Assessment
- **Many endpoint changes**: Each endpoint needs `request: Request` param added and filtering updated. Risk of missing one. Mitigate: grep for remaining `config.display_chat_ids` after refactoring.
- **WebSocket auth**: WebSocket connections don't use FastAPI dependencies. Cookie extraction from headers is different. Test thoroughly.
- **Performance**: Viewer with many allowed chats — chat list filtering is in-memory (fine for <1000 chats).

## Security Considerations
- Admin endpoints protected by `require_admin` dependency (checks role == "master")
- Viewer cannot escalate to admin via API manipulation
- Session invalidation on account update/delete prevents stale access
- Username uniqueness enforced; master username collision prevented

## Next Steps
- Phase 3: Frontend UI for admin settings and user management
