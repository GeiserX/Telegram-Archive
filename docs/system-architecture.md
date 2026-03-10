# System Architecture

**Version:** 7.2.0 | **Last Updated:** 2026-03-10

## High-Level Design

```
┌─────────────────────────────────────────────────────────────┐
│                    Telegram Archive v7.0                    │
├─────────────────────────────────────────────────────────────┤
│
│  ┌──────────────────┐              ┌──────────────────┐
│  │  Backup Engine   │              │  Real-time       │
│  │                  │              │  Listener        │
│  │ • Incremental    │              │                  │
│  │ • Scheduled      │              │ • Edits          │
│  │ • Batch proc.    │              │ • Deletions      │
│  │ • Media dedup    │              │ • New messages   │
│  └────────┬─────────┘              └────────┬─────────┘
│           │                                 │
│           │         ┌──────────────┐       │
│           └────────→│   Database   │←──────┘
│                     │              │
│        ┌────────────│  SQLite or   │────────────┐
│        │            │  PostgreSQL  │            │
│        │            └──────┬───────┘            │
│        │                   │                    │
│   ┌────▼──────────┐  ┌─────▼──────────┐   ┌────▼──────────┐
│   │  Messages     │  │  Transactions  │   │  Chat/User    │
│   │  (v1.0)       │  │  (NEW v7.0)    │   │  Metadata     │
│   └───────────────┘  └────────────────┘   └───────────────┘
│
│        ┌──────────────────────────────────────────┐
│        │        FastAPI Web Viewer                │
│        │  (30+ Endpoints)                         │
│        ├──────────────────────────────────────────┤
│        │ • Search + Advanced Filters (v7.0)       │
│        │ • Media Gallery + Lightbox (v7.0)        │
│        │ • Transaction View (NEW v7.0)            │
│        │ • Message Display + Deep Linking (v7.0)  │
│        │ • Real-time WebSocket Updates            │
│        │ • Push Notifications                     │
│        │ • Authentication & Rate Limiting         │
│        └──────────────────────────────────────────┘
│
│        ┌──────────────────────────────────────────┐
│        │    Vue 3 Frontend                        │
│        │    (Tailwind CSS, Responsive)            │
│        ├──────────────────────────────────────────┤
│        │ • Search UI with Filters (v7.0)          │
│        │ • Transaction Spreadsheet (NEW v7.0)     │
│        │ • Media Grid Gallery (v7.0)              │
│        │ • Keyboard Navigation (v7.0)             │
│        │ • Skeleton Loading (v7.0)                │
│        │ • Hash-based URL Routing (v7.0)          │
│        └──────────────────────────────────────────┘
│
│        User Browser (Desktop/Mobile)
│
└─────────────────────────────────────────────────────────────┘
```

## Component Interactions

### Backup Flow
```
Telegram API
    ↓
Telethon Client (src/connection.py)
    ↓
Backup Engine (src/telegram_backup.py)
    ├→ Fetch Messages + Media
    ├→ Dedup Media (symlinks)
    └→ Batch Insert to DB

Scheduler (src/scheduler.py)
    ├→ Run every N hours (SCHEDULE)
    └→ Trigger backup cycle
```

### Real-time Listener Flow
```
Telegram API → Listener (src/listener.py)
    ↓
Message Events
    ├→ New Message: Save to DB
    ├→ Edit: Update text + edit_date
    ├→ Delete: Mark or remove (rate-limited)
    └→ Chat Action: Update metadata

Database Trigger (PostgreSQL LISTEN)
    ↓
WebSocket (src/web/push.py)
    ↓
Browser Update (frontend sync)
```

### Search & Filtering Flow (v7.0)
```
User Query
    ↓
/api/search Endpoint (src/web/main.py)
    ├→ Parse filters: sender_id, media_type, date_from/to
    ├→ Build SQL WHERE clause
    └→ Hit indexed columns

Database Query
    ├→ Message.text (FTS)
    ├→ Message.sender_id
    ├→ Message.media_type
    └→ Message.date range

Result Processing
    ├→ Apply highlighting
    ├→ Format response
    └→ Send to Frontend
```

