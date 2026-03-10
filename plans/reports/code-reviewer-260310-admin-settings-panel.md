# Code Review: v7.3.0 Admin Settings Panel

**Date:** 2026-03-10
**Reviewer:** code-reviewer
**Scope:** 7 files (models, adapter, web endpoints, scheduler, frontend template, tests, migration)

---

## Overall Assessment

Feature is well-structured and follows established patterns. Auth guards use existing `require_admin`/`require_auth` dependencies consistently. However, there are several bugs across security, logic, and frontend layers that need attention before shipping.

---

## Critical Issues

### C1. Test file uses wrong PBKDF2 iteration count (Silent verification mismatch)

**File:** `/home/phenix/projects/tele-private/repo/dev/tests/test_admin_settings.py` lines 96-102

Production `_hash_password` in `main.py` uses **600,000** iterations (`bytes.fromhex(salt)` as salt bytes). The test reimplements hashing with **100,000** iterations and passes `salt.encode()` (string bytes, not hex-decoded). This means:
- Tests pass internally (roundtrip works) but the test does NOT validate compatibility with production hashing.
- If someone uses test helpers to generate fixture data, passwords will never verify in production.

**Fix:** Import `_hash_password` and `_verify_password` from `src.web.main` instead of reimplementing. Alternatively, match the exact parameters: 600,000 iterations and `bytes.fromhex(salt)`.

### C2. Cron injection -- no semantic validation of cron field values

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/main.py` lines 1631-1635

The `PUT /api/admin/settings` endpoint only checks that the cron string has 5 space-separated parts. It does NOT validate that each part is a legal cron field. Malformed cron expressions (e.g., `"AAAA BBBB CCCC DDDD EEEE"`) will be stored in the DB and crash the scheduler when `CronTrigger()` is called in `_poll_settings`.

Since the scheduler catches the exception and logs it, this won't bring down the process -- but it will silently prevent any schedule change from taking effect after a bad value is stored. The only recovery is a manual DB edit.

**Fix:** Wrap `CronTrigger(...)` in a try/except in the endpoint and return 400 on failure:
```python
try:
    CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])
except Exception:
    raise HTTPException(status_code=400, detail="Invalid cron expression")
```

---

## High Priority

### H1. `set_setting` race condition (TOCTOU) on concurrent upsert

**File:** `/home/phenix/projects/tele-private/repo/dev/src/db/adapter.py` lines 2081-2091

The method does SELECT then INSERT/UPDATE in sequence. Under concurrent requests (two viewers pinging `/api/activity/ping` simultaneously), both can see `row is None` and both attempt INSERT, causing a unique constraint violation on the `key` primary key.

The existing codebase uses dialect-specific `on_conflict_do_update` (see other upsert methods in adapter.py). This method should too, or use a database-level UPSERT. The `retry_on_locked` decorator helps with SQLite locking but won't prevent the PK violation on PostgreSQL.

**Fix:** Use `INSERT ... ON CONFLICT DO UPDATE` like other upsert methods in the adapter, branching on dialect.

### H2. `_run_backup_job` writes `backup_status: "idle"` even on failure

**File:** `/home/phenix/projects/tele-private/repo/dev/src/scheduler.py` lines 99-107

The `finally` block unconditionally sets status to `"idle"` and `last_backup_completed_at` to now, even when the backup threw an exception. This gives the admin UI a false "all-clear" signal after a failed backup.

**Fix:** Track success and set `backup_status` to `"failed"` in the except branch. Only set `last_backup_completed_at` on success.

```python
except Exception as e:
    logger.error(f"Scheduled backup failed: {e}", exc_info=True)
    if self._db:
        await self._db.set_setting("backup_status", "failed")
        await self._db.set_setting("last_backup_error", str(e)[:500])
finally:
    # Only set completed on success path (move above the except)
```

### H3. Active User Boost toggle stores boolean as JS string, not "true"/"false"

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/templates/index.html` line 1800

```html
@click="activeUserBoost = !activeUserBoost; localStorage.setItem('tg-active-boost', activeUserBoost)"
```

At the time `localStorage.setItem` runs, `activeUserBoost` is a Vue ref. The template expression `activeUserBoost` in an `@click` handler gives the ref's `.value`, but after the assignment `activeUserBoost = !activeUserBoost` the new value is already a boolean (`true`/`false`). The issue is that `localStorage.setItem` calls `.toString()`, so this stores `"true"` or `"false"`, which matches the initialization on line 2002: `localStorage.getItem('tg-active-boost') === 'true'`. So this is actually correct by accident, but the pattern is fragile.

However, the real bug is that the **heartbeat never starts when toggled ON after initial load**. `startActivityPing()` is called once on mount (line 2551). If the user toggles it ON later, the interval is already running but gated by `activeUserBoost.value` check. That part works. But if the user had it OFF at page load, `startActivityPing()` still starts the interval -- it just checks the flag each tick. So this is actually fine.

**Downgrade:** This is not a bug, the pattern works. Removing from findings.

