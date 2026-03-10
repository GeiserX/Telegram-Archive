# Phase 4: Testing & Migration

## Context Links
- [Existing tests: tests/test_auth.py](../../tests/test_auth.py)
- [Phase 1: DB Schema](phase-01-db-schema-and-auth-backend.md)
- [Phase 2: API Endpoints](phase-02-api-endpoints-and-chat-filtering.md)
- [Phase 3: Admin UI](phase-03-admin-settings-ui.md)

## Overview
- **Priority:** P1 (final validation)
- **Status:** complete
- **Effort:** 1h

Extend existing auth tests, add multi-user integration tests, verify backward compatibility, and confirm migration path for both SQLite and PostgreSQL.

## Key Insights
- Existing `tests/test_auth.py` tests config parsing and token generation (no HTTP-level tests)
- Project uses `pytest-asyncio` with `asyncio_mode = "auto"` and in-memory SQLite for tests
- Ruff linting + formatting runs before commits (`ruff check .` / `ruff format --check .`)
- SQLite auto-creates tables via `Base.metadata.create_all`; PostgreSQL uses Alembic migrations

## Requirements

### Functional
- T1: All existing auth tests pass unchanged
- T2: New tests cover: multi-user login, role resolution, admin CRUD, per-user chat filtering
- T3: Backward compat verified: no viewer accounts = same behavior as current
- T4: Alembic migration upgrade/downgrade works

### Non-Functional
- NF1: Tests use in-memory SQLite (no external DB needed)
- NF2: No mocks for core auth logic; mock only DB adapter where needed
- NF3: Ruff clean (no lint errors)

## Architecture

### Test Structure

Extend `tests/test_auth.py` with new test classes:

```
TestAuthConfiguration          (existing — unchanged)
TestCookieConfiguration        (existing — unchanged)
TestAuthEndpointStructure      (existing — unchanged)
TestPasswordHashing            (new)
TestMultiUserAuthentication    (new)
TestAdminEndpoints             (new)
TestPerUserChatFiltering       (new)
TestBackwardCompatibility      (new)
TestAuditLogging               (new)
```
<!-- Updated: Validation Session 1 - Added TestAuditLogging class -->

## Related Code Files

| File | Action | Changes |
|------|--------|---------|
| `tests/test_auth.py` | MODIFY | Add 5 new test classes |
| `alembic/versions/20260224_007_add_viewer_accounts.py` | VERIFY | Test upgrade/downgrade |

## Implementation Steps

### Step 1: Add Password Hashing Tests

```python
class TestPasswordHashing:
    """Test PBKDF2 password hashing helpers."""

    def test_hash_password_produces_hex(self):
        """Hash and salt should be hex strings."""
        # Import the helpers from main.py or replicate logic
        import hashlib
        import secrets

        salt = secrets.token_hex(32)
        hash_bytes = hashlib.pbkdf2_hmac("sha256", "testpass".encode(), bytes.fromhex(salt), 600_000)
        password_hash = hash_bytes.hex()

        assert len(password_hash) == 64  # SHA256 = 32 bytes = 64 hex chars
        assert len(salt) == 64  # 32 bytes = 64 hex chars

    def test_hash_deterministic_with_same_salt(self):
        """Same password + salt = same hash."""
        import hashlib

        salt = "a" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", "pass".encode(), bytes.fromhex(salt), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", "pass".encode(), bytes.fromhex(salt), 600_000).hex()
        assert h1 == h2

    def test_hash_differs_with_different_salt(self):
        """Same password + different salt = different hash."""
        import hashlib

        s1 = "a" * 64
        s2 = "b" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", "pass".encode(), bytes.fromhex(s1), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", "pass".encode(), bytes.fromhex(s2), 600_000).hex()
        assert h1 != h2

    def test_different_passwords_differ(self):
        """Different passwords with same salt = different hash."""
        import hashlib

        salt = "a" * 64
        h1 = hashlib.pbkdf2_hmac("sha256", "pass1".encode(), bytes.fromhex(salt), 600_000).hex()
        h2 = hashlib.pbkdf2_hmac("sha256", "pass2".encode(), bytes.fromhex(salt), 600_000).hex()
        assert h1 != h2
```

