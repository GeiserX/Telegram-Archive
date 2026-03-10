# Per-User Preferences Storage - Research Report

**Date:** 2026-03-10 | **Scope:** FastAPI + SQLAlchemy async | **Status:** Ready for implementation planning

---

## Executive Summary

**Recommendation:** Single database (add preferences tables to main DB) with separate engine/session for preferences queries. This is pragmatic: avoids SQLite file complexity, leverages existing dual-DB infrastructure, enables efficient per-user queries, and minimizes async session management.

Trade-offs: Slightly larger main DB file, but negligible for UI preferences. Benefits: unified migrations, single connection pool, simpler backup/restore.

---

## Question 1: Separate SQLite vs Main DB Tables?

### Separate File Approach
**Pros:**
- Clean separation of concerns
- Can rotate/archive preferences independently
- Easy to reset without touching backup data

**Cons:**
- Doubles connection management complexity (two engines, two connection pools)
- Async session handling becomes messier (which session for which query?)
- SQLite file locking on both files (performance impact under load)
- Migration/restore complexity (backup DB and prefs DB must stay in sync)
- DevOps nightmare: two Docker volumes or one?

### Single DB Tables Approach ✅ RECOMMENDED
**Pros:**
- Leverages existing `DatabaseManager` dual-engine setup (SQLite + PostgreSQL already handled)
- One migration story (Alembic + SQLite `create_all`)
- One connection pool, simpler async context
- Easier backup/restore (single database file)
- Clear owner precedent: `ViewerAccount`, `ViewerSession`, `ViewerToken` already in main DB

**Cons:**
- Preferences live alongside backup data (but minimal storage footprint)
- Slightly larger SQLite file (negligible for JSON prefs)

**Decision:** Add tables to `src/db/models.py`. Leverage existing `DatabaseAdapter`.

---

## Question 2: Efficient Per-User Per-Chat Preferences Schema

### Table Structure

```sql
-- User preferences (scope: viewer account or share token)
CREATE TABLE user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,              -- FK to ViewerAccount.id or NULL for share tokens
    share_token_id INTEGER,                -- FK to ViewerToken.id OR NULL for accounts
    preference_key VARCHAR(100) NOT NULL,  -- "background_theme", "font_size", etc.
    preference_value TEXT NOT NULL,        -- JSON value or scalar
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, share_token_id, preference_key),  -- Only one value per user+key
    CHECK (user_id IS NOT NULL OR share_token_id IS NOT NULL)
);

-- Per-chat background/display preferences
CREATE TABLE chat_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                       -- FK to ViewerAccount.id OR NULL for share tokens
    share_token_id INTEGER,                -- FK to ViewerToken.id OR NULL
    chat_id BIGINT NOT NULL,               -- Telegram chat ID
    background_choice VARCHAR(255),        -- "dark", "light", "custom_uuid"
    custom_background_url TEXT,            -- URL if user uploaded custom bg
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, share_token_id, chat_id),
    CHECK (user_id IS NOT NULL OR share_token_id IS NOT NULL)
);
```

### Query Patterns (Async SQLAlchemy)

```python
# Get all prefs for a viewer account
prefs = await db.session.execute(
    select(ChatPreference)
    .where(ChatPreference.user_id == viewer_id)
)

# Get all prefs for a share token (ephemeral)
prefs = await db.session.execute(
    select(ChatPreference)
    .where(ChatPreference.share_token_id == token_id)
)

# Get specific chat background for user
bg = await db.session.execute(
    select(ChatPreference.background_choice)
    .where(
        ChatPreference.user_id == viewer_id,
        ChatPreference.chat_id == chat_id
    )
).scalar_one_or_none()
```

### Why This Works

1. **Flexibility:** Handles both viewer accounts (persistent, linked to user record) and share tokens (ephemeral, standalone)
2. **Exclusivity:** `UNIQUE (user_id, share_token_id, chat_id)` prevents duplicates; `CHECK` ensures one is NOT NULL
3. **Efficient queries:** Single-table lookups, indexed on `(user_id, chat_id)` and `(share_token_id, chat_id)`
4. **No joins needed:** Direct filtering by user OR token
5. **Scalable:** Add new preference types without schema change (use `preference_key` column)

---

## Question 3: Share Tokens vs Viewer Accounts

