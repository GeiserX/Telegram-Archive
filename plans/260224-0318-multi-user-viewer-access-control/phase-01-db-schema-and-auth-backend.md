# Phase 1: DB Schema & Auth Backend

## Context Links
- [Research: FastAPI Multi-User Auth](research/researcher-01-fastapi-multiuser-auth.md)
- [Current auth: src/web/main.py lines 365-502](../../src/web/main.py)
- [Models: src/db/models.py](../../src/db/models.py)
- [DB base: src/db/base.py](../../src/db/base.py)

## Overview
- **Priority:** P1 (foundation for all other phases)
- **Status:** complete
- **Effort:** 2.5h

Add `ViewerAccount` model to SQLite/PostgreSQL, implement multi-user authentication that coexists with the existing master env-var auth, and generate per-user session tokens.

## Key Insights
<!-- Updated: Validation Session 1 - audit log table added -->
- Current auth uses `hashlib.pbkdf2_hmac` with PBKDF2-SHA256 (600k iterations) for master token derivation (main.py:378). Reuse same approach for viewer passwords.
- Single cookie `viewer_auth` stores a hex token. For multi-user, we need the cookie to identify WHICH user is logged in.
- Master user has no DB record; always falls back to env-var check. This guarantees backward compat.
- SQLite uses `Base.metadata.create_all` (base.py:142); new model auto-creates table. PostgreSQL needs Alembic migration.
- **Audit log**: New `viewer_audit_log` table tracks viewer API requests (viewer_id, endpoint, chat_id, timestamp, ip_address).

## Requirements

### Functional
- F1: `viewer_accounts` table stores username, password_hash, salt, allowed_chat_ids (JSON), is_active, timestamps
- F2: Login endpoint authenticates against DB first, then falls back to env-var master credentials
- F3: Session cookie identifies user role (master vs viewer) and viewer ID
- F4: Master sessions continue working with existing cookie value

### Non-Functional
- NF1: Password hashing uses PBKDF2-SHA256 with random 32-byte salt, 600k iterations (OWASP 2023)
- NF2: No new pip dependencies
- NF3: Viewer usernames must be unique, case-insensitive

## Architecture

### Token Strategy

**Current (single-user):** Cookie = `PBKDF2(username:password, static_salt)` — one global token.

**New (multi-user):** Cookie = `PBKDF2(username:password:user_id, static_salt)` for master, OR `PBKDF2(username:password_hash:user_id, random_salt)` for viewers.

Better approach: **Use a per-session random token stored in a lookup dict** (in-memory) that maps token -> user info. This avoids needing to decode the cookie.

**Simplest approach chosen:** Keep master token as-is. For viewer accounts, generate token = `PBKDF2(username:password, account_salt)`. Store a mapping `{token: user_info}` in memory on login. On each request, check cookie against master token first, then against active sessions dict.

### Session Store

```python
# In-memory session store (sufficient for single-instance viewer)
# Maps auth_token -> {"user_id": int, "username": str, "role": "viewer", "allowed_chat_ids": set[int]}
active_sessions: dict[str, dict] = {}
```

### Data Flow

```
Login Request
    |
    v
Check DB (viewer_accounts table) -- match? -> generate token, store in active_sessions, set cookie
    |
    no match
    v
Check env vars (VIEWER_USERNAME/VIEWER_PASSWORD) -- match? -> use AUTH_TOKEN, set cookie
    |
    no match
    v
401 Unauthorized
```

## Related Code Files

| File | Action | Changes |
|------|--------|---------|
| `src/db/models.py` | MODIFY | Add `ViewerAccount` model class |
| `src/web/main.py` | MODIFY | Multi-user auth logic, session store, updated `require_auth` |
| `alembic/versions/20260224_007_add_viewer_accounts.py` | CREATE | PostgreSQL migration |

## Implementation Steps

### Step 1: Add ViewerAccount Model (src/db/models.py)

Add after the `ChatFolderMember` class (line ~325):

```python
class ViewerAccount(Base):
    """Viewer accounts for multi-user access control.

    Each viewer has a username, hashed password, and list of allowed chat IDs.
    Master/admin user is always authenticated via env vars (VIEWER_USERNAME/VIEWER_PASSWORD).
    """

    __tablename__ = "viewer_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)  # hex-encoded 32-byte random salt
    allowed_chat_ids: Mapped[str | None] = mapped_column(Text)  # JSON array of int chat IDs, e.g. "[-100123, -100456]"
    is_active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, server_default=func.now()
    )

    __table_args__ = (Index("idx_viewer_accounts_username", "username"),)


class ViewerAuditLog(Base):
    """Audit log for viewer API requests. Tracks what viewers access."""

    __tablename__ = "viewer_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    viewer_id: Mapped[int] = mapped_column(Integer, nullable=False)  # FK to viewer_accounts.id
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)  # e.g. "/api/chats/123/messages"
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # extracted from path if applicable
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, server_default=func.now())

    __table_args__ = (
        Index("idx_audit_viewer_id", "viewer_id"),
        Index("idx_audit_timestamp", "timestamp"),
    )
```

