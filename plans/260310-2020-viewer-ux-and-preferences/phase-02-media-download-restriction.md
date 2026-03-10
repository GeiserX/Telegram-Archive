# Phase 2: Media Download Restriction

## Overview
- **Priority:** P1
- **Status:** Complete
- **Effort:** 3h

Add a toggle to restrict media/photo downloads for viewer accounts and share tokens. Default: ON (restricted). Admins can toggle off when creating/editing viewers and tokens. Frontend enforces restriction via disabled context menu and CSS protection. Not a hard security boundary — it's a deterrent.

## Key Insights

- No existing download restriction mechanism
- `ViewerAccount` and `ViewerToken` models need new column
- Frontend right-click context menu already exists — add/remove "Save image" based on permission
- CSS `pointer-events: none` + `user-select: none` on media elements for restricted users
- Session info already carries `role` and `allowed_chat_ids` — add `no_download` flag

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `src/db/models.py` | Modify | Add `no_download` column to `ViewerAccount` and `ViewerToken` |
| `alembic/versions/` | Create | Migration 012: add `no_download` column |
| `src/db/adapter.py` | Modify | Pass `no_download` in create/update viewer/token methods |
| `src/web/main.py` | Modify | Accept `no_download` in create/update endpoints, include in session info |
| `src/web/templates/index.html` | Modify | Add toggle in create/edit forms, enforce restriction in UI |

## Implementation Steps

### Backend

1. **Add `no_download` column to models** (`models.py`)
   ```python
   # ViewerAccount — after is_active
   no_download: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

   # ViewerToken — after is_revoked
   no_download: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
   ```

2. **Create Alembic migration 012**
   ```python
   # alembic/versions/20260310_012_add_no_download.py
   op.add_column('viewer_accounts', sa.Column('no_download', sa.Integer(), server_default='1'))
   op.add_column('viewer_tokens', sa.Column('no_download', sa.Integer(), server_default='1'))
   ```

3. **Update adapter methods** (`adapter.py`)
   - `create_viewer_account()` — accept `no_download` param
   - `update_viewer_account()` — accept `no_download` in kwargs
   - `create_viewer_token()` — accept `no_download` param
   - `update_viewer_token()` — accept `no_download` in kwargs
   - Return `no_download` field in all viewer/token list/get responses

4. **Update API endpoints** (`main.py`)
   - `POST /api/admin/viewers` — accept `no_download` from request body (default True)
   - `PUT /api/admin/viewers/{id}` — accept `no_download` update
   - `POST /api/admin/tokens` — accept `no_download` from request body (default True)
   - `PUT /api/admin/tokens/{id}` — accept `no_download` update
   - **Login handler** — add `no_download` to session info for both viewer accounts and token auth
   - `GET /api/auth/me` or session info — expose `no_download` flag to frontend

5. **Expose permission in auth check** (`main.py`)
   - When building session/user_info dict, include `no_download` flag
   - Frontend reads this on auth check to know restriction status

### Frontend

6. **Add toggle in create viewer form** (index.html)
   ```html
   <label class="flex items-center gap-2 text-sm text-tg-text">
       <input type="checkbox" v-model="newViewerNoDownload" class="rounded">
       Restrict media downloads (default: on)
   </label>
   ```

7. **Add toggle in create token form**
   Same pattern as viewer form.

8. **Add toggle in edit viewer/token forms**
   Same pattern, bound to edit state refs.

9. **Enforce restriction in UI**
   - Store `canDownload` computed from auth response
   - On media elements: conditionally add CSS class `no-download`
   - CSS: `.no-download img, .no-download video { pointer-events: none; user-select: none; -webkit-user-drag: none; }`
   - Context menu: hide "Save image" / "Download" options when `!canDownload`
   - Disable right-click on media for restricted users (already partially done, just scope it)

10. **Pass in API calls**
    - `createViewer()` — send `no_download: newViewerNoDownload.value`
    - `createToken()` — send `no_download: newTokenNoDownload.value`
    - `saveViewerEdit()` — send `no_download` in update payload
    - `saveTokenEdit()` — send `no_download` in update payload

## Todo List

- [ ] Add `no_download` column to `ViewerAccount` model
- [ ] Add `no_download` column to `ViewerToken` model
- [ ] Create Alembic migration 012
- [ ] Update adapter create/update methods
- [ ] Update adapter list/get methods to return `no_download`
- [ ] Update viewer create/update endpoints
- [ ] Update token create/update endpoints
- [ ] Add `no_download` to session info on login
- [ ] Expose flag in auth check response
- [ ] Add toggle in create viewer form (default ON)
- [ ] Add toggle in create token form (default ON)
- [ ] Add toggle in edit viewer form
- [ ] Add toggle in edit token form
- [ ] Add CSS protection on media elements
- [ ] Conditionally show/hide download in context menu
- [ ] Add Vue refs: `newViewerNoDownload`, `newTokenNoDownload`

## Security Considerations

- This is a **deterrent, not DRM**. Determined users can use DevTools to bypass
- Backend does NOT block media API routes — media URLs still accessible
- Acceptable trade-off: prevents casual right-click saving, doesn't promise cryptographic protection
- Admin (master) always has full download access
