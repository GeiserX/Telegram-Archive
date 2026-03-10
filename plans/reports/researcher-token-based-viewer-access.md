# Token-Based Viewer Access Implementation Research

**Researcher:** claude-haiku | **Date:** 2026-03-10 | **Context:** Implementing share tokens for temporary chat viewer access

---

## Executive Summary

Token-based viewer access (share links) is production-ready using industry-standard patterns. Key findings:

1. **Token Generation**: Use `secrets.token_hex(32)` for 256-bit cryptographic randomness (64-char hex string)
2. **Token Storage**: Hash tokens in DB (not plaintext) + track creation/expiry times via `created_at` and optional `expires_at` columns
3. **URL Parameter Risk**: Query params leak via browser history, server logs, Referer headers—mitigate by using POST form submission or transitioning to header-based auth after initial token redeem
4. **Session Integration**: Existing codebase already has dual auth (cookie OR token). Extend to support view tokens without username/password.
5. **Dependency Injection**: Modify existing `require_auth()` to accept tokens from query param, POST body, or header—then issue session cookie

---

## 1. Token Generation: Cryptographic Randomness & Length

### Recommended: `secrets.token_hex(32)`

```python
import secrets

# Generate 256-bit token (64 hex characters)
token = secrets.token_hex(32)  # e.g., 'a1f2b3c4d5e6f7g8h9i0j1k2l3m4n5o6'
```

**Why 32 bytes (256 bits)?**
- Industry standard minimum for cryptographic randomness (2015 consensus)
- Each byte → 2 hex characters, so 32 bytes = 64-char hex string
- Collision probability negligible (2^-256)
- Sufficient for share tokens, API keys, password resets, one-time codes