### Transaction Detection Flow (v7.0)
```
User Action: POST /api/chats/{chat_id}/transactions/scan
    ↓
Scan Messages (src/web/main.py)
    ├→ Fetch all messages for chat
    └→ Call detect_transactions()

Pattern Matching (src/transaction_detector.py)
    ├→ AMOUNT_PATTERN regex (PHP, $, ₱, P prefixes)
    ├→ DEBIT_KEYWORDS regex (sent, paid, transfer, etc.)
    ├→ CREDIT_KEYWORDS regex (received, collected, etc.)
    └→ Confidence scoring (0.4-0.9)

Bulk Insert
    └→ Create Transaction rows with:
        • message_id, chat_id, sender_id
        • credit/debit amounts
        • confidence, category, notes

Frontend Display
    ├→ Spreadsheet-like table
    ├→ Running balance calculation
    └→ Inline editing + CSV export
```

### Media Gallery Flow (v7.0)
```
GET /api/chats/{chat_id}/media
    ├→ Filters: media_type (photo/video/document/audio)
    ├→ Pagination: limit, offset
    └→ Database query

Database
    └→ Index on (chat_id, media_type, date DESC)

Response Format
    {
        "media": [
            {
                "id": message_id,
                "type": "photo|video|...",
                "thumb_url": "/data/media/...",
                "original_url": "/data/media/...",
                "date": timestamp
            }
        ],
        "total": count
    }

Frontend
    ├→ Grid Layout (responsive cols)
    ├→ Type Filters (photo/video/document/audio)
    ├→ Lightbox on Click
    └→ Keyboard Navigation (arrow keys)
```

### Viewer Preferences Flow (v7.2.0)

#### Per-Chat Background Preferences
```
User Action: Select Background Theme
    ↓
Background Picker Modal
    ├→ 6 Themes (light, dark, forest, ocean, sunset, custom)
    └→ 5-8 Presets per theme (SVG patterns, gradients, solids)

Save to localStorage (key: background_theme_{chat_id})
    ↓
Frontend Context Injection
    ├→ Apply CSS variables
    ├→ Render background SVG/gradient
    └→ Persist across sessions

Context Menu Integration
    └→ Right-click chat → "Change Background"
```

#### Download Control per Viewer
```
Admin Action: Disable Downloads
    ↓
PUT /api/admin/viewers/{viewer_id}
    └→ Set no_download=true

Frontend Enforcement
    ├→ CSS: .download-btn { display: none; }
    ├→ Hide download links in message actions
    └→ Block /media download attempts via auth layer

Database
    └→ viewer_accounts.no_download (boolean)
    └→ viewer_tokens.no_download (boolean)
```

#### Activity Log & Audit
```
User Action: Login, Logout, Settings Change
    ↓
Audit Log Adapter (src/web/main.py)
    ├→ Log login_event with timestamp
    ├→ Track action type (login/logout/settings)
    └→ Record IP, user agent

/api/admin/audit Endpoint
    ├→ Filter by username
    ├→ Filter by action (login, logout, settings)
    ├→ Pagination support
    └→ Color-coded rows (green=success, red=failure)

Activity Tab in Settings (Frontend)
    ├→ Render audit log table
    ├→ Sortable columns (date, action, status)
    ├→ Session history display
    └→ Logout all sessions option
```

#### Infinite Scroll & Message Cache (v7.2.0)
```
User Scroll Action
    ↓
Intersection Observer (rootMargin: 800px)
    ├→ Detect sentinel element near viewport
    ├→ 150ms debounce to reduce API calls
    └→ Trigger /api/chats/{chat_id}/messages?before_date=X

Message LRU Cache (in-memory, 10 chat max)
    ├→ Cache last 50-100 messages per chat
    ├→ Evict oldest chat on overflow
    ├→ Reduce API calls for rapid scroll-back
    └→ Preserve scroll position on navigation

Fallback: Scroll Event Listener
    └→ If Intersection Observer unavailable
```

## Database Schema (v7.2.0)

### Transaction Table (NEW)
```sql
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT,
    date DATETIME NOT NULL,
    category VARCHAR(100) DEFAULT 'uncategorized',
    credit FLOAT DEFAULT 0.0,
    debit FLOAT DEFAULT 0.0,
    notes TEXT,
    auto_detected INTEGER DEFAULT 1,
    confidence FLOAT DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_chat_date (chat_id, date DESC),
    INDEX idx_auto_detected (auto_detected),
    UNIQUE(message_id)
);
```

