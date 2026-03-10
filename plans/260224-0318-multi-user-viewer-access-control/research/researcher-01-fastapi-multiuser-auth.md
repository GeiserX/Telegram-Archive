# FastAPI Multi-User Session-Cookie Auth with SQLite

**Researcher:** claude-haiku | **Date:** 2026-02-24

## Executive Summary

FastAPI with session-cookie auth + SQLite is production-viable. Core pattern: `starlette.middleware.sessions` for cookie handling, `passlib` for hashing, `pydantic` for user models. Session state stored server-side (not JWT), enabling role-based access control. Backward compatibility achieved via fallback authentication logic.

---

## 1. Session-Cookie Auth in FastAPI

### Implementation Pattern

**Starlette SessionMiddleware** (FastAPI's underlying framework):
- Encrypts session data server-side, stores in cookies
- Enables stateful session management (unlike stateless JWT)
- Built-in CSRF protection via `Depends(get_session)`
- Sessions serialized with Pickle or JSON (configurable)

**Key advantage over JWT**: Session revocation is immediate (logout takes effect immediately, no token TTL waiting).

### Code Structure
```python
from starlette.middleware.sessions import SessionMiddleware
from fastapi import FastAPI

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="your-secret-key")

# Access session in endpoints:
@app.get("/login")
async def login(request: Request, response: Response):
    request.session["user_id"] = user.id
    request.session["role"] = user.role
```

---

## 2. Role-Based Access Control (RBAC)

### Pattern: Dependency Injection

```python
async def get_current_user(request: Request) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return db.query(User).filter(User.id == user_id).first()

async def require_admin(user: User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

@app.get("/admin-panel")
async def admin_panel(admin: User = Depends(require_admin)):
    return {"status": "admin access granted"}
```

### Metadata Storage
- Store `role`, `allowed_chat_ids` (JSON list), `created_at`, `last_login` in SQLite user table
- Query allowed_chat_ids on request to filter data
- Use Pydantic model for validation

---

## 3. SQLite User Storage

### Schema
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'user',  -- 'admin' or 'user'
    allowed_chat_ids TEXT,     -- JSON string: '["123", "456"]'
    metadata JSON,             -- Additional fields
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1
);
```

### Pydantic Models
```python
from pydantic import BaseModel

class UserInDB(BaseModel):
    id: int
    username: str
    role: str
    allowed_chat_ids: list[str]
    is_active: bool

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
```

---

## 4. Password Hashing Best Practices

### Recommended: Passlib + Bcrypt or Argon2

**Passlib**: Abstracts algorithm selection, automatically handles salt/rounds/parameters.

```python
from passlib.context import CryptContext

# Best: Argon2 (memory-hard, resilient to GPU attacks)
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# Fallback: Bcrypt (widely compatible, proven)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)
```

**Why Passlib**: Handles algorithm migration transparently—if you add new schemes, old hashes auto-rehash on next login.

---

## 5. Migration Strategy: Env Vars → DB

### Phase 1: Dual-Mode Auth (Backward Compat)

```python
async def authenticate_user(username: str, password: str):
    # Try database first
    db_user = db.query(User).filter(User.username == username).first()
    if db_user and verify_password(password, db_user.password_hash):
        return db_user

    # Fallback: Check env vars (VIEWER_USERNAME/VIEWER_PASSWORD)
    if (username == os.getenv("VIEWER_USERNAME") and
        password == os.getenv("VIEWER_PASSWORD")):
        # Return synthetic user or create ephemeral session
        return User(id=-1, username=username, role="admin", allowed_chat_ids=[])

    return None
```

### Phase 2: Deprecation

1. Log env var usage to stderr
2. Add startup migration task: Create initial admin from env vars if no users exist
3. Set deprecation timeline in docs

### Phase 3: Removal

- Remove env var fallback
- Require database user for all auth

---

## 6. Production Considerations

| Aspect | Pattern |
|--------|---------|
| **Secret Key** | Use strong random 32+ char string, store in `.env` or secrets manager |
| **Session TTL** | Set via SessionMiddleware: `max_age=3600` (1 hour default) |
| **HTTPS** | Mandatory—set `httponly=True, secure=True` on cookies |
| **SQL Injection** | Use SQLAlchemy ORM (parameterized queries) |
| **Logout** | `request.session.clear()` on `/logout` endpoint |
| **Concurrent Sessions** | Store session ID in DB if session revocation needed |

---

## 7. Testing & Validation

```python
from fastapi.testclient import TestClient

client = TestClient(app)

def test_login():
    resp = client.post("/login", json={"username": "user", "password": "pass"})
    assert resp.status_code == 200
    assert client.cookies  # Session set

def test_rbac():
    client.post("/login", json={"username": "admin", "password": "pass"})
    resp = client.get("/admin-panel")
    assert resp.status_code == 200
```

---

## Implementation Roadmap

1. ✅ Define SQLite schema (users table)
2. ✅ Implement Passlib context (bcrypt default, support Argon2)
3. ✅ Add SessionMiddleware to FastAPI app
4. ✅ Create login endpoint with dual-mode auth
5. ✅ Add role-based dependency injection
6. ✅ Migrate env var logic to fallback
7. ✅ Add logout endpoint
8. ✅ Test session expiry & CSRF protection

---

## Key References

- **FastAPI Security Docs**: https://fastapi.tiangolo.com/tutorial/security/
- **Passlib Documentation**: https://passlib.readthedocs.io/
- **Starlette SessionMiddleware**: https://www.starlette.io/middleware/sessions/
- **OWASP Session Management**: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html

---

## Unresolved Questions

- Should session storage be file-based or in-memory? (For single-instance app, in-memory via SessionMiddleware default is fine)
- Audit logging granularity? (Who accessed what, when?)
- Password reset flow? (Email-based token or admin-only reset?)