**Sources:**
- [Python secrets module documentation](https://docs.python.org/3/library/secrets.html)
- [OWASP token generation best practices](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)

### Alternative Formats

| Format | Length | Use Case |
|--------|--------|----------|
| `token_hex(32)` | 64 chars (hex) | URLs, share links (copy-paste friendly) |
| `token_urlsafe(32)` | 43 chars (base64) | Shorter URLs, less browser-friendly |
| `token_bytes(32)` | 32 bytes (binary) | Not suitable for URLs |

**Recommendation**: Use `token_hex(32)` for this use case—human-readable, copy-paste friendly, no special chars.

---

## 2. Token Storage: Hashed vs. Plain Text

### Database Breach Risk: Plain Text Tokens

If attacker gains read access to database:
- Plain text tokens: Attacker gets all tokens immediately, can impersonate viewers
- Hashed tokens: Attacker only sees hashes, cannot use them without original token

**Impact**: With 100 viewers sharing 50 chats, plain text breach exposes all 50 chats to unauthorized access.

### Recommended: Hash Tokens Using PBKDF2-SHA256

**Why hash?**
- Existing codebase already uses PBKDF2-SHA256 for password hashing (see `main.py:_hash_password()`)
- One-way function; stored hash cannot be reversed
- Salted; prevents rainbow table attacks
- Time-tested and OWASP-compliant

**Implementation Pattern** (reuse existing `_hash_password()` function):

```python
def hash_token(token: str) -> tuple[str, str]:
    """Hash token with PBKDF2-SHA256. Returns (hash_hex, salt_hex)."""
    salt = secrets.token_hex(32)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(salt), 600_000)
    return hash_bytes.hex(), salt

def verify_token(token: str, stored_hash: str, salt: str) -> bool:
    """Verify token against stored hash."""
    computed_hash, _ = hash_token(token)  # Recompute with same salt
    return secrets.compare_digest(computed_hash, stored_hash)
```

**Caveat**: Need to store salt in DB for verification. Schema below.

### Schema Design: `viewer_tokens` Table

```sql
CREATE TABLE viewer_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash VARCHAR(64) NOT NULL,        -- PBKDF2 hash (hex)
    token_salt VARCHAR(64) NOT NULL,        -- Salt for hash (hex)
    created_by VARCHAR(255) NOT NULL,       -- Username of admin who created token
    allowed_chat_ids TEXT NOT NULL,         -- JSON array: ["123", "456"]
    is_revoked INTEGER DEFAULT 0,           -- 0 or 1; revocation is instant
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,                   -- Optional; NULL = no expiry
    last_used_at TIMESTAMP,                 -- Track usage for audit
    created_at_unix FLOAT NOT NULL,         -- Unix timestamp for TTL queries
    UNIQUE (token_hash),
    INDEX idx_created_at (created_at),
    INDEX idx_expires_at (expires_at),
    INDEX idx_created_by (created_by)
);
```

**SQLAlchemy Model**:

```python
class ViewerToken(Base):
    """Share tokens granting temporary access to specific chats.

    v7.2.0: Token-based viewer access for share links.
    Tokens are hashed for security; plaintext token is only shown once at creation.
    """

    __tablename__ = "viewer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_chat_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    is_revoked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at_unix: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_viewer_tokens_created_at", "created_at"),
        Index("idx_viewer_tokens_expires_at", "expires_at"),
        Index("idx_viewer_tokens_created_by", "created_by"),
    )
```

**Key design choices:**
- `token_hash` + `token_salt` allows verification without storing plaintext
- `is_revoked` flag for instant revocation (no DB delete needed, better audit trail)
- `expires_at` NULL = no expiry; app checks `expires_at > NOW()` during verification
- `created_at_unix` stores Unix timestamp for efficient TTL queries (instead of parsing datetime)
- `last_used_at` for analytics (how often is this token used?)

---

## 3. URL Parameter Token Security: Mitigation Strategy

### Risks of Query Parameters

| Leak Vector | Severity | Mitigation |
|---|---|---|
| **Browser History** | High | User can see token in history; searchable |
| **Server Logs** | High | HTTP access logs record full URL with token |
| **Referer Header** | High | If user clicks link to external domain, token leaked in Referer header |
| **Proxy Logs** | Medium | Transparent proxies may log URLs |
| **Bookmarks** | Medium | User can accidentally share bookmarked URL |

**Sources:**
- [OWASP: Query String Information Exposure](https://owasp.org/www-community/vulnerabilities/Information_exposure_through_query_strings_in_url)
- [FullContact: Never Put Secrets in URLs](https://www.fullcontact.com/blog/2016/04/29/never-put-secrets-urls-query-parameters/)
- [MDN: Referer Header Privacy](https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Referer_header:_privacy_and_security_concerns)

### Recommended: Three-Step Mitigation

**Option A: URL Param → Session Cookie (Recommended)**

1. User visits `https://domain/?token=abc123`
2. Server validates token (check hash, expiry, revocation status)
3. Issue session cookie (`viewer_auth`)
4. Redirect to `https://domain/` (without token in URL)
5. Future requests use cookie (not query param)

**Pros**: Token appears in URL only once; subsequent requests use secure HttpOnly cookie
**Cons**: Requires JavaScript redirect or POST form

**Option B: POST Form Submission**

1. User pastes token in login form
2. Form submits POST (not GET) with token in body
3. Server issues session cookie
4. No token in URL at any point

**Pros**: Token never in URL
**Cons**: Requires user action (paste token, click login)

**Option C: Referer-Policy Header**

```python
response.headers["Referrer-Policy"] = "no-referrer"
```

Prevents browser from sending Referer header when user clicks link to external domain. **Not a complete solution** but reduces cross-domain token leakage.

### Recommendation for This Codebase

Use **Option A** (URL param → session cookie) with **Option C** (Referer-Policy header):

```python
@app.get("/?token={token}")
async def auth_with_token(request: Request, token: str):
    """Authenticate with share token, issue session cookie, redirect."""

    # Validate token
    viewer_info = await db.verify_viewer_token(token)
    if not viewer_info:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Create session
    session_token = secrets.token_hex(32)
    session_info = {
        "role": "viewer",
        "username": f"token-{viewer_info['id']}",  # Identify token-based users
        "allowed_chat_ids": set(viewer_info["allowed_chat_ids"]),
        "viewer_id": viewer_info["id"],
        "_created_at": time.time(),
    }
    _viewer_sessions[session_token] = session_info

    # Issue cookie and redirect (no token in redirect URL)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,  # HTTPS only
        samesite="lax",
        max_age=86400,  # 24 hours
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
```

**Why this works:**
1. Token in URL (vulnerable to logs, history) but only visible during initial login
2. After redirect, session cookie is used (HttpOnly, secure)
3. Referer-Policy prevents token leak if user clicks external link during login flow

---

## 4. Comparison with Existing Viewer Account Auth

### Current Architecture (Existing)

```
┌─ Env Var Auth: VIEWER_USERNAME + VIEWER_PASSWORD
│  └─ Master token (PBKDF2 hash of username:password)
│
├─ DB-Backed Viewer Accounts
│  └─ username/password + salt + allowed_chat_ids
│
└─ Session Management
   └─ In-memory + DB-backed (viewer_sessions table)
```

**Existing flow:**
1. User submits `POST /api/login` with username/password
2. Server verifies against DB or env vars
3. Session token generated (`secrets.token_hex(32)`)
4. Session stored in `_viewer_sessions` dict + `viewer_sessions` table
5. Cookie issued (`viewer_auth`)

### Proposed Token-Based Addition

```
┌─ Token-Based Auth: Admin-created share tokens
│  └─ One share token → One set of allowed chat IDs
│
└─ Session Management (reuse existing)
   └─ Validation happens once (token → session), then session cookie used
```

**New flow:**
1. User visits `https://domain/?token=abc123` OR pastes token in form
2. Server validates token hash against `viewer_tokens` table
3. Check: is_revoked == 0, expires_at > NOW(), allowed_chat_ids parsed
4. Session token generated (`secrets.token_hex(32)`)
5. Session stored in `_viewer_sessions` dict + `viewer_sessions` table
6. Redirect to `/` with session cookie (no token in final URL)

**Differences from username/password auth:**

| Aspect | Username/Password | Share Token |
|--------|---|---|
| **Creation** | Admin manually creates account | Admin generates one-time share link |
| **Reusability** | Account persists | Token can be single-use or time-bound |
| **Revocation** | Disable account (affects all sessions) | Revoke token immediately (affects future sessions only) |
| **Audit Trail** | Who logged in | Who generated token, when used |
| **Use Case** | Permanent viewer accounts | Temporary access (demo, client review, audit) |

### Integration Point

Modify existing `_get_current_user()` and `require_auth()` to accept tokens:

```python
def _get_current_user(auth_cookie: str | None, token: str | None = None) -> dict | None:
    """Resolve cookie or token to user info."""

    # Check master token first
    if auth_cookie and AUTH_TOKEN and auth_cookie == AUTH_TOKEN:
        return {"role": "master", "username": VIEWER_USERNAME, "allowed_chat_ids": None, "viewer_id": None}

    # Check viewer sessions (cookie-based)
    if auth_cookie and auth_cookie in _viewer_sessions:
        session = _viewer_sessions[auth_cookie]
        if time.time() - session.get("_created_at", 0) <= _SESSION_MAX_AGE:
            return session
        else:
            del _viewer_sessions[auth_cookie]

    # NEW: Check token-based access
    if token:
        viewer_info = db.verify_viewer_token(token)  # Async; needs refactor
        if viewer_info:
            return {"role": "viewer", "username": f"token-{viewer_info['id']}", ...}

    return None
```

---

## 5. SQLAlchemy Model Patterns for Token Expiry

### Pattern 1: Explicit `expires_at` Column (Recommended)

```python
class ViewerToken(Base):
    __tablename__ = "viewer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    token_salt: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # NULL = no expiry
    is_revoked: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

**Verification query**:

```python
async def verify_viewer_token(self, token: str) -> dict | None:
    """Validate token and return viewer info if valid."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerToken).where(
                ViewerToken.is_revoked == 0,
                (ViewerToken.expires_at.is_(None)) | (ViewerToken.expires_at > func.now()),
                # Token hash verification happens in Python (no way to reverse hash in SQL)
            )
        )
        token_record = result.scalar_one_or_none()

        if token_record and _verify_token(token, token_record.token_hash, token_record.token_salt):
            return {
                "id": token_record.id,
                "allowed_chat_ids": json.loads(token_record.allowed_chat_ids),
            }
    return None
```

**Advantages:**
- SQL-queryable: `SELECT * FROM viewer_tokens WHERE expires_at > NOW()`
- Audit-friendly: Can report "token expires at X"
- Flexible: `expires_at = NULL` means no expiry; `expires_at = DATE+1hour` means 1-hour token

### Pattern 2: TTL via `created_at` + Application Logic (Alternative)

```python
class ViewerToken(Base):
    __tablename__ = "viewer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)  # NULL = no expiry
    is_revoked: Mapped[int] = mapped_column(Integer, default=0)
```

**Verification logic**:

```python
if token_record.ttl_seconds:
    age_seconds = (datetime.utcnow() - token_record.created_at).total_seconds()
    if age_seconds > token_record.ttl_seconds:
        return None  # Expired
```

**Disadvantages:**
- Must compute expiry in Python (not SQL)
- Less clear "when does this expire?" (need math)
- Harder to write audit queries

**Recommendation**: Use **Pattern 1** (explicit `expires_at` column). It's standard, SQL-queryable, and matches existing database conventions.

---

## 6. FastAPI Dependency Injection Pattern: Dual Auth (Cookie OR Token)

### Current Pattern (Cookie-Only)

```python
def require_auth(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)
):
    """Dependency: enforces authentication via cookie."""
    user = _get_current_user(auth_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    request.state.user = user
```

### Enhanced Pattern (Cookie OR Token)

```python
def require_auth(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str | None = Query(default=None),  # From URL: ?token=abc123
    auth_header: str | None = Header(default=None, alias="Authorization"),
):
    """Dependency: enforces authentication via cookie, URL token, or Bearer token.

    Priority:
    1. Cookie (most common, secure HttpOnly)
    2. Query param (share link flow, should transition to cookie)
    3. Authorization header (Bearer token, for API clients)
    """

    # Extract Bearer token if present
    bearer_token = None
    if auth_header and auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]  # Strip "Bearer " prefix

    user = _get_current_user(
        auth_cookie=auth_cookie,
        token=token or bearer_token,
    )

    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    request.state.user = user
```

**Why Bearer header?**
- Standard for API tokens (RFC 6750)
- Not logged in server logs (unlike query params)
- Can be used by programmatic clients (cURL, Python requests)

**Example client usage**:

```bash
# Query param (initial login)
curl "https://domain/?token=abc123"

# Bearer header (API client)
curl -H "Authorization: Bearer abc123" https://domain/api/chats
```

### Async Support (for Token Verification)

If `db.verify_viewer_token()` is async (as it should be), dependency needs to be async:

```python
async def require_auth(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str | None = Query(default=None),
):
    """Async dependency: validates cookie or token."""

    # Cookie validation (synchronous, from memory)
    user = _get_current_user(auth_cookie)
    if user:
        request.state.user = user
        return

    # Token validation (async, from database)
    if token and db:
        try:
            viewer_info = await db.verify_viewer_token(token)
            if viewer_info:
                request.state.user = {
                    "role": "viewer",
                    "username": f"token-{viewer_info['id']}",
                    "allowed_chat_ids": set(viewer_info["allowed_chat_ids"]),
                    "viewer_id": viewer_info["id"],
                }
                return
        except Exception as e:
            logger.warning(f"Token verification failed: {e}")

    raise HTTPException(status_code=401, detail="Unauthorized")
```

**Note**: Currently `require_auth()` is not async (takes synchronous Cookie dependency). Refactoring to async adds complexity but enables database queries in dependencies. Alternative: validate token in endpoint, not in dependency (simpler for MVP).

---

## 7. Admin Endpoint: Create Share Token

### Endpoint Design

```python
@app.post("/api/admin/tokens", dependencies=[Depends(require_admin)])
async def create_viewer_token(
    request: Request,
    chat_ids: list[int],
    expires_hours: int | None = None,  # None = no expiry
) -> dict:
    """Create a new share token. Admin-only.

    Returns plaintext token (shown once only).
    """
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    # Generate token
    token = secrets.token_hex(32)  # 64 hex chars
    token_hash, token_salt = _hash_password(token, salt=None)

    # Calculate expiry
    expires_at = None
    if expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=expires_hours)

    # Store in database
    token_id = await db.create_viewer_token(
        token_hash=token_hash,
        token_salt=token_salt,
        created_by=request.state.user["username"],
        allowed_chat_ids=chat_ids,
        expires_at=expires_at,
    )

    # Return plaintext token (only shown once)
    share_url = f"https://domain/?token={token}"
    return {
        "success": True,
        "token_id": token_id,
        "token": token,  # IMPORTANT: Shown only at creation time
        "share_url": share_url,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "message": "Share this URL or token with viewers. Token will not be shown again.",
    }
```

### Token Visibility Pattern

**Important security principle**: Share tokens should be shown **once only** (at creation time). Do not provide a "show token" endpoint or list endpoint that displays plaintext tokens.

```python
@app.get("/api/admin/tokens", dependencies=[Depends(require_admin)])
async def list_viewer_tokens(request: Request) -> list[dict]:
    """List share tokens (plaintext hidden for security)."""
    if not db:
        raise HTTPException(status_code=500)

    tokens = await db.get_viewer_tokens(created_by=request.state.user["username"])
    return [
        {
            "id": t["id"],
            "created_at": t["created_at"],
            "expires_at": t["expires_at"],
            "allowed_chat_ids": t["allowed_chat_ids"],
            "is_revoked": t["is_revoked"],
            "last_used_at": t["last_used_at"],
            # Note: token hash never returned; token shown only at creation
        }
        for t in tokens
    ]
```

---

## 8. Database Adapter Methods Required

Add to `src/db/adapter.py`:

```python
async def create_viewer_token(
    self,
    token_hash: str,
    token_salt: str,
    created_by: str,
    allowed_chat_ids: list[int],
    expires_at: datetime | None = None,
) -> int:
    """Create a new viewer share token. Returns token ID."""
    async with self.db_manager.get_session() as session:
        token = ViewerToken(
            token_hash=token_hash,
            token_salt=token_salt,
            created_by=created_by,
            allowed_chat_ids=json.dumps(allowed_chat_ids),
            expires_at=expires_at,
            created_at_unix=time.time(),
        )
        session.add(token)
        await session.flush()
        token_id = token.id
        await session.commit()
        return token_id

async def verify_viewer_token(self, token: str) -> dict | None:
    """Validate token and return viewer info. Returns None if invalid/expired/revoked."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerToken).where(ViewerToken.is_revoked == 0)
        )

        for record in result.scalars().all():
            # Check expiry
            if record.expires_at and record.expires_at < datetime.utcnow():
                continue

            # Verify hash
            if _verify_password(token, record.token_hash, record.token_salt):
                # Update last_used_at
                record.last_used_at = datetime.utcnow()
                await session.commit()

                return {
                    "id": record.id,
                    "allowed_chat_ids": json.loads(record.allowed_chat_ids or "[]"),
                    "created_by": record.created_by,
                }

        return None

async def revoke_viewer_token(self, token_id: int) -> bool:
    """Revoke a share token by ID."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerToken).where(ViewerToken.id == token_id)
        )
        token = result.scalar_one_or_none()
        if not token:
            return False

        token.is_revoked = 1
        await session.commit()
        return True

async def get_viewer_tokens(self, created_by: str | None = None) -> list[dict]:
    """List viewer tokens (without plaintext)."""
    async with self.db_manager.get_session() as session:
        query = select(ViewerToken)
        if created_by:
            query = query.where(ViewerToken.created_by == created_by)

        result = await session.execute(query.order_by(ViewerToken.created_at.desc()))

        return [
            {
                "id": t.id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                "allowed_chat_ids": json.loads(t.allowed_chat_ids or "[]"),
                "is_revoked": t.is_revoked,
                "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                "created_by": t.created_by,
            }
            for t in result.scalars().all()
        ]
```

---

## 9. Summary: Implementation Checklist

### Phase 1: Data Model
- [ ] Create `ViewerToken` SQLAlchemy model (token_hash, token_salt, created_by, allowed_chat_ids, expires_at, is_revoked, last_used_at)
- [ ] Create Alembic migration for PostgreSQL + SQLite schema
- [ ] Add indexes on created_at, expires_at, created_by

### Phase 2: Database Adapter
- [ ] Implement `create_viewer_token()`
- [ ] Implement `verify_viewer_token()` (hash verification + expiry check)
- [ ] Implement `revoke_viewer_token()`
- [ ] Implement `get_viewer_tokens()` (list without plaintext)

### Phase 3: FastAPI Endpoints
- [ ] POST `/api/admin/tokens` (create share token, return plaintext once)
- [ ] GET `/api/admin/tokens` (list tokens, hide plaintext)
- [ ] DELETE `/api/admin/tokens/{id}` (revoke token)
- [ ] GET `/?token=...` (login with token, redirect to session)

### Phase 4: Authentication Dependency
- [ ] Refactor `require_auth()` to accept token from query param, body, or Bearer header
- [ ] Update `_get_current_user()` to handle token verification
- [ ] Add token verification audit logging

### Phase 5: Frontend UI (stretch)
- [ ] Admin panel: Create token form (chat ID selection, optional expiry hours)
- [ ] Display share URL + copy-to-clipboard button
- [ ] List tokens with revoke button
- [ ] Login form: Add tab for pasting token

---

## 10. Security Checklist

- [ ] Tokens generated with `secrets.token_hex(32)` (256-bit cryptographic randomness)
- [ ] Tokens stored as PBKDF2 hash (not plaintext) with salt
- [ ] Token plaintext shown only once (at creation time)
- [ ] Token verification uses `secrets.compare_digest()` (timing-attack resistant)
- [ ] Expiry enforced: `expires_at > NOW()` checked during verification
- [ ] Revocation instant: `is_revoked = 1` flag checked during verification
- [ ] URL param mitigated: Token redirects to session cookie after validation
- [ ] Referer-Policy header set: `no-referrer` prevents cross-domain leakage
- [ ] HttpOnly cookies used: `httponly=True` on session cookie
- [ ] Secure flag set: `secure=True` on HTTPS (checked via `x-forwarded-proto`)
- [ ] CSRF protection: Existing SameSite=Lax cookie policy maintained
- [ ] Audit logging: Track token creation, use, revocation
- [ ] No token leakage in logs: Bearer token or cookie preferred over query param
- [ ] Token rotation: Session tokens separate from share tokens (not conflated)

---

## 11. References

### Token Generation & Cryptography
- [Python secrets module](https://docs.python.org/3/library/secrets.html)
- [Miguel Grinberg: The New Way to Generate Secure Tokens in Python](https://blog.miguelgrinberg.com/post/the-new-way-to-generate-secure-tokens-in-python)

### Token Storage Security
- [Is it safe to store tokens in plaintext?](https://github.com/gonzalo-bulnes/simple_token_authentication/issues/82)
- [Why hashed tokens are better](https://dev.to/mustafakhaleddev/why-hashed-otp-tokens-are-better-than-storing-them-in-a-database-4mjf)

### Query Parameter Security
- [OWASP: Information exposure through query strings](https://owasp.org/www-community/vulnerabilities/Information_exposure_through_query_strings_in_url)
- [Never Put Secrets in URLs (FullContact)](https://www.fullcontact.com/blog/2016/04/29/never-put-secrets-urls-query-parameters/)
- [Bearer token vs query parameter security](https://mojoauth.com/ciam-qna/bearer-token-vs-api-key-security)

### FastAPI Authentication
- [FastAPI OAuth2 with JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)
- [FastAPI Security Best Practices (TestDriven.io)](https://testdriven.io/blog/fastapi-jwt-auth/)
- [Top 5 authentication solutions for FastAPI 2026 (WorkOS)](https://workos.com/blog/top-authentication-solutions-fastapi-2026)

### SQLAlchemy
- [SQLAlchemy ORM documentation](https://docs.sqlalchemy.org/en/21/orm/)

---

## Unresolved Questions

1. **Token reusability**: Should a single token support multiple users simultaneously, or enforce single-session-per-token?
   - *Current design allows multiple sessions per token (not tracked)*
   - *Recommendation: Add `max_concurrent_sessions` column if needed*

2. **Token rotation**: Should admin be able to "regenerate" a token (invalidate old, create new) without deleting the record?
   - *Recommendation: Add `refreshable` flag; revoke old token and create new with same permissions*

3. **Frontend: Should token be visible in browser console?**
   - *Session cookie is HttpOnly (not accessible via JS)*
   - *Token from query param will be visible in JS (mitigated by immediate redirect)*

4. **Audit detail level**: Should each token use log which specific chats/messages were accessed?
   - *Current design logs token ID + endpoint; can enhance to log specific chat_id per request*

5. **Token export/sharing**: Can admin export token URL as QR code or shortened link?
   - *Out of scope for MVP; suggestion for future enhancement*

---

**Report Complete**