### Message Extensions (v7.0)
Transaction detection works on existing Message table:
- text field (for pattern matching)
- sender_id, date (for filtering)
- chat_id (for grouping)
- is_outgoing (for direction heuristics)

### Indexes for Performance (v7.0)
```sql
CREATE INDEX idx_message_text_fts ON messages USING FTS(text);
CREATE INDEX idx_message_sender ON messages(sender_id);
CREATE INDEX idx_message_media_type ON messages(media_type, chat_id);
CREATE INDEX idx_message_date_range ON messages(chat_id, date DESC);
CREATE INDEX idx_transaction_chat ON transactions(chat_id, date DESC);
```

### Viewer Preferences Tables (v7.2.0)

**ViewerAccount Extensions:**
```sql
ALTER TABLE viewer_accounts ADD COLUMN no_download BOOLEAN DEFAULT FALSE;
```
- Controls whether account can download media via `/media/*` endpoints
- When enabled, frontend hides download buttons via CSS
- Backend enforces at authentication layer

**ViewerToken Extensions:**
```sql
ALTER TABLE viewer_tokens ADD COLUMN no_download BOOLEAN DEFAULT FALSE;
```
- Per-token download restriction (overrides account setting if more restrictive)
- Useful for API clients or temporary access grants

**Audit Log Table (existing, v7.0):**
```sql
CREATE TABLE viewer_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(255) NOT NULL,
    action VARCHAR(50) NOT NULL,  -- 'login', 'logout', 'settings_change'
    ip_address VARCHAR(45),
    user_agent TEXT,
    success BOOLEAN DEFAULT TRUE,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Viewer Sessions Table (v7.1, persisted sessions):**
```sql
CREATE TABLE viewer_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    allowed_chat_ids TEXT,  -- JSON array
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME,
    last_accessed DATETIME
);
```

## API Request/Response Patterns

### Search Endpoint (v7.0)
**Request:**
```
GET /api/search?q=payment&sender=12345&media_type=document&from=2026-01-01&to=2026-02-28&limit=50&offset=0
```

**Response:**
```json
{
  "results": [
    {
      "id": message_id,
      "chat_id": chat_id,
      "text": "Payment received 500 PHP...",
      "highlighted_text": "Payment received <mark>500 PHP</mark>",
      "sender_name": "John Doe",
      "date": "2026-02-15T10:30:00Z",
      "media_type": "text"
    }
  ],
  "total": 142,
  "took_ms": 25
}
```

### Transaction Detection (v7.0)
**Request:**
```
POST /api/chats/-1234567890/transactions/scan
```

**Response:**
```json
{
  "created": 15,
  "transactions": [
    {
      "id": 42,
      "message_id": 789,
      "credit": 500.0,
      "debit": 0.0,
      "category": "uncategorized",
      "confidence": 0.9,
      "notes": "Detected: 500 from 'sent 500 PHP'",
      "auto_detected": true,
      "date": "2026-02-15T10:30:00Z"
    }
  ]
}
```

### Transaction Summary (v7.0)
**Request:**
```
GET /api/chats/-1234567890/transactions/summary
```

**Response:**
```json
{
  "total_credit": 15000.0,
  "total_debit": 8500.0,
  "balance": 6500.0,
  "count": 42,
  "by_category": {
    "uncategorized": 28,
    "expense": 14
  }
}
```

### Chat List with User Details (v7.2.0)
**Request:**
```
GET /api/admin/chats
```

**Response:**
```json
{
  "chats": [
    {
      "id": -1001234567890,
      "title": "Group Name",
      "username": "group_username",
      "first_name": "John",
      "last_name": "Doe",
      "type": "private|group|supergroup|channel",
      "message_count": 5000
    }
  ]
}
```
- `username`, `first_name`, `last_name` fields improve chat picker display in admin panel
- All fields nullable for channels/groups without usernames

### Audit Log Endpoint (v7.2.0)
**Request:**
```
GET /api/admin/audit?username=john_doe&action=login&limit=50&offset=0
```

**Response:**
```json
{
  "logs": [
    {
      "id": 1,
      "username": "john_doe",
      "action": "login",
      "ip_address": "192.168.1.100",
      "user_agent": "Mozilla/5.0...",
      "success": true,
      "timestamp": "2026-03-10T15:30:00Z"
    }
  ],
  "total": 150,
  "took_ms": 12
}
```
- Supports filtering by `username` and `action` (login, logout, settings_change)
- Color-coded in UI: green (success), red (failure)
- Pagination via `limit` and `offset`

### Download Control (v7.2.0)
**Update Viewer Account:**
```
PUT /api/admin/viewers/{viewer_id}
```

**Request:**
```json
{
  "username": "john_doe",
  "no_download": true
}
```

**Response:**
```json
{
  "id": 1,
  "username": "john_doe",
  "no_download": true,
  "allowed_chat_ids": [123, 456]
}
```
- Setting `no_download=true` disables media downloads for the account
- Frontend enforces via CSS + hidden download links
- Backend validates at `/media/*` authentication layer

## Deployment Architecture

### Docker Compose Services
```yaml
telegram-backup:
  image: drumsergio/telegram-archive:v7.0
  environment:
    - DB_TYPE: sqlite|postgresql
    - SCHEDULE: 0 */6 * * *
    - ENABLE_LISTENER: true
  volumes:
    - ./data/backups:/data/backups
    - ./data/session:/data/session