### Token Lifecycle
- **Created:** Master/viewer admin creates token, optionally sets expiry
- **Shared:** Plaintext URL sent (hashed in DB)
- **Stateless:** Token holder has NO account, NO login persistence
- **Ephemeral data:** Preferences stored under `share_token_id`, deleted on token revocation

### Viewer Account Lifecycle
- **Created:** Master/viewer admin creates account with password
- **Persistent:** Survives across sessions (via `ViewerSession` cookie)
- **Preferences:** Stored under `user_id`, survived account lifetime

### Implementation Strategy

```python
# In web/main.py, current_user dependency:
@app.dependency
async def get_current_user(token: str = Cookie(None)):
    # Try viewer session first
    session = await db.get_viewer_session(token)
    if session:
        return {"type": "viewer", "id": session.user_id, "username": session.username}

    # Try share token
    token_record = await db.verify_share_token(token)
    if token_record:
        return {"type": "share_token", "id": token_record.id, "username": None}

    raise HTTPException(403)

# Fetch preferences anywhere
async def get_chat_background(current_user, chat_id):
    if current_user["type"] == "viewer":
        return await db.get_chat_pref(
            user_id=current_user["id"],
            chat_id=chat_id
        )
    else:  # share_token
        return await db.get_chat_pref(
            share_token_id=current_user["id"],
            chat_id=chat_id
        )
```

---

## Question 4: Dual-Database with SQLAlchemy Async

### Current Setup (Observed)
`src/db/base.py` creates ONE async engine per dialect (SQLite or PostgreSQL), wrapped in `DatabaseManager`. All operations go through single `DatabaseAdapter` with dialect-specific branches.

### For Preferences

**Option A:** Use same engine + new tables
- ✅ **SIMPLEST** — Single session factory, single connection pool
- ✅ Works with existing `DatabaseAdapter`
- ✅ No async complexity

**Option B:** Separate preferences engine
- ❌ Two engines, two sessions, confusing ownership
- ❌ Requires explicit session management (`async with session()`)
- ❌ Defeats SQLite WAL performance gains

**Decision:** Use Option A. Preferences and backup data share same engine.

### Implementation in DatabaseAdapter

```python
# Add to adapter.py
async def get_chat_preference(self, chat_id: int, user_id: int | None = None, share_token_id: int | None = None):
    stmt = select(ChatPreference).where(ChatPreference.chat_id == chat_id)
    if user_id:
        stmt = stmt.where(ChatPreference.user_id == user_id)
    elif share_token_id:
        stmt = stmt.where(ChatPreference.share_token_id == share_token_id)

    result = await self.session.execute(stmt)
    return result.scalar_one_or_none()

async def upsert_chat_preference(self, chat_id: int, background: str, user_id: int | None = None, share_token_id: int | None = None):
    # SQLite
    if self.is_sqlite:
        stmt = sqlite_insert(ChatPreference).values(
            user_id=user_id,
            share_token_id=share_token_id,
            chat_id=chat_id,
            background_choice=background,
            updated_at=datetime.utcnow()
        ).on_conflict_do_update(
            index_elements=["user_id", "share_token_id", "chat_id"],
            set_={"background_choice": background, "updated_at": datetime.utcnow()}
        )
    # PostgreSQL
    else:
        stmt = pg_insert(ChatPreference).values(...).on_conflict_do_update(...)

    await self.session.execute(stmt)
    await self.session.commit()
```

---

## Question 5: localStorage vs Server-Side Storage

### Analysis by User Type

#### Share Token Holders
- **No persistent identity** → localStorage is ONLY option
- **BUT:** If they reload, prefs lost (unless backend stores for session duration)
- **Best practice:** Server-side + optional localStorage cache
- **Flow:**
  1. User clicks background → localStorage UPDATE + POST to backend
  2. Backend stores in `chat_preferences` under `share_token_id`
  3. On reload → fetch from server, populate localStorage

#### Viewer Accounts
- **Persistent identity** → Server-side ONLY
- **Why:** Preferences must survive logout/login, device changes, browser clear
- **localStorage fallback:** Use for instant UI response (optimistic update), but always sync to server
- **Flow:**
  1. User clicks background → localStorage UPDATE + POST to backend
  2. Backend stores in `chat_preferences` under `user_id`
  3. On reload → fetch from server if localStorage missing/stale