Also add `ViewerAccount` and `ViewerAuditLog` to the import in `src/db/adapter.py` (line 26-36) and `src/db/__init__.py` if it re-exports models.

### Step 2: Create Alembic Migration

Create `alembic/versions/20260224_007_add_viewer_accounts.py`:

```python
"""Add viewer_accounts table for multi-user access control.

Revision ID: 007
Revises: 006
Create Date: 2026-02-24
"""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "viewer_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("salt", sa.String(64), nullable=False),
        sa.Column("allowed_chat_ids", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("idx_viewer_accounts_username", "viewer_accounts", ["username"])


def downgrade() -> None:
    op.drop_index("idx_viewer_accounts_username", table_name="viewer_accounts")
    op.drop_table("viewer_accounts")
```

### Step 3: Add Password Hashing Helpers (src/web/main.py)

Add after the AUTH_TOKEN block (after line ~386):

```python
import secrets

def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Hash password with PBKDF2-SHA256. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_hex(32)  # 32-byte random salt
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        600_000,
    )
    return hash_bytes.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Verify password against stored hash."""
    computed_hash, _ = _hash_password(password, salt)
    return secrets.compare_digest(computed_hash, stored_hash)
```

### Step 4: Add Session Store and User Resolution (src/web/main.py)

Add after the password helpers:

```python
# In-memory session store: token -> user_info
# For viewer accounts only; master uses AUTH_TOKEN directly
_viewer_sessions: dict[str, dict] = {}


def _get_current_user(auth_cookie: str | None) -> dict | None:
    """Resolve cookie to user info. Returns None if not authenticated.

    Returns dict with keys:
        role: "master" | "viewer"
        username: str
        allowed_chat_ids: set[int] | None  (None = all chats)
        viewer_id: int | None  (None for master)
    """
    if not auth_cookie:
        return None

    # Check master token first
    if AUTH_TOKEN and auth_cookie == AUTH_TOKEN:
        return {
            "role": "master",
            "username": VIEWER_USERNAME,
            "allowed_chat_ids": None,  # master sees ALL chats
            "viewer_id": None,
        }

    # Check viewer sessions
    return _viewer_sessions.get(auth_cookie)
```

### Step 5: Update require_auth Dependency (src/web/main.py)

Replace the existing `require_auth` function (line 389-395):

```python
def require_auth(request: Request, auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Dependency that enforces cookie-based auth. Stores user info on request.state."""
    if not AUTH_ENABLED:
        # No auth configured — treat as master with full access
        request.state.user = {
            "role": "master",
            "username": "anonymous",
            "allowed_chat_ids": None,
            "viewer_id": None,
        }
        return

    user = _get_current_user(auth_cookie)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    request.state.user = user
```

### Step 6: Update Login Endpoint (src/web/main.py)

Replace the existing `login` function (lines 462-502). The new version tries DB first, then env-var fallback:

```python
@app.post("/api/login")
async def login(request: Request):
    """Authenticate user — checks DB viewer accounts first, then master env vars."""
    if not AUTH_ENABLED:
        return JSONResponse({"success": True, "message": "Auth disabled"})

    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        token = None
        user_info = None

        # 1. Try viewer accounts from DB
        if db:
            viewer = await db.get_viewer_account_by_username(username)
            if viewer and viewer.get("is_active") and _verify_password(password, viewer["password_hash"], viewer["salt"]):
                # Generate session token for this viewer
                token = secrets.token_hex(32)
                allowed_ids_raw = json.loads(viewer["allowed_chat_ids"] or "[]")
                user_info = {
                    "role": "viewer",
                    "username": viewer["username"],
                    "allowed_chat_ids": set(int(cid) for cid in allowed_ids_raw),
                    "viewer_id": viewer["id"],
                }
                _viewer_sessions[token] = user_info

        # 2. Fallback: master env-var credentials
        if token is None and username == VIEWER_USERNAME and password == VIEWER_PASSWORD:
            token = AUTH_TOKEN
            user_info = {
                "role": "master",
                "username": VIEWER_USERNAME,
                "allowed_chat_ids": None,
                "viewer_id": None,
            }

        if token is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        response = JSONResponse({
            "success": True,
            "role": user_info["role"],
            "username": user_info["username"],
        })
        # Set cookie (reuse existing secure cookie logic)
        secure_env = os.getenv("SECURE_COOKIES", "").strip().lower()
        if secure_env == "true":
            secure_cookies = True
        elif secure_env == "false":
            secure_cookies = False
        else:
            forwarded_proto = request.headers.get("x-forwarded-proto", "")
            secure_cookies = forwarded_proto == "https" or str(request.url.scheme) == "https"

        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=secure_cookies,
            samesite="lax",
            max_age=AUTH_SESSION_SECONDS,
        )
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=400, detail="Invalid request")
```

### Step 7: Update auth/check Endpoint (src/web/main.py)

Update `check_auth` (line 452-459) to return role info:

```python
@app.get("/api/auth/check")
async def check_auth(auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Check current authentication status and return user role."""
    if not AUTH_ENABLED:
        return {"authenticated": True, "auth_required": False, "role": "master"}

    user = _get_current_user(auth_cookie)
    return {
        "authenticated": user is not None,
        "auth_required": True,
        "role": user["role"] if user else None,
        "username": user["username"] if user else None,
    }
```

### Step 8: Add Logout Endpoint (src/web/main.py)

Add after login endpoint:

```python
@app.post("/api/logout")
async def logout(request: Request, auth_cookie: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME)):
    """Log out and clear session."""
    # Remove viewer session if exists
    if auth_cookie and auth_cookie in _viewer_sessions:
        del _viewer_sessions[auth_cookie]

    response = JSONResponse({"success": True})
    response.delete_cookie(key=AUTH_COOKIE_NAME)
    return response
```

### Step 9: Add DB Helper Methods (src/db/adapter.py)

Add these methods to the `DatabaseAdapter` class:

```python
@retry_on_locked()
async def get_viewer_account_by_username(self, username: str) -> dict | None:
    """Get viewer account by username (case-insensitive)."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerAccount).where(func.lower(ViewerAccount.username) == username.lower())
        )
        account = result.scalar_one_or_none()
        if not account:
            return None
        return {
            "id": account.id,
            "username": account.username,
            "password_hash": account.password_hash,
            "salt": account.salt,
            "allowed_chat_ids": account.allowed_chat_ids,
            "is_active": account.is_active,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }

@retry_on_locked()
async def get_all_viewer_accounts(self) -> list[dict]:
    """Get all viewer accounts (for admin panel)."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerAccount).order_by(ViewerAccount.created_at)
        )
        accounts = result.scalars().all()
        return [
            {
                "id": a.id,
                "username": a.username,
                "allowed_chat_ids": json.loads(a.allowed_chat_ids or "[]"),
                "is_active": a.is_active,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in accounts
        ]

@retry_on_locked()
async def create_viewer_account(self, username: str, password_hash: str, salt: str, allowed_chat_ids: list[int]) -> dict:
    """Create a new viewer account."""
    async with self.db_manager.get_session() as session:
        account = ViewerAccount(
            username=username,
            password_hash=password_hash,
            salt=salt,
            allowed_chat_ids=json.dumps(allowed_chat_ids),
        )
        session.add(account)
        await session.flush()
        return {"id": account.id, "username": account.username}

@retry_on_locked()
async def update_viewer_account(self, account_id: int, **kwargs) -> bool:
    """Update viewer account fields. Supports: password_hash, salt, allowed_chat_ids, is_active."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            select(ViewerAccount).where(ViewerAccount.id == account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            return False
        for key, value in kwargs.items():
            if hasattr(account, key):
                setattr(account, key, value)
        account.updated_at = datetime.utcnow()
        return True

@retry_on_locked()
async def delete_viewer_account(self, account_id: int) -> bool:
    """Delete a viewer account."""
    async with self.db_manager.get_session() as session:
        result = await session.execute(
            delete(ViewerAccount).where(ViewerAccount.id == account_id)
        )
        return result.rowcount > 0
```

Add `ViewerAccount` to the imports in adapter.py (line 26-36):
```python
from .models import (
    ...
    ViewerAccount,
)
```

## Todo List

- [x] Add `ViewerAccount` model to `src/db/models.py`
- [x] Add `ViewerAccount` to imports in `src/db/adapter.py`
- [x] Add 5 DB adapter methods (get_by_username, get_all, create, update, delete)
- [x] Create Alembic migration `007_add_viewer_accounts.py`
- [x] Add `_hash_password`, `_verify_password` helpers to `main.py`
- [x] Add `_viewer_sessions` dict and `_get_current_user` function
- [x] Update `require_auth` to set `request.state.user`
- [x] Update `login` endpoint for dual-mode auth (DB + env-var fallback)
- [x] Update `check_auth` to return role/username
- [x] Add `logout` endpoint
- [x] Import `secrets` and `json` at top of `main.py`

## Success Criteria
- Master login with env-var credentials works exactly as before
- Viewer account login sets session cookie and resolves to correct user info
- `request.state.user` available in all auth-protected endpoints
- Password stored as PBKDF2-SHA256 with random salt
- SQLite auto-creates table; Alembic migration works for PostgreSQL

## Risk Assessment
- **In-memory sessions lost on restart**: Acceptable for viewer app; users just re-login. Could add DB-backed sessions later if needed.
- **Username collision with master**: Viewer username matching env-var username should still work (DB checked first, then env-var). But login always tries DB first — if a viewer has same username as master, viewer gets priority. Mitigate: admin CRUD should reject creating viewer with same username as master.

## Security Considerations
- PBKDF2-SHA256 with 600k iterations (OWASP 2023 recommendation)
- Random 32-byte salt per account (not shared)
- `secrets.compare_digest` for timing-safe comparison
- Cookie remains HttpOnly, Secure (auto-detect), SameSite=Lax
- Master credentials never stored in DB

## Next Steps
- Phase 2 uses `request.state.user` to filter all endpoints