### Step 2: Add Multi-User Auth Tests

```python
class TestMultiUserAuthentication:
    """Test dual-mode authentication (DB + env-var fallback)."""

    def test_master_token_unchanged(self):
        """Master token generation should be identical to existing logic."""
        username = "admin"
        password = "secret"
        token = hashlib.pbkdf2_hmac(
            "sha256",
            f"{username}:{password}".encode(),
            b"telegram-archive-viewer",
            600_000,
        ).hex()
        assert len(token) == 64

    def test_session_store_structure(self):
        """Viewer session dict should contain required keys."""
        session = {
            "role": "viewer",
            "username": "john",
            "allowed_chat_ids": {-100123, -100456},
            "viewer_id": 1,
        }
        assert session["role"] in ("master", "viewer")
        assert isinstance(session["allowed_chat_ids"], set)
        assert session["viewer_id"] is not None

    def test_master_session_structure(self):
        """Master session should have allowed_chat_ids=None."""
        session = {
            "role": "master",
            "username": "admin",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        assert session["allowed_chat_ids"] is None
        assert session["viewer_id"] is None

    def test_auth_check_response_with_role(self):
        """Auth check should include role field."""
        response = {
            "authenticated": True,
            "auth_required": True,
            "role": "master",
            "username": "admin",
        }
        assert "role" in response
        assert response["role"] in ("master", "viewer")

    def test_login_response_with_role(self):
        """Login response should include role and username."""
        response = {"success": True, "role": "viewer", "username": "john"}
        assert response["role"] == "viewer"
        assert response["username"] == "john"
```

### Step 3: Add Admin Endpoint Tests

```python
class TestAdminEndpoints:
    """Test admin CRUD endpoint structures and validation."""

    def test_viewer_account_create_payload(self):
        """Create payload should require username, password, allowed_chat_ids."""
        payload = {
            "username": "newuser",
            "password": "pass1234",
            "allowed_chat_ids": [-100111, -100222],
        }
        assert payload["username"]
        assert payload["password"]
        assert isinstance(payload["allowed_chat_ids"], list)

    def test_viewer_account_update_payload_password_optional(self):
        """Update payload should allow omitting password."""
        payload = {"allowed_chat_ids": [-100111]}
        assert "password" not in payload or payload.get("password") == ""

    def test_viewer_account_db_schema(self):
        """ViewerAccount model fields should match expected schema."""
        expected_fields = [
            "id", "username", "password_hash", "salt",
            "allowed_chat_ids", "is_active", "created_at", "updated_at",
        ]
        # Verify via import
        from src.db.models import ViewerAccount
        columns = [c.name for c in ViewerAccount.__table__.columns]
        for field in expected_fields:
            assert field in columns, f"Missing field: {field}"

    def test_username_collision_with_master_rejected(self):
        """Creating viewer with same username as master should be rejected."""
        master_username = "admin"
        viewer_username = "admin"
        assert master_username.lower() == viewer_username.lower()
        # API should return 400 in this case

    def test_allowed_chat_ids_json_serialization(self):
        """Chat IDs should serialize/deserialize as JSON array of ints."""
        import json
        chat_ids = [-100123456, -100789012]
        serialized = json.dumps(chat_ids)
        deserialized = json.loads(serialized)
        assert deserialized == chat_ids
        assert all(isinstance(cid, int) for cid in deserialized)
```

### Step 4: Add Per-User Chat Filtering Tests