### Hybrid Approach ✅ RECOMMENDED

```javascript
// Frontend: src/web/templates/chat-preferences.js
class ChatPreferences {
  constructor(userId, shareTokenId) {
    this.userId = userId;
    this.shareTokenId = shareTokenId;
    this.cache = this.loadFromLocalStorage();
  }

  async setBackground(chatId, bgChoice) {
    // Instant UI update
    this.cache[chatId] = bgChoice;
    this.saveToLocalStorage();

    // Persist to server
    try {
      await fetch('/api/preferences/chat', {
        method: 'POST',
        body: JSON.stringify({ chat_id: chatId, background_choice: bgChoice })
      });
    } catch (e) {
      console.warn('Failed to sync preferences, using local copy');
    }
  }

  async getBackground(chatId) {
    // Check localStorage first (instant)
    if (this.cache[chatId]) return this.cache[chatId];

    // Fall back to server
    const resp = await fetch(`/api/preferences/chat/${chatId}`);
    const bg = await resp.json();
    this.cache[chatId] = bg;
    this.saveToLocalStorage();
    return bg;
  }

  loadFromLocalStorage() {
    const key = this.userId ? `prefs-user-${this.userId}` : `prefs-token-${this.shareTokenId}`;
    return JSON.parse(localStorage.getItem(key) || '{}');
  }

  saveToLocalStorage() {
    const key = this.userId ? `prefs-user-${this.userId}` : `prefs-token-${this.shareTokenId}`;
    localStorage.setItem(key, JSON.stringify(this.cache));
  }
}
```

### Server-Side Implementation

```python
# Add to web/main.py
@app.post("/api/preferences/chat")
async def set_chat_preference(
    request: Request,
    chat_id: int,
    background_choice: str,
    current_user = Depends(get_current_user)
):
    if current_user["type"] == "viewer":
        await db.upsert_chat_preference(
            chat_id=chat_id,
            background=background_choice,
            user_id=current_user["id"]
        )
    else:  # share_token
        await db.upsert_chat_preference(
            chat_id=chat_id,
            background=background_choice,
            share_token_id=current_user["id"]
        )
    return {"status": "ok"}

@app.get("/api/preferences/chat/{chat_id}")
async def get_chat_preference(
    chat_id: int,
    current_user = Depends(get_current_user)
):
    if current_user["type"] == "viewer":
        pref = await db.get_chat_preference(chat_id=chat_id, user_id=current_user["id"])
    else:
        pref = await db.get_chat_preference(chat_id=chat_id, share_token_id=current_user["id"])

    return {"background_choice": pref.background_choice if pref else "default"}
```

---

## Implementation Checklist

1. **Database Schema**
   - [ ] Add `UserPreferences` + `ChatPreferences` models to `src/db/models.py`
   - [ ] Create Alembic migration for PostgreSQL
   - [ ] Test with both SQLite and PostgreSQL

2. **Backend API**
   - [ ] Add preference methods to `DatabaseAdapter`
   - [ ] Add `/api/preferences/*` endpoints to `src/web/main.py`
   - [ ] Implement preference cleanup on token revocation (cascade delete)

3. **Frontend**
   - [ ] Create `src/web/static/preferences.js` module
   - [ ] Add background selector UI to chat viewer
   - [ ] Integrate localStorage + server sync

4. **Migrations**
   - [ ] Alembic migration for PostgreSQL
   - [ ] SQLite: auto-created via `Base.metadata.create_all`

5. **Testing**
   - [ ] Unit: preference CRUD operations
   - [ ] Integration: viewer account vs share token isolation
   - [ ] Async: SQLAlchemy session management

---

## Unresolved Questions

1. **Background storage location:** Should custom background images be stored in `BACKUP_PATH/backgrounds/` or as base64 in DB?
   - Recommend: `BACKUP_PATH/backgrounds/{user_id or token_id}/{hash}.webp` with DB storing only filename

2. **Preference versioning:** Do we track preference change history or just the current value?
   - Recommend: Current value only, simpler

3. **Migration from session-based to server-based:** If we're adding preferences now, should old sessions' prefs (if any exist) be preserved?
   - Answer: Likely none exist, fresh feature

4. **Preference TTL for share tokens:** Should token preferences auto-delete when token expires?
   - Recommend: YES, add cleanup job to scheduler
