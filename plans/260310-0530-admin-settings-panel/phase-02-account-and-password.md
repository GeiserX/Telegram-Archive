# Phase 2: Account & Password Management

**Priority:** HIGH
**Status:** TODO
**Effort:** Small
**Files:** `src/web/main.py`, `src/web/templates/index.html`

---

## Overview

Add self-service password change for the admin. Currently only API-level password management exists for viewer accounts — no way for the logged-in master to change their own password. Master credentials come from env vars (`VIEWER_USERNAME`, `VIEWER_PASSWORD`), so we need a DB-override mechanism.

---

## Key Insights

- Master auth currently checks env vars only (line 613-624 in `main.py`)
- Viewer accounts have DB-stored passwords with PBKDF2-SHA256
- No `MasterPasswordOverride` table exists — must handle this
- Simplest approach: store master password override in `app_settings` table (Phase 5) or use existing `ViewerAccount` with a special flag
- **Better approach:** Create a master account row in `viewer_accounts` with `is_master=1` flag, or use `app_settings` key-value

**Chosen approach:** Use `app_settings` table (Phase 5 dependency) with key `master_password_hash` and `master_password_salt`. Login flow checks DB override first, falls back to env vars.

**Alternative (no Phase 5 dependency):** Store in a simple `admin_settings` key-value in existing metadata. Actually, the simplest: just make the login check viewer_accounts FIRST (already does), then allow creating a viewer account with the master username. No — that breaks the uniqueness check.

**Final approach:** New endpoint `PUT /api/auth/password` that:
- For master: writes override hash/salt to DB metadata table (already exists for VAPID keys)
- For viewer: updates their own viewer_account password via `update_viewer_account`
- Login flow: check DB override for master before env var fallback

---

## Related Code

- `POST /api/login` — line 568 in `main.py`
- `_verify_password()` — existing PBKDF2 verification helper
- `_hash_password()` — existing PBKDF2 hashing helper
- `ViewerAccount` model — line 330 in `models.py`
- `db.update_viewer_account()` — line 1903 in `adapter.py`

---

## Implementation Steps

### 1. New endpoint: `PUT /api/auth/password`

Add after `/api/logout` endpoint:

```python
@app.put("/api/auth/password")
async def change_own_password(
    request: Request,
    body: dict = Body(...),
    _=Depends(require_auth),
):
    user = request.state.user
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")

    if len(new_pw) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    if user["role"] == "master":
        # Verify current password against env var (or DB override)
        if not _verify_master_password(current):
            raise HTTPException(403, "Current password incorrect")
        # Store override in DB metadata
        new_hash, new_salt = _hash_password(new_pw)
        await db.upsert_metadata("master_password_hash", new_hash)
        await db.upsert_metadata("master_password_salt", new_salt)
        # Update in-memory AUTH_TOKEN so existing sessions still work
        # ... recalculate AUTH_TOKEN

    elif user["role"] == "viewer":
        viewer_id = user.get("viewer_id")
        if not viewer_id:
            raise HTTPException(400, "Token sessions cannot change password")
        account = await db.get_viewer_account_by_id(viewer_id)
        if not _verify_password(current, account["password_hash"], account["salt"]):
            raise HTTPException(403, "Current password incorrect")
        new_hash, new_salt = _hash_password(new_pw)
        await db.update_viewer_account(viewer_id, password_hash=new_hash, salt=new_salt)

    return {"success": True}
```

### 2. Update login flow for master password DB override

In `POST /api/login`, before checking env vars:

```python
# Check for master password override in DB
master_hash = await db.get_metadata("master_password_hash")
master_salt = await db.get_metadata("master_password_salt")
if master_hash and master_salt:
    if username.lower() == config.viewer_username.lower():
        if _verify_password(password, master_hash, master_salt):
            # Master login via DB override
            ...
```

### 3. Check if `upsert_metadata` / `get_metadata` exist in adapter