telegram-viewer:
  image: drumsergio/telegram-archive-viewer:v7.0
  environment:
    - DB_TYPE: sqlite
    - VIEWER_USERNAME: admin
  ports:
    - 8000:8000
  volumes:
    - ./data/backups:/data/backups:ro

postgres (optional):
  image: postgres:15
  environment:
    - POSTGRES_DB: telegram_backup
  volumes:
    - postgres-data:/var/lib/postgresql/data
```

### File Structure
```
data/
├── session/
│   └── telegram_backup.session (Telethon auth)
└── backups/
    ├── telegram_backup.db (or PostgreSQL)
    └── media/
        ├── {chat_id}/
        │   ├── photo_{id}.jpg
        │   ├── video_{id}.mp4
        │   └── document_{id}.pdf
        └── {chat_id_2}/
```

## Performance Characteristics (v7.2.0)

| Operation | Time | Notes |
|-----------|------|-------|
| Search 1000 messages | 50-100ms | FTS index on text |
| Media gallery (100 items) | 15-30ms | Index on (chat_id, media_type, date) |
| Transaction scan | 200-500ms | Pattern matching + DB insert |
| Deep link navigation | <100ms | Direct message_id lookup |
| WebSocket broadcast | 10-50ms | PostgreSQL LISTEN or polling fallback |
| Infinite scroll sentinel check | <5ms | Intersection Observer, 150ms debounced |
| Background theme load | <10ms | localStorage lookup + CSS var injection |
| Audit log query | 15-30ms | Indexed on (username, action) |

## Error Handling & Recovery

**Database Crashes (Backup)**
- Checkpoint every N batches (CHECKPOINT_INTERVAL)
- Resume from last committed batch
- Media re-download on VERIFY_MEDIA

**Real-time Listener Disconnects**
- Auto-reconnect with exponential backoff
- Message queue buffer (in-memory)
- Catch-up on reconnect

**Transaction Detection Errors**
- Graceful pattern matching failures
- Confidence scores reflect uncertainty
- Manual override UI for corrections

**WebSocket Failures**
- Fallback to polling
- Auto-reconnect on tab focus
- Message sync queue

## Security Considerations

**Authentication**
- Optional password-protect viewer
- Session tokens with expiration
- HTTPS-only cookies (configurable)

**Data Access Control**
- DISPLAY_CHAT_IDS restricts visible chats
- Per-chat message filtering
- No cross-tenant data leakage

**Rate Limiting**
- Mass operation sliding window (deletions)
- Search result pagination
- API request throttling (optional)

**Encryption**
- HTTPS in production (via reverse proxy)
- SQLite: file permissions (mode 600)
- PostgreSQL: built-in TLS support
