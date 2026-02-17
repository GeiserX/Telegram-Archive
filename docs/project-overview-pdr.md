# Project Overview & Product Development Requirements (PDR)

**Version:** 7.0 | **Last Updated:** 2026-02-17 | **Status:** Active Development

## Executive Summary

Telegram Archive is an open-source automated backup system for Telegram messages and media with a modern web viewer. It performs incremental, scheduled backups with real-time message syncing, full-text search, media management, and a new v7.0 accounting view for transaction detection.

**Key Metrics:**
- ~10K LOC across backup engine, database, and web viewer
- Supports SQLite (default) and PostgreSQL
- 30+ API endpoints
- Vue 3 frontend with Tailwind CSS
- 6-month release cycle

## Project Vision

Enable Telegram users to independently archive, search, and analyze their conversations without reliance on Telegram's cloud storage or third-party services.

## Product Goals (v7.0)

### Primary Goals
1. **Search Enhancement** - Advanced filters (sender, media type, date range), global cross-chat search, highlighted results
2. **Media Management** - Grid gallery with type filters, lightbox viewer, keyboard navigation
3. **Accounting View** - Auto-detect monetary transactions, spreadsheet interface, CSV export with running balance
4. **UX Improvements** - Skeleton loading, keyboard shortcuts, URL hash routing, responsive mobile design

### Technical Goals
1. Maintain backward compatibility with v6.x databases
2. Add transaction detection without impacting backup performance
3. Improve search performance with indexed queries
4. Enhance frontend responsiveness with Vue 3

## Core Features (v7.0)

### Backup Engine
- Incremental backup (only new messages since last run)
- Scheduled execution (configurable cron)
- Media deduplication via symlinks
- Batch processing with checkpoint recovery
- Avatar auto-refresh
- Service message tracking (joins, title changes)

### Real-time Listener
- Message edit tracking
- Deletion mirroring (rate-limited)
- New message capture between backups
- Chat action monitoring

### Web Viewer
**Search & Discovery**
- Full-text search across all messages
- Advanced filters: sender, media type, date range
- Global cross-chat search
- Result highlighting in context
- Deep linking via URL hash

**Message Display**
- Telegram-like dark UI
- Message context view (surrounding messages)
- Copy message link
- Sender profile snippets
- Rich media preview

**Media Gallery** (NEW v7.0)
- Grid layout (responsive columns)
- Type filters: photo, video, document, audio
- Lightbox with fullscreen
- Keyboard navigation (arrow keys, Esc)
- Thumbnail previews

**Transaction View** (NEW v7.0)
- Auto-detect monetary amounts from text
- Keyword-based classification (credit/debit)
- Spreadsheet-like interface
- Inline editing
- Category tagging
- CSV export with running balance
- Confidence scoring

**Real-time Updates**
- WebSocket sync
- Push notifications (in-browser + Web Push)
- Auto-reconnect with backoff

## Functional Requirements

### Search & Filters (v7.0)
**FR-1.1:** Global full-text search across all backed-up messages
- Implement FTS index on message text
- Support AND/OR operators
- Performance: <100ms for typical queries

**FR-1.2:** Advanced filtering
- Filter by sender (name/ID)
- Filter by media type (photo, video, document, audio, text)
- Filter by date range (from/to)
- Combine multiple filters
- Return highlighted results with context

