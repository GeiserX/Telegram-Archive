# Code Review: Viewer UX & Preferences System

**Date:** 2026-03-10
**Reviewer:** code-reviewer agent
**Branch:** feat/web-viewer-enhancements

## Scope

- **Files reviewed:** 7
  - `src/db/models.py` (436 LOC)
  - `src/db/adapter.py` (~2160 LOC, focused on lines 1900-2160)
  - `src/web/main.py` (~1810 LOC, focused on lines 475-1810)
  - `src/web/templates/index.html` (~5470 LOC, focused on diff areas)
  - `alembic/versions/20260310_012_add_no_download.py` (32 LOC)
  - `tests/test_viewer_preferences.py` (153 LOC)
  - `tests/test_token_auth.py` (211 LOC, updated `test_model_columns`)
- **Focus:** Security, correctness, edge cases, code quality
- **Tests:** 41/41 pass, 3 pre-existing lint warnings

## Overall Assessment

Implementation is mostly solid and follows existing codebase patterns well. Models, adapter, migration, and frontend are consistent. However, there is **one critical positional argument bug** in `_log_viewer_audit` that corrupts every audit log entry, and a few medium-priority issues.

---

## Critical Issues

### 1. `_log_viewer_audit()` positional argument mismatch -- all audit log entries corrupted

**File:** `src/web/main.py`, lines 480, 915, 931, 997, 1050, 1083, 1297, 1321, 1360

**Problem:** Function signature is:
```python
async def _log_viewer_audit(request: Request, action: str = "api_access", chat_id: int | None = None):
```

But every call site passes `chat_id` as the second positional argument:
```python
await _log_viewer_audit(request, chat_id)  # chat_id (int) fills `action` (str)
```

**Impact:**
- `action` column stores the chat ID as a string (e.g., `"-1001234567"`) instead of `"api_access"`
- `chat_id` column is always `None`
- Audit log filtering by action in the frontend will never work
- All existing audit data since deployment is corrupted

**Fix:** Change all 8 call sites to use keyword arguments:
```python
await _log_viewer_audit(request, chat_id=chat_id)
```

---

## High Priority

### 2. Download links not hidden when `noDownload` is active

**File:** `src/web/templates/index.html`, lines 1468-1471, 1476-1489, 1624-1630

**Problem:** The CSS `.no-download` class disables `pointer-events` on `img` and `video` elements, but:
- Document download links (`<a :href="getMediaUrl(msg)" download>`) at lines 1468-1471 and 1476-1489 are still fully functional -- they are `<a>` tags, not `img/video`
- The lightbox download button (line 1625) is not conditionally hidden

The CSS only blocks drag/right-click on images and videos. All explicit download `<a>` links remain clickable.

**Fix:** Add `v-if="!noDownload"` to the download link elements:
```html
<!-- Line 1468 -->
<a v-if="!noDownload" :href="getMediaUrl(msg)" download ...>Download</a>

<!-- Line 1476 -->
<a v-if="!noDownload" :href="getMediaUrl(msg)" download ...>

<!-- Line 1625 (lightbox) -->
<a v-if="lightboxMedia && !noDownload" :href="getMediaUrl(lightboxMedia)" download ...>
```

### 3. Audit log `total` field is misleading

**File:** `src/web/main.py`, line 1558

**Problem:**
```python
return {"entries": entries, "total": len(entries)}
```
`total` always equals `len(entries)` (the page size), not the total count of matching records. The frontend uses `auditHasMore = data.entries.length === 50` for pagination, which works, but `total` is misleading for any consumer expecting a real total count.

**Impact:** Low functional impact since frontend ignores `total`, but API contract is misleading.

**Fix:** Either remove `total` or run a `SELECT COUNT(*)` query with the same filters and return the actual count.

---

## Medium Priority

### 4. `getattr(account, "no_download", 1)` used on ORM objects

**File:** `src/db/adapter.py`, lines 1918, 1935, 2106, 2154

**Problem:** Uses `getattr(account, "no_download", 1)` as a defensive fallback, implying the column might not exist on the ORM object. Since `no_download` is now a defined column on both `ViewerAccount` and `ViewerToken` models, `getattr` is unnecessary defensive code. After migration 012 runs, the column always exists.

**Impact:** No functional issue. But it obscures bugs -- if the column were accidentally removed from the model, this would silently default to `1` instead of raising an `AttributeError`.

**Recommendation:** Replace `getattr(a, "no_download", 1)` with `a.no_download` throughout, or keep `getattr` only temporarily with a comment explaining it's for pre-migration compatibility.

### 5. Migration column is `nullable=True` but model uses non-nullable mapped column

**File:** `alembic/versions/20260310_012_add_no_download.py` line 25 vs `src/db/models.py` lines 345, 410

**Problem:** The migration adds columns with `nullable=True`, but the ORM model defines:
```python
no_download: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
```
`Mapped[int]` (no `| None`) means SQLAlchemy considers the column NOT NULL at ORM level. The database column is nullable, but the ORM type annotation disagrees.

**Impact:** Pre-existing rows get `NULL` from the migration (server_default applies to new INSERTs only in some databases). The `getattr` defensive code in the adapter masks this. PostgreSQL `ALTER TABLE ADD COLUMN` with `server_default` does apply the default to existing rows, but SQLite `ALTER TABLE ADD COLUMN` does not -- existing rows get `NULL`.

