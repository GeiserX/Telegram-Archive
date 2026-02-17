# v7.0 Quick Reference Guide

**Version:** 7.0 | **Last Updated:** 2026-02-17

## What's New in v7.0

### 1. Advanced Search with Filters
Search across all messages with advanced filtering options.

**API Endpoint:**
```
GET /api/search?q=payment&sender=12345&media_type=document&from=2026-01-01&to=2026-02-28&limit=50
```

**Frontend Usage:**
- Click search icon or press `Ctrl+K`
- Type query
- Click filter button to add sender, media type, date range
- Results include highlighting

### 2. Media Gallery
Browse all media in a chat with type filters and lightbox.

**API Endpoint:**
```
GET /api/chats/{chat_id}/media?type=photo&limit=100&offset=0
```

**Features:**
- Type filters: photo, video, document, audio
- Grid layout (responsive columns)
- Lightbox viewer with fullscreen
- Keyboard navigation (arrow keys, Esc)

### 3. Transaction Accounting
Auto-detect and manage monetary transactions from messages.

**API Endpoints:**
```
POST /api/chats/{chat_id}/transactions/scan           # Auto-detect
GET /api/chats/{chat_id}/transactions                # List
PUT /api/transactions/{txn_id}                        # Edit
DELETE /api/transactions/{txn_id}                     # Remove
GET /api/chats/{chat_id}/transactions/summary        # Summary
GET /api/chats/{chat_id}/transactions/export         # CSV
```

**How It Works:**
1. System scans message text for amounts (PHP, $, ₱, P)
2. Keywords (sent, paid, received, etc.) determine credit/debit
3. Confidence scores (0.4-0.9) indicate accuracy
4. User can manually override or adjust
5. Export to CSV with running balance

**Transaction Fields:**
- date, sender, category
- credit (incoming)
- debit (outgoing)
- balance (running total)
- confidence score
- auto-detected (yes/no)

### 4. Deep Linking
Share links to specific messages.

**Format:**
```
#/chat/{chat_id}/message/{message_id}
```

**Features:**
- Copy message link button
- Highlight message on load
- Restore scroll position
- Shareable via external channels

### 5. Keyboard Shortcuts
```
Ctrl+K / Cmd+K    → Focus search
Esc               → Close lightbox / clear search
?                 → Show help overlay
Arrow keys        → Navigate media in lightbox
Enter             → Submit search
```

### 6. URL Hash Routing
All navigation is now URL-based using hash fragments.

**Routes:**
- `#/` — Home (chat list)
- `#/search` — Search view
- `#/chat/{chat_id}` — Chat messages
- `#/chat/{chat_id}/media` — Media gallery
- `#/chat/{chat_id}/transactions` — Transactions

**Benefits:**
- Shareable links
- Browser back/forward
- Bookmarkable states
- Filter preservation

## Database Changes

### New Table: Transaction
```sql
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    message_id BIGINT NOT NULL UNIQUE,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT,
    date DATETIME,
    category VARCHAR(100),
    credit FLOAT DEFAULT 0,
    debit FLOAT DEFAULT 0,
    confidence FLOAT DEFAULT 0,
    auto_detected INTEGER DEFAULT 1,
    notes TEXT,
    created_at DATETIME,
    updated_at DATETIME
);

CREATE INDEX idx_transaction_chat ON transactions(chat_id, date DESC);
```

### Migration
Run on startup (automatic):
```bash
docker compose up  # Alembic migration 007 runs automatically
```

Or manually:
```bash
docker compose exec telegram-backup alembic upgrade head
```

## Code Changes Summary

### New Files
- `src/transaction_detector.py` — Pattern matching for transactions

### Modified Files
- `src/db/models.py` — Added Transaction model
- `src/db/adapter.py` — Added transaction query methods
- `src/web/main.py` — Added 30+ API endpoints
- `src/web/templates/index.html` — Vue 3 UI for new features

### API Changes
**30+ New Endpoints:**

Search & Discovery:
- `GET /api/search` — Global search with filters
- `GET /api/chats/{chat_id}/messages` — Chat messages
- `GET /api/chats/{chat_id}/messages/{message_id}/context` — Message context

Media:
- `GET /api/chats/{chat_id}/media` — Media gallery with filters
- `GET /api/chats/{chat_id}/media/{media_id}` — Single media (if needed)

Transactions:
- `GET /api/chats/{chat_id}/transactions` — Paginated list
- `POST /api/chats/{chat_id}/transactions/scan` — Auto-detect
- `PUT /api/transactions/{txn_id}` — Update
- `DELETE /api/transactions/{txn_id}` — Delete
- `GET /api/chats/{chat_id}/transactions/summary` — Summary stats
- `GET /api/chats/{chat_id}/transactions/export` — CSV download

Plus supporting endpoints for stats, pinned messages, topics, etc.

## Backward Compatibility

✓ v7.0 is fully backward compatible with v6.x

