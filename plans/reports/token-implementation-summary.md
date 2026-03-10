# Token-Based Viewer Access: Quick Reference & Code Patterns

**Summary for implementation teams** — See full research in `researcher-token-based-viewer-access.md`

---

## 1. Token Generation (One-Liner)

```python
import secrets

# Create 256-bit cryptographically random token (64 hex chars)
share_token = secrets.token_hex(32)  # 'a1f2b3c4d5e6...'

# Verification: use existing _hash_password() from main.py
token_hash, token_salt = _hash_password(share_token)

# Later: verify with secrets.compare_digest()
stored_hash, stored_salt = ...  # from database
def verify_token(input_token):
    computed_hash, _ = _hash_password(input_token, stored_salt)
    return secrets.compare_digest(computed_hash, stored_hash)
```

---

## 2. Database Schema (SQLAlchemy Model)

```python
class ViewerToken(Base):
    __tablename__ = "viewer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_chat_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON: ["123", "456"]
    is_revoked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)  # NULL = no expiry
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    created_at_unix: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("idx_viewer_tokens_created_at", "created_at"),
        Index("idx_viewer_tokens_expires_at", "expires_at"),
        Index("idx_viewer_tokens_created_by", "created_by"),
    )
```

**Key design:**
- `token_hash` + `token_salt`: Secure storage (plaintext never in DB)
- `expires_at` NULL: No expiry; `expires_at > NOW()`: Checks expiry
- `is_revoked`: Instant revocation (no DB delete)
- `last_used_at`: Audit trail

---

## 3. Adapter Methods (Add to `src/db/adapter.py`)