**Fix:** Add a data migration step for SQLite:
```python
def upgrade():
    op.add_column("viewer_accounts", sa.Column("no_download", sa.Integer(), server_default="1", nullable=True))
    op.add_column("viewer_tokens", sa.Column("no_download", sa.Integer(), server_default="1", nullable=True))
    # Backfill existing rows
    op.execute("UPDATE viewer_accounts SET no_download = 1 WHERE no_download IS NULL")
    op.execute("UPDATE viewer_tokens SET no_download = 1 WHERE no_download IS NULL")
```

### 6. `main.py.bak` file should be cleaned up

**File:** `src/web/main.py.bak` (71 KB)

**Problem:** A `.bak` file of `main.py` exists and should not be committed. If `.gitignore` does not exclude `*.bak`, this could end up in the repository.

**Fix:** Delete `src/web/main.py.bak` and add `*.bak` to `.gitignore` if not already present.

### 7. No test for `_log_viewer_audit` behavior

**File:** `tests/test_viewer_preferences.py`

**Problem:** The test suite validates model structure, route existence, and migration files, but does not test any runtime behavior of:
- `_log_viewer_audit` (and would have caught the positional arg bug)
- Audit log creation/retrieval
- `no_download` propagation through login -> session -> auth check

**Recommendation:** Add at minimum:
- A test that creates an audit log entry and retrieves it with correct fields
- A test that verifies `_log_viewer_audit(request, chat_id=123)` produces `action="api_access"` and `chat_id=123`

---

## Low Priority

### 8. Pre-existing `datetime.utcnow()` deprecation in test_token_auth.py

**File:** `tests/test_token_auth.py`, lines 95-101

Python 3.12+ deprecates `datetime.utcnow()`. Four warnings emitted during test run. Use `datetime.now(datetime.UTC)` instead.

### 9. Pre-existing lint: `open()` without context manager in test_token_auth.py

**File:** `tests/test_token_auth.py`, lines 171, 178, 185

Three `SIM115` ruff violations for `open(path).read()` without `with` statement.

### 10. `no_download` boolean coercion inconsistency

**Files:** `src/web/main.py` lines 1443, 1586 vs `src/web/templates/index.html`

Backend converts: `no_download = 1 if data.get("no_download", True) else 0`
Frontend sends: `no_download: newViewerNoDownload.value` (boolean `true`/`false`)

This works because Python's `if True: 1` and `if False: 0` are correct. But the frontend default for `check_auth` response is `"no_download": user.get("no_download", 0)` (default 0 = allowed for master), while viewer defaults to `1` (restricted). This is intentional per the plan, but worth documenting.

---

## Positive Observations

1. **Session invalidation on update/delete** -- When a viewer account or token is updated, active sessions are properly evicted from `_viewer_sessions`. This prevents stale `no_download` flags from persisting.

2. **Defensive `getattr` during migration transition** -- While I flagged it as unnecessary long-term, using `getattr(account, "no_download", 1)` is pragmatic for the transition period before migration 012 runs.

3. **Per-chat backgrounds use localStorage** -- Good architecture decision. No backend storage needed, no migration, graceful degradation if cleared.

4. **Audit log is fire-and-forget** -- `_log_viewer_audit` wraps `create_audit_log` in try/except, so audit failures never block API responses. Good resilience pattern.

5. **Migration has proper downgrade** -- `downgrade()` drops both columns cleanly.

6. **Frontend checkbox defaults** -- All `noDownload` refs default to `true` (restricted), matching the secure-by-default model column `server_default="1"`.

7. **Infinite scroll uses IntersectionObserver with scroll fallback** -- The `handleScroll` function provides a scroll-position-based fallback for when the IntersectionObserver sentinel is not triggered. Good resilience for edge cases with `flex-col-reverse`.

---

## Recommended Actions (Priority Order)

1. **[CRITICAL]** Fix `_log_viewer_audit` calls: change `_log_viewer_audit(request, chat_id)` to `_log_viewer_audit(request, chat_id=chat_id)` at all 8 call sites
2. **[HIGH]** Add `v-if="!noDownload"` to the 3 download `<a>` elements in `index.html`
3. **[MEDIUM]** Add `UPDATE` statements to migration 012 to backfill `NULL` values on existing rows
4. **[MEDIUM]** Delete `src/web/main.py.bak`
5. **[MEDIUM]** Add behavioral test for audit log creation
6. **[LOW]** Fix `total` field in audit endpoint or remove it
7. **[LOW]** Replace `getattr(obj, "no_download", 1)` with direct attribute access post-migration

---

## Metrics

- **Test Coverage:** 41/41 pass (100% of suite). No runtime/integration tests for new audit or no_download behavior.
- **Linting Issues:** 3 pre-existing (SIM115 in test_token_auth.py). 0 new.
- **Type Coverage:** Model types consistent. `no_download` correctly typed as `Mapped[int]`.

## Unresolved Questions

1. Should the `ViewerSession` DB model also include a `no_download` column for full persistence across container restarts? Currently in-memory sessions carry it, but DB-backed sessions do not.
2. Should there be rate limiting on audit log writes to prevent log flooding from automated viewers hitting many endpoints?