### H4. `active_viewers_count` only increments, never decrements

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/main.py` lines 1659-1660

```python
current = await db.get_setting("active_viewers_count") or "0"
await db.set_setting("active_viewers_count", str(max(1, int(current))))
```

This sets the count to `max(1, current)`, meaning it will always be >= 1 and never decrease. The count is functionally useless -- it can only go up. The deadline-based approach (`active_viewers_deadline`) is what the scheduler actually checks, so this count is misleading but harmless.

**Fix:** Either remove `active_viewers_count` or implement proper tracking (e.g., count distinct sessions that pinged in the last 2 minutes).

---

## Medium Priority

### M1. `_poll_settings` does not guard against `self._db` being None after initialization failure

**File:** `/home/phenix/projects/tele-private/repo/dev/src/scheduler.py` lines 211-251

The method accesses `self._db.get_setting(...)` without checking if `self._db` is None. While the caller in `run_forever` only creates the poll task when `self._db` is truthy, if `self._db` were somehow set to None later (e.g., on close), the poll loop would crash.

Currently safe in practice because the poll task is cancelled in the finally block. Low risk, but defensive check costs nothing.

### M2. No error response to client when `PUT /api/admin/settings` receives no whitelisted keys

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/main.py` lines 1626-1636

If a request body contains only keys NOT in `allowed_keys`, the loop silently skips them all and returns `{"success": True}`. The client thinks the setting was saved.

**Fix:** Track whether any key was actually written, return 400 if none.

### M3. Backup schedule UI visible only to master, but `/api/backup-status` is accessible to all authenticated users

This is by design (the comment says so), but viewers can see the cron schedule in the response at line 1645-1650. If the schedule is considered admin-only information, this is a minor information leak. If intentional, no action needed.

### M4. Migration does not set `onupdate` for `updated_at` column

**File:** `/home/phenix/projects/tele-private/repo/dev/alembic/versions/20260310_011_add_app_settings.py` line 33

The migration creates `updated_at` with `server_default=sa.func.now()` but does not include an `onupdate` clause. The ORM model has `onupdate=datetime.utcnow` which handles this at the Python level, but direct SQL updates to the table (e.g., from the backup container which may not use the ORM) will not auto-update this column.

For PostgreSQL, consider adding a trigger or using `server_onupdate` if direct SQL writes are expected.

### M5. `moment(d.last_completed_at).fromNow()` parses without timezone context

**File:** `/home/phenix/projects/tele-private/repo/dev/src/web/templates/index.html` line 4239

```js
lastBackupCompleted.value = d.last_completed_at
    ? moment(d.last_completed_at).fromNow()
    : null
```

`last_backup_completed_at` is stored as `datetime.utcnow().isoformat()` (no `Z` suffix). Moment.js will parse this as **local time**, not UTC. This means "2 minutes ago" could be off by the user's timezone offset (e.g., 5 hours).

**Fix:** Parse explicitly as UTC: `moment.utc(d.last_completed_at).fromNow()`

---

## Low Priority

### L1. `ViewerSession` import missing in adapter.py

**File:** `/home/phenix/projects/tele-private/repo/dev/src/db/adapter.py` line 27-42

`ViewerSession` is imported in `main.py` for session persistence but is not in the adapter's import list. This is not a bug (adapter doesn't need it), but noted for completeness.

### L2. `datetime.utcnow()` is deprecated in Python 3.12+

Multiple files use `datetime.utcnow()`. In Python 3.14 (project target), this emits a deprecation warning. Prefer `datetime.now(datetime.UTC)`. Not blocking but will generate log noise.

---

## Edge Cases Found by Scouting

1. **Two scheduler instances (backup + viewer containers):** If both containers initialize `get_adapter()` and write to `app_settings`, the TOCTOU in `set_setting` becomes a real concurrent issue on PostgreSQL.
2. **Malformed `active_viewers_deadline` ISO string:** `datetime.fromisoformat()` in `_poll_settings` (line 231) will throw on corrupt data. Caught by the broad except, but worth a targeted try/except with a warning log.
3. **Empty cron from custom input:** Frontend `saveBackupSchedule` sends `customCron.value` which could be empty string if user selects "Custom cron..." but types nothing. Backend would reject (0 parts != 5), but UX gives no hint about the format.

---

## Positive Observations

- Auth gates are consistent: `require_admin` for write operations, `require_auth` for read-only status.
- Settings whitelist (`allowed_keys = {"backup_schedule"}`) prevents arbitrary key injection. Good defense-in-depth.
- Scheduler polling loop has broad exception handling, preventing a single bad setting from killing the loop.
- Theme flash prevention (inline `<script>` in `<head>`) is a good UX pattern.
- Migration uses `IF NOT EXISTS` check, making it idempotent.
- Frontend reactivity is correct: all state is properly wrapped in `ref()`, returned from setup, and used in template.

---

## Summary of Required Actions

| Priority | Issue | File | Action |
|----------|-------|------|--------|
| CRITICAL | C1 | test_admin_settings.py | Fix iteration count & salt encoding to match production |
| CRITICAL | C2 | main.py:1631 | Validate cron with CronTrigger before storing |
| HIGH | H1 | adapter.py:2081 | Use dialect-specific UPSERT for set_setting |
| HIGH | H2 | scheduler.py:99 | Track backup failure, don't write "idle" on error |
| HIGH | H4 | main.py:1659 | Fix active_viewers_count logic or remove it |
| MEDIUM | M2 | main.py:1626 | Return 400 when no whitelisted keys were written |
| MEDIUM | M5 | index.html:4239 | Parse last_completed_at as UTC with moment.utc() |
| LOW | L2 | multiple | Replace datetime.utcnow() with datetime.now(UTC) |

---

## Metrics

- **Files reviewed:** 7
- **Critical issues:** 2
- **High issues:** 3 (H3 downgraded on analysis)
- **Medium issues:** 5
- **Low issues:** 2
- **Test coverage of new code:** Partial (route existence, model structure, theme HTML checks; no integration tests for actual endpoint behavior)