**FR-2.1:** Message deep linking
- Generate copyable links to specific messages
- URL hash-based navigation (#chat/{id}/message/{id})
- Restore scroll position on link click

### Media Gallery (v7.0)
**FR-3.1:** Media grid view
- Display all media in chat (thumbnails)
- Responsive grid (1-4 columns based on screen width)
- Lazy load thumbnails
- Support pagination (limit 100 per page)

**FR-3.2:** Media filtering
- Filter by type: photo, video, document, audio
- Combine with date range filter
- Update grid count when filter changes
- Remember last selected filter

**FR-3.3:** Lightbox viewer
- Open on thumbnail click
- Full-size image/video preview
- Navigation between media (arrow keys)
- Fullscreen mode
- Close on Esc key
- Download option

### Transaction Accounting (NEW v7.0)
**FR-4.1:** Auto-detect transactions
- Scan message text for monetary amounts
- Support multiple currency prefixes: PHP, $, ₱, P
- Classify as credit or debit based on keywords
- Handle comma-separated numbers (1,000.00)
- Validate amounts (1-10,000,000 range)

**FR-4.2:** Transaction management
- Display in spreadsheet interface
- Columns: date, sender, debit, credit, balance, category
- Inline editing of category and notes
- Manual override of credit/debit
- Confidence score visibility
- Toggle auto-detected vs manual

**FR-4.3:** Transaction export
- CSV format with headers
- Include running balance column
- Date range filter
- Category filter
- Download as file

**FR-4.4:** Transaction summary
- Total credit/debit/balance
- Count by category
- Auto-generated insights
- Date range support

### UX/Performance (v7.0)
**FR-5.1:** Loading states
- Skeleton screens during data fetch
- Spinner during search
- Progress indicator for media gallery load
- Disable interactions during load

**FR-5.2:** Keyboard shortcuts
- Esc: Close lightbox, clear search
- Ctrl+K or Cmd+K: Focus search
- ?: Show help overlay
- Arrow keys: Navigate media in lightbox
- Enter: Submit search

**FR-5.3:** URL-based routing
- Hash-based navigation (#/search, #/chat/{id}/media, etc.)
- Preserve filters in URL
- Browser back/forward support
- Shareable links

## Non-Functional Requirements

### Performance
| Operation | Target | Notes |
|-----------|--------|-------|
| Search 1000 messages | <100ms | FTS index required |
| Media gallery load | <200ms | Thumbnail pagination |
| Transaction scan | <1s | 1000 messages |
| Deep link navigation | <50ms | Direct message_id lookup |
| WebSocket message | <100ms | Latency to browser |

### Reliability
- Backup continues on media download failure
- WebSocket auto-reconnect on disconnect
- Database transaction safety (ACID)
- Graceful degradation if PostgreSQL unavailable

### Security
- Optional authentication (username/password)
- Session tokens with 30-day expiration
- DISPLAY_CHAT_IDS chat filtering
- HTTPS cookie flag (auto-detect or explicit)
- CORS origin restriction
- Rate limiting on deletions

### Scalability
- Support 10K+ messages per chat
- Support 100+ chats per backup
- Efficient media deduplication
- Pagination for large result sets
- Index-based query optimization

### Compatibility
- SQLite: Zero-config, file-based
- PostgreSQL: For large deployments
- Backward compatible with v6.x databases
- Modern browsers (Chrome 90+, Firefox 88+, Safari 14+)
- Mobile-responsive design
- Works offline (with cached data)

## Technical Architecture

### Tech Stack
- **Backend:** Python 3.11+, FastAPI, SQLAlchemy ORM
- **Frontend:** Vue 3, Tailwind CSS, TypeScript
- **Database:** SQLite or PostgreSQL
- **Deployment:** Docker Compose
- **Message Client:** Telethon (Telegram MTProto)

### Key Components
1. **Backup Engine** (src/telegram_backup.py) - Incremental backup scheduler
2. **Real-time Listener** (src/listener.py) - Event-based message sync
3. **Database Adapter** (src/db/adapter.py) - Query abstraction layer
4. **Web API** (src/web/main.py) - 30+ endpoints
5. **Transaction Detector** (src/transaction_detector.py) - Pattern matching
6. **Frontend** (src/web/templates/index.html) - Vue 3 UI

### Database Schema
- Message (core table, v1.0)
- Transaction (NEW v7.0 accounting)
- Chat, User, Reaction, Forward (supporting tables)
- Migration 007: Add transactions table

## Acceptance Criteria

### v7.0 Release
- [x] Search with advanced filters (sender, media type, date range)
- [x] Full-text search index on message text
- [x] Media gallery with type filters and lightbox
- [x] Transaction auto-detection with pattern matching
- [x] Transaction spreadsheet UI with inline editing
- [x] CSV export with running balance
- [x] Keyboard shortcuts (Esc, Ctrl+K, ?)
- [x] Skeleton loading states
- [x] URL hash routing
- [x] Mobile-responsive design
- [x] Deep linking to specific messages
- [x] Copy message link feature
- [x] Alembic migration 007
- [x] Backward compatibility with v6.x

## Success Metrics

### Usage Metrics
- Search query count (weekly)
- Media gallery views (weekly)
- Transaction scans per chat (monthly)
- Average result count per search

### Performance Metrics
- P95 search latency <100ms
- P95 media gallery load <200ms
- P95 transaction scan <1s for 1000 messages
- WebSocket message latency <100ms

### Reliability Metrics
- Uptime >99.9% (viewer)
- Backup success rate >98%
- WebSocket reconnect success rate >99%
- Database integrity (zero corruption reports)

### User Engagement
- Daily active users (backup + viewer)
- Feature usage breakdown
- Chat activity distribution
- Transaction vs. message ratio

## Constraints & Assumptions

### Constraints
- Secret chats not supported (Telegram API limitation)
- Edit history not tracked (only latest version)
- Deleted messages before first backup cannot be recovered
- Media storage is proportional to backup size
- Real-time listener requires persistent connection

### Assumptions
- Users have valid Telegram credentials
- Database is accessible (local or networked)
- Backup path has sufficient disk space
- Docker/Python runtime available
- Browser supports Vue 3 and ES2020

## Risk Assessment

### High-Priority Risks
1. **Database Corruption** - Mitigated by:
   - ACID transactions
   - Checkpoint recovery
   - Migration testing

2. **Real-time Listener Instability** - Mitigated by:
   - Auto-reconnect with exponential backoff
   - In-memory message queue
   - Batch catch-up on reconnect

3. **Transaction Detection False Positives** - Mitigated by:
   - Confidence scoring (0.4-0.9)
   - Manual override UI
   - Conservative regex patterns

### Medium-Priority Risks
1. **Search Performance Degradation** - Mitigated by:
   - FTS index on text column
   - Pagination (max 500 results)
   - Query optimization

2. **Media Storage Bloat** - Mitigated by:
   - Media deduplication (symlinks)
   - Size limits (MAX_MEDIA_SIZE_MB)
   - Cleanup on chat skip

3. **WebSocket Scalability** - Mitigated by:
   - Fallback to polling
   - Connection limits
   - Message queue buffering

## Dependencies

### External
- Telegram API (MTProto)
- Telethon library (message client)
- FastAPI (web framework)
- SQLAlchemy (ORM)
- PostgreSQL (optional)

### Internal
- Database models (src/db/models.py)
- Configuration system (src/config.py)
- Authentication middleware (src/web/main.py)
- Frontend build (Tailwind, Vue 3)

## Version History

| Version | Release | Key Features |
|---------|---------|--------------|
| 7.0 | Feb 2026 | Search filters, media gallery, transaction accounting, UX improvements |
| 6.3.1 | Feb 2026 | Checkpoint recovery, memory optimization |
| 6.3.0 | Feb 2026 | Skip media downloads per chat |
| 6.2.x | Jan 2026 | Bug fixes, WebSocket fixes |
| 6.0.0 | 2025 | PostgreSQL support, real-time listener |
| 5.0.0 | 2024 | Web viewer launch |
| 4.0.0 | 2023 | Docker split (backup + viewer) |

## Next Steps (v7.1+)

### Planned Features
- [ ] Advanced transaction categorization (AI-assisted)
- [ ] Budget tracking and alerts
- [ ] Chat analytics dashboard
- [ ] Mobile app (native iOS/Android)
- [ ] End-to-end encryption option
- [ ] Cloud backup storage integration

### Infrastructure
- [ ] Metrics dashboard (Prometheus/Grafana)
- [ ] Alerting on backup failures
- [ ] Database migration automation
- [ ] Multi-user support
- [ ] Chat sharing/collaboration

## Maintenance & Support

### Update Schedule
- Security patches: As needed
- Bug fixes: Monthly
- Features: Quarterly (v6.1, 6.2, etc.)
- Major releases: 6-12 months (v7.0, v8.0, etc.)

### Support Channels
- GitHub Issues: Bug reports, feature requests
- Discussions: General questions
- Documentation: Self-service guides
- No SLA (community-driven)

## Document Change Log

| Date | Version | Changes |
|------|---------|---------|
| 2026-02-17 | 7.0 | Initial v7.0 PDR with search, media gallery, transaction accounting |
| 2026-01-15 | 6.3 | Added checkpoint recovery details |
| 2025-12-01 | 6.0 | PostgreSQL support, real-time listener |

---

**Document Owner:** Telegram Archive Team
**Last Review:** 2026-02-17
**Next Review:** 2026-05-17
