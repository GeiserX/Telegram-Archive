# Phase 5: Accounting / Transaction View

## Context Links

- [plan.md](plan.md)
- [Phase 2: Message Display](phase-02-message-display-improvements.md) — relies on enriched message data
- DB Models: `src/db/models.py` — new `transactions` table
- DB Adapter: `src/db/adapter.py` — CRUD for transactions
- Frontend: `src/web/templates/index.html` — spreadsheet panel
- **Branch:** `branch-als-accounting` (separate from viewer-enhancement)

## Overview

- **Priority:** HIGH
- **Status:** complete
- **Effort:** ~4h
- **Description:** Add spreadsheet-style accounting columns alongside chat messages. Auto-detect credit/debit amounts from message text via pattern matching, with manual override. Store in new `transactions` DB table.

## Key Insights

- Messages in certain chats represent financial transactions between two parties
- Pattern detection: regex for amounts like "sent 500", "received 1,000", "paid 200", currency symbols (PHP, $, etc.)
- Need manual override for misclassified or missed transactions
- Running balance computed from chronological credit/debit entries
- Categories for grouping (groceries, rent, utilities, etc.)

## Requirements

### Functional
1. New `transactions` table: `id`, `message_id` (FK), `chat_id`, `sender_id`, `date`, `category`, `credit`, `debit`, `balance`, `notes`, `auto_detected` (bool), `created_at`, `updated_at`
2. Pattern detection engine: scan message text for monetary amounts, classify as credit/debit based on sender direction
3. Spreadsheet view: columns — Date, Sender, Message (truncated), Category, Credit, Debit, Running Balance, Notes
4. Manual override: click any cell to edit credit/debit/category/notes
5. Bulk scan: button to scan all messages in a chat and populate transactions
6. Export to CSV/Excel
7. Summary stats: total credit, total debit, net balance per chat

### Non-Functional
- Pattern detection < 1s for 1000 messages
- Running balance recalculated on any edit
- Spreadsheet view scrollable independently of message view
- Mobile: horizontal scroll for table, or card layout fallback

## Architecture

### Database Schema

```sql
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT,
    date DATETIME NOT NULL,
    category VARCHAR(100) DEFAULT 'uncategorized',
    credit DECIMAL(15,2) DEFAULT 0,
    debit DECIMAL(15,2) DEFAULT 0,
    notes TEXT,
    auto_detected BOOLEAN DEFAULT 1,
    confidence REAL DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES messages(id),
    FOREIGN KEY (chat_id) REFERENCES chats(id)
);
CREATE INDEX idx_transactions_chat ON transactions (chat_id, date);
CREATE UNIQUE INDEX idx_transactions_message ON transactions (message_id);
```

### Pattern Detection Engine

Python module `src/transaction_detector.py`:
- Regex patterns for common amount formats: `\d{1,3}(,\d{3})*(\.\d{2})?`, currency prefixes/suffixes
- Direction heuristics: if sender == "me" and text contains "sent/paid/transfer" → debit; if sender == other party → credit
- Confidence score (0-1) based on pattern match strength
- Returns list of `{message_id, credit, debit, confidence, detected_text}`

### API Endpoints

```
GET    /api/chats/{chat_id}/transactions?limit=50&offset=0&category=...
POST   /api/chats/{chat_id}/transactions/scan          — bulk pattern detection
PUT    /api/transactions/{id}                           — update single transaction
DELETE /api/transactions/{id}                           — remove transaction
GET    /api/chats/{chat_id}/transactions/summary        — totals and balance
GET    /api/chats/{chat_id}/transactions/export?format=csv
```

### Frontend

- Spreadsheet panel: toggle button in chat header (calculator icon)
- Two-panel layout: messages on left, spreadsheet on right (desktop) or tab switch (mobile)
- Editable cells via click → inline input
- Category dropdown with custom option
- Color coding: green for credit, red for debit
- Running balance column auto-computed
- "Scan Messages" button triggers bulk detection
- Export button for CSV download

## Related Code Files

### Create
- `src/transaction_detector.py` — pattern detection engine
- `src/web/static/js/accounting-panel.js` — Vue component for spreadsheet view
- `alembic/versions/20260217_007_add_transactions_table.py` — migration

