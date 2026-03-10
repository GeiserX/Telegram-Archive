# Phase 6: Testing

**Priority:** HIGH
**Status:** TODO
**Effort:** Medium
**Files:** `tests/test_admin_settings.py`

---

## Overview

Write tests for all new backend functionality. Frontend is tested manually via browser. Focus on: AppSettings model, adapter methods, new endpoints, password change, backup status.

---

## Test Classes

### TestAppSettingsModel
- Model table name is `app_settings`
- Primary key is `key` column (String)
- Has `value` (Text) and `updated_at` (DateTime) columns

### TestAppSettingsAdapter
- `set_setting` stores key-value
- `get_setting` retrieves stored value
- `get_setting` returns None for missing key
- `set_setting` updates existing key (upsert)
- `get_all_settings` returns dict of all settings

### TestPasswordChange
- Verify `_hash_password()` and `_verify_password()` round-trip
- Master password override stored in settings
- Login checks DB override before env var fallback
- Wrong current password returns 403
- Password min length enforced (4 chars)

### TestBackupScheduleValidation
- Valid 5-part cron accepted
- Invalid cron rejected (wrong number of parts)
- Schedule stored in app_settings

### TestActivityPing
- Ping updates active_viewers_deadline
- Deadline is ~2 min in future

### TestBackupStatusEndpoint
- Returns status, last_completed, last_started, schedule
- Works for both master and viewer roles

### TestEndpointRoutes
- `/api/admin/settings` GET and PUT registered
- `/api/backup-status` GET registered
- `/api/activity/ping` POST registered
- `/api/auth/password` PUT registered

---

## Test File Structure

```python
"""Tests for admin settings panel (v7.3.0)."""
import json, secrets, hashlib
from datetime import datetime, timedelta
import pytest

class TestAppSettingsModel:
    def test_model_table_name(self): ...
    def test_model_columns(self): ...
    def test_primary_key_is_string(self): ...

class TestCronValidation:
    def test_valid_cron_5_parts(self): ...
    def test_invalid_cron_too_few_parts(self): ...
    def test_common_presets(self): ...

class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self): ...
    def test_wrong_password_fails(self): ...
    def test_min_length_validation(self): ...

class TestEndpointRoutes:
    def test_settings_routes_exist(self): ...
    def test_backup_status_route_exists(self): ...
    def test_activity_ping_route_exists(self): ...
    def test_password_change_route_exists(self): ...

class TestThemePresets:
    """Test that theme CSS variables are well-formed."""
    def test_all_six_themes_defined(self): ...
    def test_theme_has_required_variables(self): ...
```

---

## Todo

- [ ] Write TestAppSettingsModel
- [ ] Write TestCronValidation
- [ ] Write TestPasswordHashing
- [ ] Write TestEndpointRoutes
- [ ] Write TestThemePresets (parse HTML for CSS vars)
- [ ] Run all tests: `python -m pytest tests/test_admin_settings.py -v`
- [ ] Verify no regressions: `python -m pytest tests/ -v --ignore=tests/test_multi_user_auth.py --ignore=tests/test_telegram_import.py`
- [ ] Manual browser testing: all 6 themes, settings modal, CRUD operations

---

## Success Criteria

- All new tests pass
- Zero regressions in existing tests
- Manual verification of all UI features in browser
