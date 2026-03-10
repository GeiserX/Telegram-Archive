# Code Review: Multi-User Viewer Access Control

## Scope
- **Files**: `src/db/models.py`, `src/db/adapter.py`, `src/web/main.py`, `src/web/templates/index.html`, `tests/test_auth.py`, `alembic/versions/20260224_007_add_viewer_accounts.py`
- **Focus**: Security (password hashing, access control), correctness (per-user filtering, backward compat), code quality, bugs
- **LOC changed**: ~600 (backend) + ~250 (frontend) + ~340 (tests)
- **Plan**: `plans/260224-0318-multi-user-viewer-access-control/plan.md`

## Overall Assessment

Solid implementation of multi-user access control with good architectural decisions: PBKDF2-SHA256 (600k iterations), timing-safe comparison, per-user session management, and clean separation of master vs viewer roles. The ORM models, migration, and admin UI are well-structured.

However, there are **2 critical issues** (CORS blocking PUT/DELETE, DISPLAY_CHAT_IDS regression for deployments that rely on it), **3 high-priority issues** (session memory leak, overly permissive adapter method, missing integration tests), and several medium items.

---

## Critical Issues

### C1. CORS middleware blocks PUT and DELETE requests

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` line 332

The CORS middleware only allows `["GET", "POST"]` methods:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["GET", "POST"],  # <-- Missing PUT, DELETE
    allow_headers=["*"],
)
```

The new admin endpoints use PUT (`/api/admin/viewers/{id}`) and DELETE (`/api/admin/viewers/{id}`). When the viewer is served from a different origin (e.g., behind a reverse proxy with a different domain), preflight CORS requests for PUT/DELETE will be rejected by the browser.

**Impact**: Admin viewer management (edit/delete) will fail silently in cross-origin deployments.

**Fix**:
```python
allow_methods=["GET", "POST", "PUT", "DELETE"],
```

### C2. DISPLAY_CHAT_IDS regression -- breaking change for existing users

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` lines 447-458

The `_get_user_chat_ids()` for master role always returns `None` (all chats):
```python
def _get_user_chat_ids(request: Request) -> set[int] | None:
    if user["role"] == "master":
        return None  # master sees ALL chats
```

Previously, `config.display_chat_ids` restricted which chats the master/single-user saw in the viewer. This was used by deployments that expose the viewer publicly and want to restrict visible chats via env var.

**Impact**: Existing deployments using `DISPLAY_CHAT_IDS` will suddenly expose ALL backed-up chats to the master user after upgrade.

The plan explicitly documents this as a deliberate decision (plan.md Q1: "Master always sees ALL chats"), but **no migration path or deprecation warning** exists. The `DISPLAY_CHAT_IDS` env var is documented in README.md line 266 and docker-compose examples.

**Recommendation**: At minimum, log a WARNING at startup when `DISPLAY_CHAT_IDS` is set, informing the user it no longer restricts the master view and suggesting they create viewer accounts for restricted access. Alternatively, apply `DISPLAY_CHAT_IDS` as the default `allowed_chat_ids` for the master role until viewer accounts are created.

---

## High Priority

### H1. In-memory session store grows without bound (memory leak)

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` line 405

```python
_viewer_sessions: dict[str, dict] = {}
```

Viewer sessions are added on each login but only removed on explicit logout or admin update/delete. If a viewer logs in repeatedly (e.g., from different browsers/devices), each login generates a new `secrets.token_hex(32)` token that stays in memory forever.

**Impact**: In long-running containers (weeks/months), the session dict will accumulate stale entries.

**Fix**: Add a TTL to sessions (e.g., store `created_at` timestamp and evict sessions older than `AUTH_SESSION_DAYS` during auth checks), or cap sessions per viewer_id.

### H2. `update_viewer_account` adapter accepts arbitrary kwargs

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` lines 1900-1912

```python
async def update_viewer_account(self, account_id: int, **kwargs) -> bool:
    for key, value in kwargs.items():
        if hasattr(account, key):
            setattr(account, key, value)
