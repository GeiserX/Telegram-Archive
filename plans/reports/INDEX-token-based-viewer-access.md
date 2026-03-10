# Token-Based Viewer Access: Research & Implementation Guide
**Index of All Reports**

---

## Quick Navigation

### For Implementation Teams (Start Here)
1. **[token-implementation-summary.md](token-implementation-summary.md)** — Code-focused guide with all patterns and endpoints ready to implement (~400 lines)
2. **[token-faq-and-architecture.md](token-faq-and-architecture.md)** — Architecture decisions, security analysis, FAQ (~500 lines)

### For Deep Dive / Architecture Review
3. **[researcher-token-based-viewer-access.md](researcher-token-based-viewer-access.md)** — Complete research with sources, comparisons, and detailed security analysis (~600 lines)

---

## One-Minute Overview

### What Are We Building?

Share tokens (like Figma share links) that grant temporary access to specific chats without username/password.

```
Admin: "Create token for client to view chats 123, 456, expires in 7 days"
       ↓
       https://domain/?token=abc123xyz (share this link)
       ↓
Client: Clicks link → auto-logged in → sees only chats 123, 456
```

### Key Design Decisions

| Decision | Recommendation | Why |
|----------|---|---|
| **Token generation** | `secrets.token_hex(32)` | 256-bit cryptographic randomness, standard |
| **Token storage** | Hash with PBKDF2-SHA256 | Matches existing codebase, OWASP compliant |
| **URL parameters** | Use query param + immediate redirect | Token visible in logs once, never reused after session issued |
| **Session handling** | Reuse existing session system | Token → session cookie, then normal flow |
| **Expiry** | Optional `expires_at` column (NULL = no expiry) | Flexible, SQL-queryable, audit-friendly |
| **Revocation** | Instant via `is_revoked` flag | No need to delete records, audit trail preserved |

### Security Checklist

- ✅ Tokens generated with cryptographic randomness (256-bit)
- ✅ Tokens hashed before storage (not plaintext)
- ✅ Tokens shown only once (at creation time)
- ✅ Query parameter vulnerability mitigated (redirect → cookie)
- ✅ Referer header leakage prevented (Referrer-Policy header)
- ✅ Session cookies are HttpOnly and Secure
- ✅ Instant revocation supported
- ✅ Time-bound access supported
- ✅ Audit trail for all operations

---

## Report Structure

### 1. Implementation Summary (token-implementation-summary.md)
**Best for:** "I need to code this"
- 1-liner token generation
- SQLAlchemy model (copy-paste ready)
- All adapter methods (ready to implement)
- All FastAPI endpoints (copy-paste with annotations)
- Enhanced `require_auth()` dependency
- PostgreSQL migration script
- MVP implementation order (4 hours estimated)

### 2. FAQ & Architecture (token-faq-and-architecture.md)
**Best for:** "I need to understand this"
- Three-mode authentication overview (master + viewer + token)
- Detailed security comparison (hash vs. plaintext vs. encryption)
- Four risk scenarios with mitigations
- Answers to 10 common implementation questions
- Performance analysis
- Deployment checklist

### 3. Full Research Report (researcher-token-based-viewer-access.md)
**Best for:** "I need deep technical context"
- 11 sections covering all aspects
- Links to authoritative sources (OWASP, Python docs, FastAPI, etc.)
- Comparison with existing auth system
- SQLAlchemy patterns for TTL
- Security checklist (14 items)
- Implementation checklist
- Unresolved questions for future enhancement

---

## Key Files to Create/Modify

### New Files

```
/src/db/models.py
  + Add ViewerToken model (copy from token-implementation-summary.md)

/src/db/adapter.py
  + Add 4 methods: create, verify, revoke, list tokens

/src/web/main.py
  + Add 4 endpoints: create, list, revoke, login-with-token
  + Enhance require_auth() dependency

/alembic/versions/010_add_viewer_tokens.py
  + Create (new migration file)
```

### No Changes to Existing

- `viewer_accounts` table (unchanged)
- `viewer_sessions` table (reused for token sessions)
- Auth cookie mechanism (reused)
- Password hashing functions (reused)

---

## Implementation Phases

### Phase 1: Data Model (30 min)
- [ ] Add `ViewerToken` model to `models.py`
- [ ] Create Alembic migration

### Phase 2: Database Layer (45 min)
- [ ] Add 4 methods to `adapter.py`
- [ ] Test with sqlite/postgresql

### Phase 3: API Endpoints (60 min)
- [ ] POST `/api/admin/tokens` (create)
- [ ] GET `/api/admin/tokens` (list)
- [ ] DELETE `/api/admin/tokens/{id}` (revoke)
- [ ] GET `/auth/token?token=...` (login)