```python
@retry_on_locked()
async def create_viewer_token(
    self,
    token_hash: str,
    token_salt: str,
    created_by: str,
    allowed_chat_ids: list[int],
    expires_at: datetime | None = None,
) -> int:
    """Create share token. Returns token ID."""
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

@retry_on_locked()
async def verify_viewer_token(self, token: str) -> dict | None:
    """Validate token. Returns None if invalid/expired/revoked."""
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
                record.last_used_at = datetime.utcnow()
                await session.commit()
                return {
                    "id": record.id,
                    "allowed_chat_ids": json.loads(record.allowed_chat_ids or "[]"),
                }

        return None

@retry_on_locked()
async def revoke_viewer_token(self, token_id: int) -> bool:
    """Revoke token by ID."""
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

@retry_on_locked()
async def get_viewer_tokens(self, created_by: str | None = None) -> list[dict]:
    """List tokens (plaintext hidden). Returns dicts without token_hash."""
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

## 4. FastAPI Endpoints (Add to `src/web/main.py`)

### Create Token (Admin Only)
```python
@app.post("/api/admin/tokens", dependencies=[Depends(require_admin)])
async def create_viewer_token(request: Request, chat_ids: list[int], expires_hours: int | None = None):
    """Create share token. Returns plaintext (shown once only)."""
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    token = secrets.token_hex(32)
    token_hash, token_salt = _hash_password(token)

    expires_at = None
    if expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=expires_hours)

    token_id = await db.create_viewer_token(
        token_hash=token_hash,
        token_salt=token_salt,
        created_by=request.state.user["username"],
        allowed_chat_ids=chat_ids,
        expires_at=expires_at,
    )

    return {
        "success": True,
        "token_id": token_id,
        "token": token,  # Shown ONLY here
        "share_url": f"https://domain/?token={token}",
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
```

### List Tokens (Admin Only)
```python
@app.get("/api/admin/tokens", dependencies=[Depends(require_admin)])
async def list_viewer_tokens(request: Request):
    """List tokens (plaintext hidden)."""
    if not db:
        raise HTTPException(status_code=500)
    return await db.get_viewer_tokens(created_by=request.state.user["username"])
```

### Revoke Token (Admin Only)
```python
@app.delete("/api/admin/tokens/{token_id}", dependencies=[Depends(require_admin)])
async def revoke_token(token_id: int):
    """Revoke token by ID."""
    if not db:
        raise HTTPException(status_code=500)
    success = await db.revoke_viewer_token(token_id)
    return {"success": success}
```

### Login with Token (Public)
```python
@app.get("/auth/token")
async def auth_with_token(request: Request, token: str):
    """Validate token, issue session cookie, redirect to home."""
    if not AUTH_ENABLED:
        raise HTTPException(status_code=401, detail="Auth disabled")

    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    # Verify token
    viewer_info = await db.verify_viewer_token(token)
    if not viewer_info:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Create session
    session_token = secrets.token_hex(32)
    user_info = {
        "role": "viewer",
        "username": f"token-{viewer_info['id']}",
        "allowed_chat_ids": set(viewer_info["allowed_chat_ids"]),
        "viewer_id": viewer_info["id"],
        "_created_at": time.time(),
    }
    _viewer_sessions[session_token] = user_info

    # Issue cookie + redirect (no token in final URL)
    response = RedirectResponse(url="/", status_code=302)
    secure_cookies = request.headers.get("x-forwarded-proto", "").lower() == "https"
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=secure_cookies,
        samesite="lax",
        max_age=86400,
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
```

---

## 5. Enhanced `require_auth()` Dependency

```python
async def require_auth(
    request: Request,
    auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str | None = Query(default=None),  # ?token=abc123
    auth_header: str | None = Header(default=None, alias="Authorization"),
):
    """Enforces auth via cookie, token, or Bearer header."""

    if not AUTH_ENABLED:
        request.state.user = {
            "role": "master",
            "username": "anonymous",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        return

    # Extract Bearer token
    bearer_token = None
    if auth_header and auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]

    user = _get_current_user(auth_cookie)
    if user:
        request.state.user = user
        return

    # Token-based auth (async)
    if db and (token or bearer_token):
        try:
            viewer_info = await db.verify_viewer_token(token or bearer_token)
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

**Note:** Current `require_auth()` is synchronous. Making it async requires dependency refactoring. For MVP, consider validating tokens in endpoint instead of dependency.

---

## 6. Security Mitigations Checklist

| Risk | Mitigation | Status |
|---|---|---|
| Plaintext tokens in DB | Hash with PBKDF2-SHA256 + salt | ✅ |
| Token shown multiple times | Return plaintext only at creation | ✅ |
| Timing attacks | Use `secrets.compare_digest()` | ✅ |
| Expired tokens | Check `expires_at > NOW()` | ✅ |
| Revoked tokens | Flag `is_revoked = 1` | ✅ |
| Token in browser history | Redirect ?token → session cookie | ✅ |
| Token in server logs | Use Bearer header or cookie | ✅ |
| Token in Referer header | Set `Referrer-Policy: no-referrer` | ✅ |
| Session hijacking | HttpOnly + Secure cookies | ✅ (existing) |
| CSRF | SameSite=Lax cookie policy | ✅ (existing) |

---

## 7. Migration for PostgreSQL (Alembic)

Create `alembic/versions/010_add_viewer_tokens.py`:

```python
def upgrade() -> None:
    op.create_table(
        "viewer_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("token_salt", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("allowed_chat_ids", sa.Text(), nullable=False),
        sa.Column("is_revoked", sa.Integer(), server_default="0"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("created_at_unix", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_viewer_tokens_created_at", "viewer_tokens", ["created_at"])
    op.create_index("idx_viewer_tokens_expires_at", "viewer_tokens", ["expires_at"])
    op.create_index("idx_viewer_tokens_created_by", "viewer_tokens", ["created_by"])

def downgrade() -> None:
    op.drop_table("viewer_tokens")
```

---

## 8. Implementation Order (MVP)

1. **Model + Migration** (30 min)
   - Add `ViewerToken` model
   - Create migration

2. **Adapter Methods** (45 min)
   - `create_viewer_token()`
   - `verify_viewer_token()`
   - `revoke_viewer_token()`
   - `get_viewer_tokens()`

3. **Admin Endpoints** (60 min)
   - POST `/api/admin/tokens` (create)
   - GET `/api/admin/tokens` (list)
   - DELETE `/api/admin/tokens/{id}` (revoke)

4. **Login Endpoint** (45 min)
   - GET `/auth/token?token=...` (validate + redirect)

5. **Dependency Update** (30 min)
   - Enhance `require_auth()` to accept token

6. **Testing** (60 min)
   - Unit tests for token generation, hash verification
   - Integration tests for endpoints
   - Security tests (expiry, revocation)

**Total MVP: ~4 hours**

---

## Key Differences: Token vs. Username/Password Auth

| Feature | Token-Based | Username/Password |
|---|---|---|
| **Creation** | Admin generates share link | Admin creates account |
| **Reusability** | Temporary (can expire) | Permanent account |
| **Revocation** | Instant (flag-based) | Disables all sessions |
| **Use Case** | Client demos, audits, temp access | Permanent viewer accounts |
| **Session** | Token → session cookie (same) | Username → session cookie |

**Both converge on same session storage** — once authenticated, use in-memory `_viewer_sessions` + DB persistence.

---

## References

- Full research: `researcher-token-based-viewer-access.md`
- Python secrets: https://docs.python.org/3/library/secrets.html
- OWASP query params: https://owasp.org/www-community/vulnerabilities/Information_exposure_through_query_strings_in_url
- FastAPI security: https://fastapi.tiangolo.com/tutorial/security/
- SQLAlchemy: https://docs.sqlalchemy.org/en/21/orm/