```

Any attribute on the `ViewerAccount` model can be overwritten, including `id`, `username`, `created_at`. While the web endpoint currently whitelists fields, a future caller could accidentally pass unsanitized data.

**Fix**: Whitelist allowed fields:
```python
UPDATABLE_FIELDS = {"password_hash", "salt", "allowed_chat_ids", "is_active"}
for key, value in kwargs.items():
    if key in UPDATABLE_FIELDS:
        setattr(account, key, value)
```

### H3. Tests are pure data-structure validations, no integration coverage

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/tests/test_auth.py`

All 35 tests assert static data structures (dicts, strings, model column names). None test:
- Actual `_hash_password` / `_verify_password` functions from `main.py`
- Login flow against a test FastAPI client
- Admin CRUD endpoints with httpx AsyncClient
- Per-user chat filtering against a real in-memory database
- Session invalidation on viewer update/delete

**Impact**: Bugs in the actual auth flow (e.g., the CORS issue) would not be caught.

**Recommendation**: Add integration tests using `httpx.AsyncClient` with the FastAPI `TestClient` pattern. At minimum, test login, admin CRUD, and per-user filtering with a real in-memory SQLite database.

---

## Medium Priority

### M1. `except ValueError, TypeError:` syntax -- works but confusing

**Files**:
- `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` lines 1340, 1372
- `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` lines 916, 1138, 1513

In Python 3.14 this syntax `except ValueError, TypeError:` is parsed as `except (ValueError, TypeError):` (catches both), which is correct behavior. However, this syntax is confusing and non-standard -- the canonical form is:
```python
except (ValueError, TypeError):
```

**Note**: These instances in `adapter.py` lines 916, 1138, 1513 appear to be **pre-existing** code, not introduced by this PR. The two instances in `main.py` (lines 1340, 1372) are new.

### M2. No login rate limiting / brute force protection

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` lines 555-633

The `/api/login` endpoint has no rate limiting. An attacker can attempt unlimited password guesses. PBKDF2 with 600k iterations provides some protection (slow computation), but an automated attack could still try thousands of common passwords per hour.

**Recommendation**: Add per-IP rate limiting (e.g., max 5 failed attempts per minute) or exponential backoff on failures. This could be a simple in-memory counter.

### M3. Audit log has no retention policy

**Files**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py`, `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/models.py`

The `viewer_audit_log` table grows unbounded. Every viewer API request inserts a row (including every message page load, every chat switch). No cleanup mechanism exists.

**Recommendation**: Add a periodic cleanup task (e.g., delete entries older than 90 days) in the `stats_calculation_scheduler` or similar background task.

### M4. `ViewerAuditLog.viewer_id` has no foreign key to `ViewerAccount`

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/models.py` line 356

```python
viewer_id: Mapped[int] = mapped_column(Integer, nullable=False)
```

No `ForeignKey("viewer_accounts.id")` is defined. While this allows audit entries to survive after a viewer account is deleted (which may be intentional for forensics), it also means there's no referential integrity check. Consider adding `ForeignKey(..., ondelete="SET NULL")` with a nullable column, or document the intentional omission.

### M5. Adapter methods use `get_session()` instead of `async_session_factory()`

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` lines 1846, 1867, 1886, 1902, 1917, 1931, 1945

The new viewer account methods use `self.db_manager.get_session()` while all existing methods use `self.db_manager.async_session_factory()`:

```python
# New methods (inconsistent):
async with self.db_manager.get_session() as session:

# Existing pattern throughout adapter.py:
async with self.db_manager.async_session_factory() as session:
```