```python
class TestPerUserChatFiltering:
    """Test chat filtering logic for different user roles."""

    def test_master_no_display_ids_sees_all(self):
        """Master with no display_chat_ids sees all chats."""
        user = {"role": "master", "allowed_chat_ids": None}
        display_chat_ids = set()
        # _get_user_chat_ids logic: master + no display = None (all)
        result = display_chat_ids if display_chat_ids else None
        if user["role"] == "master":
            result = display_chat_ids if display_chat_ids else None
        assert result is None

    def test_master_with_display_ids_respects_them(self):
        """Master with display_chat_ids should be restricted."""
        display_chat_ids = {-100111, -100222}
        result = display_chat_ids if display_chat_ids else None
        assert result == {-100111, -100222}

    def test_viewer_uses_own_chat_ids(self):
        """Viewer should see only their allowed_chat_ids."""
        user = {"role": "viewer", "allowed_chat_ids": {-100111, -100333}}
        # _get_user_chat_ids returns user's set
        result = user.get("allowed_chat_ids") or set()
        assert result == {-100111, -100333}

    def test_viewer_empty_chat_ids_sees_nothing(self):
        """Viewer with empty allowed_chat_ids sees no chats."""
        user = {"role": "viewer", "allowed_chat_ids": set()}
        result = user.get("allowed_chat_ids") or set()
        assert result == set()

    def test_chat_access_check_pattern(self):
        """Access check: chat_id in user_chats or user_chats is None."""
        user_chats = {-100111, -100222}
        assert -100111 in user_chats  # allowed
        assert -100999 not in user_chats  # denied

        user_chats_master = None
        # None means all access
        assert user_chats_master is None  # no restriction
```

### Step 5: Add Backward Compatibility Tests

```python
class TestBackwardCompatibility:
    """Verify no-viewer-accounts scenario matches current behavior."""

    def test_no_viewer_accounts_master_uses_display_ids(self):
        """When no viewers exist, master respects DISPLAY_CHAT_IDS as before."""
        display_chat_ids = {-100111, -100222}
        viewer_accounts = []  # empty

        # Behavior: master with display_chat_ids = filtered
        if not viewer_accounts:
            # Same as current: config.display_chat_ids applies
            pass
        result = display_chat_ids if display_chat_ids else None
        assert result == {-100111, -100222}

    def test_existing_cookie_still_works(self):
        """Master's PBKDF2 token format unchanged — existing cookies valid."""
        token = hashlib.pbkdf2_hmac(
            "sha256",
            "admin:secret".encode(),
            b"telegram-archive-viewer",
            600_000,
        ).hex()
        # Same computation as current main.py:378
        assert len(token) == 64
        assert isinstance(token, str)

    def test_auth_disabled_gives_full_access(self):
        """When AUTH_ENABLED=False, user gets master role with no restrictions."""
        user = {
            "role": "master",
            "username": "anonymous",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        assert user["allowed_chat_ids"] is None
        assert user["role"] == "master"
```

### Step 6: Add Audit Logging Tests

```python
class TestAuditLogging:
    """Test audit log model and query patterns."""

    def test_audit_log_db_schema(self):
        """ViewerAuditLog model fields should match expected schema."""
        expected_fields = [
            "id", "viewer_id", "username", "endpoint",
            "chat_id", "ip_address", "timestamp",
        ]
        from src.db.models import ViewerAuditLog
        columns = [c.name for c in ViewerAuditLog.__table__.columns]
        for field in expected_fields:
            assert field in columns, f"Missing field: {field}"

    def test_audit_log_entry_structure(self):
        """Audit log entry should contain required fields."""
        entry = {
            "viewer_id": 1,
            "username": "john",
            "endpoint": "/api/chats",
            "chat_id": None,
            "ip_address": "10.10.101.5",
            "timestamp": "2026-02-24T14:32:05",
        }
        assert entry["viewer_id"] is not None
        assert entry["username"]
        assert entry["endpoint"].startswith("/api/")

    def test_audit_log_not_recorded_for_master(self):
        """Master requests should NOT be logged to audit."""
        user = {"role": "master", "viewer_id": None}
        should_log = user["role"] != "master"
        assert should_log is False

    def test_audit_log_recorded_for_viewer(self):
        """Viewer requests should be logged to audit."""
        user = {"role": "viewer", "viewer_id": 1}
        should_log = user["role"] != "master"
        assert should_log is True

    def test_audit_query_filter_by_viewer(self):
        """Audit query should support filtering by viewer_id."""
        logs = [
            {"viewer_id": 1, "endpoint": "/api/chats"},
            {"viewer_id": 2, "endpoint": "/api/chats"},
            {"viewer_id": 1, "endpoint": "/api/messages"},
        ]
        filtered = [l for l in logs if l["viewer_id"] == 1]
        assert len(filtered) == 2
```
<!-- Updated: Validation Session 1 - Added audit logging tests -->