The adapter uses metadata for VAPID keys already. Verify methods exist:
- `db.store_metadata(key, value)` or `db.upsert_metadata(key, value)`
- `db.get_metadata(key)`

If not, add them (simple key-value in existing `metadata` table).

### 4. Account tab UI in settings modal

```html
<div v-if="settingsTab === 'account'" class="space-y-6">
  <!-- Current account info -->
  <div class="bg-tg-bg rounded-xl p-4 border border-gray-700">
    <div class="flex items-center gap-3">
      <div class="w-12 h-12 rounded-full bg-blue-600 flex items-center justify-center text-white font-bold text-lg">
        {{ (currentUsername || '?')[0].toUpperCase() }}
      </div>
      <div>
        <div class="text-tg-text font-semibold">{{ currentUsername }}</div>
        <div class="text-tg-muted text-sm">{{ userRole === 'master' ? 'Administrator' : 'Viewer' }}</div>
      </div>
    </div>
  </div>

  <!-- Change password form -->
  <div>
    <h3 class="text-lg font-semibold text-tg-text mb-4">Change Password</h3>
    <form @submit.prevent="changePassword" class="space-y-3">
      <input v-model="currentPassword" type="password" placeholder="Current password"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2.5 border border-gray-700 focus:border-blue-500 focus:outline-none">
      <input v-model="newPassword" type="password" placeholder="New password"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2.5 border border-gray-700 focus:border-blue-500 focus:outline-none">
      <input v-model="confirmPassword" type="password" placeholder="Confirm new password"
        class="w-full bg-tg-input text-tg-text rounded-lg px-4 py-2.5 border border-gray-700 focus:border-blue-500 focus:outline-none">
      <div v-if="passwordError" class="text-red-400 text-sm">{{ passwordError }}</div>
      <div v-if="passwordSuccess" class="text-green-400 text-sm">{{ passwordSuccess }}</div>
      <button type="submit" :disabled="changingPassword"
        class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition disabled:opacity-50">
        {{ changingPassword ? 'Changing...' : 'Change Password' }}
      </button>
    </form>
  </div>

  <!-- Session info -->
  <div class="text-tg-muted text-sm">
    <p>Session expires: {{ sessionExpiryText }}</p>
  </div>
</div>
```

### 5. Vue methods for password change

```javascript
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const passwordError = ref('')
const passwordSuccess = ref('')
const changingPassword = ref(false)

async function changePassword() {
  passwordError.value = ''
  passwordSuccess.value = ''
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = 'Passwords do not match'
    return
  }
  if (newPassword.value.length < 4) {
    passwordError.value = 'Password must be at least 4 characters'
    return
  }
  changingPassword.value = true
  try {
    const res = await fetch('/api/auth/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        current_password: currentPassword.value,
        new_password: newPassword.value
      })
    })
    const data = await res.json()
    if (res.ok) {
      passwordSuccess.value = 'Password changed successfully'
      currentPassword.value = ''
      newPassword.value = ''
      confirmPassword.value = ''
    } else {
      passwordError.value = data.detail || 'Failed to change password'
    }
  } catch {
    passwordError.value = 'Network error'
  } finally {
    changingPassword.value = false
  }
}
```

---

## Todo

- [ ] Check if `metadata` table + adapter methods exist for key-value storage
- [ ] Add `PUT /api/auth/password` endpoint
- [ ] Update master login to check DB override before env vars
- [ ] Add helper `_verify_master_password()` that checks DB override then env var
- [ ] Build Account tab UI with password change form
- [ ] Add Vue state and methods for password change
- [ ] Test: master password change → re-login works with new password
- [ ] Test: viewer password change → re-login works
- [ ] Test: wrong current password → 403 error
- [ ] Test: token session → cannot change password

---

## Success Criteria

- Admin can change own password from settings panel
- Old password required for verification
- New password persists across container restart (stored in DB)
- Login still works with new password
- Env var password is fallback if no DB override exists