Both work, but `get_session()` auto-commits on exit while `async_session_factory()` does not. The new methods also call `await session.commit()` explicitly, creating a double-commit scenario (the explicit commit followed by `get_session`'s implicit commit). This is harmless (second commit is a no-op) but wasteful and inconsistent.

**Fix**: Use `self.db_manager.async_session_factory()` for consistency with the rest of the adapter.

### M6. Password minimum length is only 4 characters

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` line 1326

```python
if len(password) < 4:
    raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
```

4 characters is very weak even with PBKDF2. OWASP recommends minimum 8 characters.

---

## Low Priority

### L1. `openSettings` loads data sequentially instead of in parallel

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html` lines 2745-2748

```javascript
const openSettings = async () => {
    showSettings.value = true
    await loadViewerAccounts()
    await loadAllChatsAdmin()  // waits for first to finish
}
```

These two API calls are independent and could run in parallel:
```javascript
await Promise.all([loadViewerAccounts(), loadAllChatsAdmin()])
```

### L2. Audit log `total` count is just `len(entries)`, not actual DB count

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` line 1449

```python
return {"entries": entries, "total": len(entries)}
```

When `limit=100`, `total` will always be <= 100, not the actual total count. This makes pagination impossible. Should add a separate COUNT query if pagination is desired.

### L3. Settings panel hardcodes "admin" badge, doesn't show viewer's own role

**File**: `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html` line 633

```html
<span class="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 ml-1">admin</span>
```

This is always visible since settings are only shown for master role, so it's correct. No change needed.

---

## Positive Observations

1. **Password hashing**: PBKDF2-SHA256 with 600k iterations and per-user random salt is the OWASP-recommended approach. Using `secrets.compare_digest` for timing-safe comparison prevents timing attacks.

2. **Session token generation**: `secrets.token_hex(32)` (256-bit) is cryptographically secure and appropriately sized.

3. **Master username collision prevention**: Rejecting viewer accounts with the same username as the master env-var user prevents auth ambiguity.

4. **Session invalidation on admin changes**: When a viewer account is updated or deleted, all their active sessions are immediately revoked.

5. **Audit logging scoped to viewers only**: Master actions are not logged, reducing noise while providing accountability for shared accounts.

6. **Clean ORM model design**: `ViewerAccount` and `ViewerAuditLog` follow existing model patterns with proper indexes.

7. **Alembic migration**: Clean up/down migration with proper index management.

8. **Frontend UI**: Well-structured settings panel with tab navigation, inline search for chat picker, confirmation modals for destructive actions.

---

## Recommended Actions (prioritized)

1. **[Critical]** Add PUT and DELETE to CORS `allow_methods`
2. **[Critical]** Add deprecation warning for `DISPLAY_CHAT_IDS` at startup when env var is set
3. **[High]** Add TTL or eviction to `_viewer_sessions` dict
4. **[High]** Whitelist updatable fields in `update_viewer_account`
5. **[High]** Add integration tests with FastAPI TestClient and real DB
6. **[Medium]** Use parenthesized `except (ValueError, TypeError):` form in new code
7. **[Medium]** Add audit log retention/cleanup mechanism
8. **[Medium]** Switch new adapter methods from `get_session()` to `async_session_factory()`
9. **[Low]** Consider increasing minimum password length to 8

---

## Metrics

- **Type Coverage**: N/A (Python, no type checker in CI)
- **Test Coverage**: 35 tests, all pass. Coverage is structural only (no integration).
- **Linting Issues**: 0 (ruff check + ruff format pass)
- **Build Status**: All 105 tests pass

---

## Unresolved Questions

1. **DISPLAY_CHAT_IDS deprecation path**: Should existing deployments be warned at startup, or should the env var continue to work as a "default viewer account" chat filter until explicit viewer accounts are created?

2. **Session persistence across restarts**: Plan says "in-memory is fine" and sessions lost on restart is acceptable. But for deployments with many viewer users, should this be reconsidered? (Low priority, acknowledged in plan.)

3. **Audit log for master actions**: The plan excludes master from audit logging. Should there be a separate admin audit trail for security-sensitive operations like creating/deleting viewer accounts?