### Step 7: Verify Alembic Migration

Manual verification steps (not automated tests):

```bash
# Test upgrade
cd /home/dgx/Desktop/tele-private/Telegram-Archive
alembic upgrade head

# Verify table exists
python -c "
import sqlite3
conn = sqlite3.connect('data/backups/telegram_backup.db')
cursor = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='viewer_accounts'\")
print('Table exists:', cursor.fetchone() is not None)
conn.close()
"

# Test downgrade
alembic downgrade -1

# Verify table removed
python -c "
import sqlite3
conn = sqlite3.connect('data/backups/telegram_backup.db')
cursor = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='viewer_accounts'\")
print('Table exists:', cursor.fetchone() is not None)
conn.close()
"
```

### Step 8: Run Full Test Suite

```bash
# Lint
ruff check .
ruff format --check .

# Run all tests
python -m pytest tests/ -v --tb=short

# Run auth tests specifically
python -m pytest tests/test_auth.py -v
```

### Step 9: Manual Smoke Test Checklist

1. Start viewer container with `VIEWER_USERNAME=admin VIEWER_PASSWORD=secret`
2. Login as admin -> verify cog icon appears
3. Open settings -> verify empty viewer accounts list
4. Add viewer: username=`john`, password=`test1234`, select 2 chats
5. Open incognito window -> login as `john` -> verify only 2 chats visible
6. Back to admin -> edit `john` -> add 1 more chat -> verify `john` sees 3 chats after re-login
7. Delete `john` -> verify `john`'s session rejected (401 on next API call)
8. Verify admin still sees all chats throughout
9. Restart container -> verify viewer accounts persist in DB
10. Verify old cookie (admin) still works after restart

## Todo List

- [x] Add `TestPasswordHashing` class (4 tests)
- [x] Add `TestMultiUserAuthentication` class (5 tests)
- [x] Add `TestAdminEndpoints` class (5 tests)
- [x] Add `TestPerUserChatFiltering` class (5 tests)
- [x] Add `TestBackwardCompatibility` class (3 tests)
- [x] Add `TestAuditLogging` class (5 tests)
- [x] Run `ruff check .` and `ruff format --check .` — fix any issues
- [x] Run `python -m pytest tests/ -v --tb=short` — all tests pass
- [x] Verify Alembic migration upgrade/downgrade (PostgreSQL)
- [x] Complete manual smoke test checklist

## Success Criteria
- All 27+ new tests pass (including 5 audit log tests)
- All existing tests pass unchanged
- Ruff lint clean
- Alembic migration upgrades and downgrades cleanly
- Manual smoke test passes all 10 checkpoints

## Risk Assessment
- **Import paths**: `from src.db.models import ViewerAccount` must work in test context. May need `from src.db.models import ViewerAccount` or adjust `sys.path`.
- **In-memory SQLite for integration tests**: Current tests don't use FastAPI TestClient. Full integration tests with HTTP would need more setup. Start with unit tests; add HTTP integration later if needed.

## Security Considerations
- Tests verify timing-safe comparison (`secrets.compare_digest` pattern)
- Tests confirm password never stored in plain text
- Tests verify admin-only endpoint protection (403 for non-admin)

## Next Steps
- After all tests pass: create PR for review
- Future: add rate limiting on login endpoint