- Existing databases continue to work without modification
- Alembic migration 007 is non-breaking (adds new table)
- All v6.x features unchanged
- v6.x API endpoints still work
- No data loss or corruption risk

## Performance Improvements

### Indexes Added
```sql
CREATE INDEX idx_message_text_fts ON messages USING FTS(text);
CREATE INDEX idx_message_sender ON messages(sender_id);
CREATE INDEX idx_message_media_type ON messages(media_type, chat_id);
CREATE INDEX idx_transaction_chat ON transactions(chat_id, date DESC);
```

### Performance Targets
| Operation | Target |
|-----------|--------|
| Full-text search | <100ms |
| Media gallery | <200ms |
| Transaction scan | <1s (1000 msgs) |
| Deep link navigation | <50ms |

## Configuration Notes

### New Environment Variables
None added - v7.0 uses existing config

### Affected Variables
- `DISPLAY_CHAT_IDS` — Now filters media gallery and transactions
- `VIEWER_USERNAME/PASSWORD` — Protects all new endpoints
- `DATABASE_URL/DB_PATH` — Transaction table added

## Troubleshooting

### Transactions Not Detected
1. Check message contains amount: PHP, $, ₱, P, or currency keywords
2. Amount must be 1-10,000,000
3. Confidence score shows detection accuracy
4. Manually add if auto-detection missed

**Example detected:**
- "sent 500 PHP" → debit 500 (confidence 0.9)
- "received $100" → credit 100 (confidence 0.9)
- "paid 25" → ambiguous (confidence 0.4)

### Search Returns No Results
1. Check FTS index was created: `CREATE INDEX idx_message_text_fts...`
2. Try simpler search term (single word)
3. Increase limit parameter (default 50)
4. Check DISPLAY_CHAT_IDS doesn't exclude the chat

### Media Gallery Empty
1. Verify media was downloaded (DOWNLOAD_MEDIA=true)
2. Check SKIP_MEDIA_CHAT_IDS doesn't include this chat
3. Ensure thumbnails exist in media directory
4. Try different media type filter

### Deep Link Not Working
1. Check message ID is correct
2. Verify hash format: `#/chat/{id}/message/{id}`
3. Browser must support hash routing
4. Clear browser cache if issues persist

## Migration Checklist

When upgrading to v7.0:

- [ ] Backup existing database: `cp telegram_backup.db telegram_backup.db.backup`
- [ ] Pull latest image: `docker pull drumsergio/telegram-archive:v7.0`
- [ ] Run docker compose: `docker compose up` (migration runs automatically)
- [ ] Verify migration completed (check logs for "Migration 007")
- [ ] Test backup function: Run one backup cycle
- [ ] Test viewer: Navigate to http://localhost:8000
- [ ] Try search with filters
- [ ] Try media gallery
- [ ] (Optional) Scan transactions: POST `/api/chats/{chat_id}/transactions/scan`

## Documentation References

| Document | Purpose |
|----------|---------|
| [codebase-summary.md](./codebase-summary.md) | Technical overview of modules |
| [system-architecture.md](./system-architecture.md) | Design patterns and data flows |
| [code-standards.md](./code-standards.md) | Development guidelines |
| [project-overview-pdr.md](./project-overview-pdr.md) | Requirements and specifications |
| [CHANGELOG.md](./CHANGELOG.md) | Complete version history |

## Getting Help

### Common Questions

**Q: Will my database be backed up?**
A: Always backup before major upgrades. Migration 007 is non-breaking, but it's best practice.

**Q: Can I skip transaction detection?**
A: Yes, don't call `/api/chats/{chat_id}/transactions/scan`. Transactions table will be empty but won't affect backup.

**Q: How do I export all transactions?**
A: Call `GET /api/chats/{chat_id}/transactions/export` for each chat, or write script to iterate all chats.

**Q: Are old messages included in search?**
A: Yes, all backed-up messages are searchable immediately.

**Q: Can I customize transaction detection keywords?**
A: Not via config. Edit `src/transaction_detector.py` DEBIT_KEYWORDS and CREDIT_KEYWORDS regex patterns.

### Reporting Issues
- GitHub Issues: https://github.com/GeiserX/Telegram-Archive/issues
- Include v7.0 in issue title
- Provide error logs from container
- Describe steps to reproduce

## Next Steps

1. **Upgrade to v7.0:**
   ```bash
   docker compose pull
   docker compose up
   ```

2. **Explore New Features:**
   - Try advanced search with filters
   - Browse media gallery
   - Scan transactions in a chat

3. **Provide Feedback:**
   - Report bugs
   - Suggest improvements
   - Share success stories

---

**For detailed technical documentation, see:**
- Architecture: [system-architecture.md](./system-architecture.md)
- Code Patterns: [code-standards.md](./code-standards.md)
- Full Specification: [project-overview-pdr.md](./project-overview-pdr.md)