### Phase 4: Auth Dependency (30 min)
- [ ] Enhance `require_auth()` to accept tokens

### Phase 5: Testing (60 min)
- [ ] Unit tests (token generation, verification)
- [ ] Integration tests (endpoints)
- [ ] Security tests (expiry, revocation, hashing)

**Total MVP: ~4 hours**

---

## Security Highlights

### Token Generation
```python
# Generate 256-bit cryptographically random token
token = secrets.token_hex(32)  # 64 hex characters

# Example: a1f2b3c4d5e6f7g8h9i0j1k2l3m4n5o6...
```

### Token Storage (One-Way Hash)
```
Plaintext (shown to user):  "a1f2b3c4d5e6..."
                  ↓
          PBKDF2-SHA256 (600k iterations)
                  ↓
Stored in DB:              "xyz789..." (only hash, no plaintext)
```

**Benefit:** Even if DB is breached, attacker cannot use tokens.

### URL Parameter Mitigation
```
User clicks:     https://domain/?token=abc123xyz
                              ↓
Server validates token
Issues session cookie
                              ↓
Redirects to:    https://domain/
                              ↓
Browser history: https://domain/  (token not stored)
Future requests: Use session cookie (not token)
```

**Benefit:** Token visible in logs only at initial login, never reused.

---

## Comparison with Existing Auth

### Master Account (Env Vars)
- Single admin account
- Derived from VIEWER_USERNAME + VIEWER_PASSWORD
- Permanent
- Sees all chats (respects DISPLAY_CHAT_IDS)

### Viewer Account (DB-Backed)
- Created by admin
- Username + password in DB
- Permanent until deleted
- Per-account chat whitelist

### Share Token (NEW)
- Created by admin
- No password needed
- Time-bound (optional expiry)
- Per-token chat whitelist
- Can be revoked instantly

**All three converge on same session mechanism:**
```
Authenticate (any method)
         ↓
Create session token
         ↓
Store in _viewer_sessions dict + DB
         ↓
Issue HttpOnly cookie
         ↓
User has same access to chats
```

---

## Real-World Examples

### Use Case 1: Client Demo
```
Admin: "Give client access to 2 chats for 1 day"
       Create token (chat_ids=[123, 456], expires_hours=24)
       Send link: https://domain/?token=abc123xyz

Client: Clicks link
        Auto-logged in
        Sees only chats 123, 456
        24 hours later: Token expires, access revoked
```

### Use Case 2: Audit Review
```
Admin: "Auditor needs to review chats 789, 1000 for 3 hours"
       Create token (chat_ids=[789, 1000], expires_hours=3)
       Send via secure email: https://domain/?token=xyz789abc

Auditor: Clicks link
         Reviews chats
         3 hours later: Token expires automatically
         No session lingering
```

### Use Case 3: Team Access
```
Admin: "Grant team access to chat 2000 indefinitely"
       Create token (chat_ids=[2000], expires_hours=None)
       Pin link in team Wiki

Team member: Uses link anytime
             Auto-logged in
             No password to remember
             Can be revoked by admin anytime
```

---

## Performance

**New queries per request:**
- Cookie auth: 0 DB queries (in-memory lookup)
- Token auth (first time): 1-2 DB queries (verify hash, update last_used_at)
- Token auth (subsequent): 0 DB queries (session cookie used)

**Database storage:**
- Per token: ~200 bytes (hash, salt, metadata)
- Per 1000 tokens: ~200 KB (negligible)

**Typical load:**
- 1000 concurrent sessions: milliseconds (in-memory)
- 100 new token logins/minute: ~200 DB queries/minute (0.3% of typical DB capacity)

---

## Unresolved Questions (Future Enhancement)

1. Should tokens support max concurrent sessions?
2. Should admins be able to rotate (not just revoke) tokens?
3. Should we implement granular permissions (read-only vs. full access)?
4. Should we add rate limiting to prevent brute-force token attacks?
5. Should we implement token refresh (auto-issue new on use)?

---

## Testing Strategy

### Unit Tests
- Token generation randomness (no collisions)
- Hash verification (correct/incorrect tokens)
- Expiry logic (expired vs. valid tokens)
- Revocation logic (revoked vs. active tokens)

### Integration Tests
- Create token endpoint (admin only)
- List tokens endpoint (plaintext hidden)
- Revoke token endpoint (instant)
- Login with token endpoint (session issued)
- Viewer permissions (can only access allowed chats)

### Security Tests
- Expired tokens rejected
- Revoked tokens rejected
- Invalid tokens rejected
- Timing attack resistance (constant-time comparison)
- Token hash not reversible

---

## Deployment Checklist

