# System Architecture

**Version:** 7.0 | **Last Updated:** 2026-02-17

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

## Database Schema (v7.0)

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

## Performance Characteristics (v7.0)

| Operation | Time | Notes |
|-----------|------|-------|
| Search 1000 messages | 50-100ms | FTS index on text |
| Media gallery (100 items) | 15-30ms | Index on (chat_id, media_type, date) |
| Transaction scan | 200-500ms | Pattern matching + DB insert |
| Deep link navigation | <100ms | Direct message_id lookup |
| WebSocket broadcast | 10-50ms | PostgreSQL LISTEN or polling fallback |

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
