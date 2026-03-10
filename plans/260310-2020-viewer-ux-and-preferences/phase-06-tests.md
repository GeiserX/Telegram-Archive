# Phase 6: Tests

## Overview
- **Priority:** P2
- **Status:** Complete
- **Effort:** 1.5h

Add tests covering new features from phases 1-5. Extend existing test suite.

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `tests/test_admin_settings.py` | Modify | Add tests for new endpoints and model changes |
| `tests/test_viewer_preferences.py` | Create | Test media download restriction and audit log |

## Test Cases

### Phase 1: Admin Chats API
- [ ] Test `/api/admin/chats` returns `username`, `first_name`, `last_name` fields
- [ ] Test response structure matches expected schema

### Phase 2: Media Download Restriction
- [ ] Test `ViewerAccount` model has `no_download` column (default 1)
- [ ] Test `ViewerToken` model has `no_download` column (default 1)
- [ ] Test `POST /api/admin/viewers` accepts `no_download` parameter
- [ ] Test `PUT /api/admin/viewers/{id}` accepts `no_download` update
- [ ] Test `POST /api/admin/tokens` accepts `no_download` parameter
- [ ] Test `PUT /api/admin/tokens/{id}` accepts `no_download` update
- [ ] Test migration 012 exists and has correct structure

### Phase 3: Audit Log
- [ ] Test `ViewerAuditLog` model has correct columns
- [ ] Test `create_audit_log()` adapter creates entry with all fields
- [ ] Test `get_audit_log()` adapter returns entries sorted by `created_at`
- [ ] Test `get_audit_log()` filters by `username` and `action`
- [ ] Test `/api/admin/audit` endpoint route exists
- [ ] Test audit log accepts `action` query parameter

### Phase 4: Infinite Scroll (frontend only — no backend tests needed)

### Phase 5: Per-Chat Backgrounds (frontend only — localStorage, no backend tests)

## Success Criteria

- All new tests pass
- No regressions in existing 78 passing tests
- Model structure tests verify new columns
- Route existence tests verify new/modified endpoints