- [ ] Database migration applied (PostgreSQL + SQLite)
- [ ] All endpoints working (create, list, revoke, login)
- [ ] Tokens hashed in DB (verify with SELECT)
- [ ] Session cookies set correctly (HttpOnly, Secure)
- [ ] Referer-Policy header set (no-referrer)
- [ ] Audit logging working (token creation + usage)
- [ ] Token expiry working (test with 1-hour token)
- [ ] Token revocation instant (test revoke + verify rejected)
- [ ] Rate limiting configured (5 attempts/IP/minute)
- [ ] Admin trained (tokens shown once, save immediately)
- [ ] Documentation updated (token management guide)

---

## Document Map

```
/plans/reports/
├── INDEX-token-based-viewer-access.md        ← YOU ARE HERE
├── token-implementation-summary.md            ← START HERE for coding
├── token-faq-and-architecture.md              ← START HERE for design
└── researcher-token-based-viewer-access.md    ← START HERE for deep dive
```

---

## Quick Links to Sections

### token-implementation-summary.md
- [Token Generation (One-Liner)](#1-token-generation-one-liner)
- [Database Schema (SQLAlchemy Model)](#2-database-schema-sqlalchemy-model)
- [Adapter Methods](#3-adapter-methods-add-to-srcdbadapterpy)
- [FastAPI Endpoints](#4-fastapi-endpoints-add-to-srcwebmainpy)
- [Enhanced require_auth() Dependency](#5-enhanced-require_auth-dependency)
- [Security Mitigations Checklist](#6-security-mitigations-checklist)
- [PostgreSQL Migration](#7-migration-for-postgresql-alembic)
- [Implementation Order (MVP)](#8-implementation-order-mvp)

### token-faq-and-architecture.md
- [Architecture Overview](#architecture-overview)
- [Comparison: All Three Auth Modes](#comparison-all-three-auth-modes)
- [Security Comparison: Token Storage Methods](#security-comparison-token-storage-methods)
- [URL Parameter Security: Detailed Risk Analysis](#url-parameter-security-detailed-risk-analysis)
- [FAQ: Implementation Questions](#faq-implementation-questions)
- [Performance Impact](#performance-impact)

### researcher-token-based-viewer-access.md
- [1. Token Generation: Cryptographic Randomness & Length](#1-token-generation-cryptographic-randomness--length)
- [2. Token Storage: Hashed vs. Plain Text](#2-token-storage-hashed-vs-plain-text)
- [3. URL Parameter Token Security: Mitigation Strategy](#3-url-parameter-token-security-mitigation-strategy)
- [4. Comparison with Existing Viewer Account Auth](#4-comparison-with-existing-viewer-account-auth)
- [5. SQLAlchemy Model Patterns for Token Expiry](#5-sqlalchemy-model-patterns-for-token-expiry)
- [6. FastAPI Dependency Injection Pattern: Dual Auth](#6-fastapi-dependency-injection-pattern-dual-auth)
- [7. Admin Endpoint: Create Share Token](#7-admin-endpoint-create-share-token)
- [8. Database Adapter Methods Required](#8-database-adapter-methods-required)
- [9. Summary: Implementation Checklist](#9-summary-implementation-checklist)
- [10. Security Checklist](#10-security-checklist)
- [11. References](#11-references)

---

## Summary for Stakeholders

### What is changing?
Adding optional share token auth alongside existing username/password auth. Admins can now generate temporary access links.

### What stays the same?
- Master account (env vars) unaffected
- Viewer accounts (username/password) unaffected
- Session mechanism (cookies, audit logging) unaffected
- Database design (existing tables) unaffected

### New table: `viewer_tokens`
- Stores hashed share tokens
- Supports expiry dates
- Supports instant revocation
- Minimal footprint (~200 bytes per token)

### New endpoints
- POST `/api/admin/tokens` → Create share token
- GET `/api/admin/tokens` → List tokens
- DELETE `/api/admin/tokens/{id}` → Revoke token
- GET `/auth/token?token=...` → Login with token

### Timeline
- Phase 1-5: ~4 hours (single developer)
- Testing: ~1 hour
- Deployment: ~30 minutes
- Total: ~5.5 hours

### Security
- No plaintext tokens in database
- Instant revocation support
- Time-bound access support
- Industry-standard hashing (PBKDF2-SHA256)
- Same security posture as username/password auth

---

## Next Steps

1. **Implementation teams:** Start with [token-implementation-summary.md](token-implementation-summary.md)
2. **Architects:** Review [token-faq-and-architecture.md](token-faq-and-architecture.md)
3. **Security review:** Check [researcher-token-based-viewer-access.md](researcher-token-based-viewer-access.md) sections 2, 3, 10
4. **Questions?** See [token-faq-and-architecture.md#faq-implementation-questions](token-faq-and-architecture.md#faq-implementation-questions)

---

**Research Complete** — Ready for implementation