### Modify
- `src/db/models.py` — add Transaction model
- `src/db/adapter.py` — CRUD methods for transactions
- `src/web/main.py` — transaction API endpoints
- `src/web/templates/index.html` — mount accounting panel, add toggle button

## Implementation Steps

### Backend

1. **Create Alembic migration** for `transactions` table with indexes

2. **Add Transaction model** to `src/db/models.py`:
   - Fields matching schema above
   - Relationship to Message and Chat

3. **Create `src/transaction_detector.py`**:
   - `detect_transactions(messages, my_user_id)` → list of transaction dicts
   - Regex patterns: `r'(\$|PHP|₱|P)?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)'`
   - Direction keywords: sent/paid/transfer/gave = debit; received/got/collected = credit
   - Confidence: exact keyword match = 0.9, amount only = 0.5, ambiguous = 0.3

4. **Add adapter methods** to `src/db/adapter.py`:
   - `get_transactions(chat_id, limit, offset, category)`
   - `create_transactions_bulk(transactions_list)`
   - `update_transaction(txn_id, fields)`
   - `delete_transaction(txn_id)`
   - `get_transaction_summary(chat_id)` → `{total_credit, total_debit, net_balance, count}`

5. **Add API endpoints** to `src/web/main.py`:
   - `GET /api/chats/{id}/transactions` — paginated list
   - `POST /api/chats/{id}/transactions/scan` — trigger detection, store results
   - `PUT /api/transactions/{id}` — update
   - `DELETE /api/transactions/{id}` — delete
   - `GET /api/chats/{id}/transactions/summary` — summary stats
   - `GET /api/chats/{id}/transactions/export` — CSV stream

### Frontend

6. **Create `src/web/static/js/accounting-panel.js`** Vue component:
   - Table with sortable columns
   - Inline editing on cell click
   - Category dropdown
   - Running balance auto-compute
   - Color coding (green credit, red debit)
   - "Scan Messages" button with progress indicator

7. **Add toggle button** in chat header (calculator icon)

8. **Two-panel layout**:
   - Desktop: split view (messages 60%, spreadsheet 40%) with draggable divider
   - Mobile: tab switch between messages and accounting

9. **Export button**: download as CSV with all columns

10. **Summary bar** at bottom of spreadsheet: total credit, total debit, net balance

## Todo List

- [ ] Create Alembic migration for transactions table
- [ ] Add Transaction model to models.py
- [ ] Create transaction_detector.py with regex patterns
- [ ] Add adapter CRUD methods
- [ ] Add transaction API endpoints
- [ ] Build accounting-panel.js Vue component
- [ ] Implement inline cell editing
- [ ] Add category dropdown with custom option
- [ ] Implement running balance computation
- [ ] Add "Scan Messages" bulk detection with progress
- [ ] Add CSV export endpoint and button
- [ ] Add summary stats bar
- [ ] Build responsive two-panel layout
- [ ] Test pattern detection accuracy on real messages
- [ ] Test with chats containing 1000+ messages

## Success Criteria

- Scanning a chat auto-detects >80% of monetary transactions
- Manual override for any auto-detected entry works inline
- Running balance updates immediately on edit
- CSV export contains all columns with correct data
- Two-panel layout works on desktop (side-by-side) and mobile (tabs)
- New transactions table created via Alembic migration

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Pattern detection false positives (phone numbers, dates) | High | Confidence scoring + manual override; filter amounts < 1 or > 1M |
| Currency/locale format mismatch | Medium | Support PHP/$/₱ patterns; configurable via env var |
| Large chat scan timeout | Medium | Background task with progress; chunk processing |
| Running balance accuracy with edits | Medium | Recompute full balance on any change, not incremental |

## Security Considerations

- Transaction endpoints must check auth + DISPLAY_CHAT_IDS
- SQL injection prevented by SQLAlchemy parameterized queries
- CSV export sanitize to prevent formula injection (prefix `=`, `+`, `-`, `@` with `'`)

## Next Steps

- Fine-tune detection patterns based on real message analysis
- Add recurring transaction detection (same amount, same party, regular intervals)
- Integration with actual accounting software export formats (QIF, OFX)
