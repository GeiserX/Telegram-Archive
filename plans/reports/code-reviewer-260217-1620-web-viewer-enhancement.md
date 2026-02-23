# Code Review: Web Viewer Enhancement (v7.0)

**Reviewer:** code-reviewer
**Date:** 2026-02-17
**Scope:** 20 files changed, ~1,192 insertions
**Focus:** Security, performance, correctness, maintainability

---

## Code Review Summary

### Scope
- **Files:** `src/db/adapter.py`, `src/db/models.py`, `src/web/main.py`, `src/web/templates/index.html`, `src/transaction_detector.py`, `alembic/versions/20260217_007_add_transactions_table.py`
- **LOC changed:** ~1,192 insertions, 32 deletions
- **Focus areas:** SQL injection, XSS, error handling, transaction detector edge cases, N+1 queries, code organization

### Overall Assessment

The implementation is solid and well-structured. SQLAlchemy ORM parameterized queries protect against SQL injection throughout. The code follows existing patterns and integrates cleanly. However, there are **two high-severity XSS risks** and a **critical running balance correctness bug** that must be addressed before production use.

---

## Critical Issues

### 1. [CRITICAL] XSS via `v-html` with `highlightText` + `linkifyText` double rendering

**Files:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html` (lines 1240, 406)

**Problem:** Message text is rendered with `v-html` after passing through `linkifyText` (which inserts raw HTML `<a>` tags) and `highlightText` (which inserts `<mark>` tags). The message text itself is **not sanitized** before HTML injection. A malicious Telegram message containing `<img src=x onerror=alert(1)>` or `<script>` tags would execute in the viewer.

```html
<!-- Line 1240 - message text -->
v-html="messageSearchQuery ? highlightText(linkifyText(msg.text), messageSearchQuery) : linkifyText(msg.text)"

<!-- Line 406 - global search snippet -->
v-html="highlightText(r.text_snippet, searchQuery)"
```

The `linkifyText` function (line 3474) does a naive regex replace without escaping HTML entities first:
```js
const linkifyText = (text) => {
    if (!text) return ''
    const urlRegex = /(https?:\/\/[^\s]+)/g
    return text.replace(urlRegex, '<a href="$1" target="_blank">$1</a>')
}
```

**Impact:** Any Telegram user can send a message with XSS payload that executes in every viewer's browser. This is particularly dangerous because the viewer handles auth cookies.

**Fix:** Escape HTML entities **before** linkifying/highlighting:
```js
const escapeHtml = (text) => {
    const div = document.createElement('div')
    div.textContent = text
    return div.innerHTML
}

const linkifyText = (text) => {
    if (!text) return ''
    const escaped = escapeHtml(text)
    const urlRegex = /(https?:\/\/[^\s<]+)/g
    return escaped.replace(urlRegex, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
}
```

Also add `rel="noopener noreferrer"` to prevent `window.opener` attacks on external links.

### 2. [CRITICAL] Running balance calculation is wrong with pagination/filtering

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` (lines 1826-1860)

**Problem:** The `get_transactions` method calculates running balance by iterating over the **current page** of results, starting from 0.0. When `offset > 0` or a `category` filter is applied, the running balance is incorrect because it does not account for transactions on previous pages.

```python
running = 0.0
for t in result.scalars():
    running += t.credit - t.debit
    txns.append({...
        "balance": round(running, 2),
    })
```

**Impact:** Users see incorrect running balances on page 2+, making the accounting feature unreliable.

**Fix:** Pre-compute the cumulative balance up to the current page offset:
```python
# Calculate running balance up to offset
if offset > 0:
    prefix_base = select(Transaction).where(Transaction.chat_id == chat_id)
    if category:
        prefix_base = prefix_base.where(Transaction.category == category)
    prefix_stmt = select(
        func.coalesce(func.sum(Transaction.credit - Transaction.debit), 0)
    ).select_from(prefix_base.order_by(Transaction.date.asc()).limit(offset).subquery())
    running = float((await session.execute(prefix_stmt)).scalar() or 0)
else:
    running = 0.0
```

---

## High Priority

### 3. [HIGH] Float type for monetary values causes precision errors

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/models.py` (lines 341-342)

**Problem:** `credit` and `debit` columns use `Float` which is IEEE 754 double-precision. This causes classic floating point errors (e.g., `0.1 + 0.2 = 0.30000000000000004`). Over many transactions, accumulated rounding errors become significant.

```python
credit: Mapped[float] = mapped_column(default=0.0, server_default="0")
debit: Mapped[float] = mapped_column(default=0.0, server_default="0")
```

**Impact:** Running balances can drift from expected values. Minor for small datasets; problematic for accounting with many transactions.

**Recommendation:** Use `Numeric(precision=12, scale=2)` (or `sa.Numeric(12, 2)`) for both columns. This stores exact decimal values. Requires a migration update. For v7.0 initial release this is acceptable if documented, but should be a fast follow-up.

### 4. [HIGH] N+1 query in `get_messages_paginated` - reactions fetched per message

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` (lines 1130-1152)

**Problem:** For each message in the paginated result, the code makes a separate `get_reactions()` call. With a page size of 50, this means 50 additional queries per page load.

```python
# Line 1143 - inside loop over messages
reactions = await self.get_reactions(msg["id"], chat_id)
```

Similarly, reply text lookups (line 1133) do individual queries for each message with a reply.

**Impact:** Slow page loads on large chats, especially with many reactions/replies.

**Fix:** Batch-fetch reactions for all message IDs in a single query:
```python
# After fetching messages, batch-load reactions
msg_ids = [m["id"] for m in messages]
if msg_ids:
    stmt = select(Reaction).where(
        and_(Reaction.chat_id == chat_id, Reaction.message_id.in_(msg_ids))
    ).order_by(Reaction.emoji)
    result = await session.execute(stmt)
    # Group by message_id
    reactions_map = {}
    for r in result.scalars():
        reactions_map.setdefault(r.message_id, []).append(r)
```

### 5. [HIGH] Transaction scan loads all messages into memory

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` (lines 1096-1114)

**Problem:** The `scan_transactions` endpoint loads **all messages** for a chat into memory using repeated paginated fetches. For chats with millions of messages, this causes OOM.

```python
all_messages = []
offset = 0
batch = 200
while True:
    msgs = await db.get_messages_paginated(chat_id=chat_id, limit=batch, offset=offset)
    ...
    all_messages.extend(msgs)
```

Note also: `get_messages_paginated` includes expensive JOINs (user, media) and reaction lookups per message. The scan only needs `id`, `chat_id`, `sender_id`, `text`, `date`, `is_outgoing`.

**Fix:** Create a lightweight `get_messages_for_scan()` method that streams only the needed fields, or process in batches without accumulating:
```python
# Process in streaming batches
detected = []
offset = 0
while True:
    msgs = await db.get_messages_text_only(chat_id, limit=1000, offset=offset)
    if not msgs:
        break
    detected.extend(detect_transactions(msgs))
    offset += 1000
```

### 6. [HIGH] Missing `limit` validation on several new endpoints

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py`

**Problem:** The `get_transactions` endpoint (line 1073) accepts `limit` without upper-bound validation:
```python
async def get_transactions(chat_id: int, limit: int = 50, offset: int = 0, ...):
```

A request with `limit=999999999` could return the entire table. Same issue for `get_chat_media` (line 630).

Contrast with `get_chats` (line 536) which properly validates with `Query(50, ge=1, le=1000)`.

**Fix:** Add `Query` validation:
```python
limit: int = Query(50, ge=1, le=500)
offset: int = Query(0, ge=0)
```

---

## Medium Priority

### 7. [MEDIUM] `get_message_context` endpoint is broken

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` (lines 649-688)

**Problem:** The `get_message_context` endpoint calls `get_messages_paginated` with `date_from=target["date"]` to get messages after the target. But `get_messages_paginated` has `date_from` as a filter parameter that uses `>=`, and the results are ordered `DESC`. This does not correctly fetch messages "after" (newer than) the target in the reversed list.

Additionally, line 680 fetches `target_full` with `limit=1, search=None` which just returns the latest message, not the target -- appears unused/dead code.

**Impact:** Deep link navigation may show an incomplete or incorrect context window.

### 8. [MEDIUM] Transaction detector false positives on phone numbers and dates

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/transaction_detector.py` (lines 15-18)

**Problem:** The `AMOUNT_PATTERN` regex matches standalone numbers 1-9,999,999. This catches:
- Phone numbers: "Call me at 09123456789" (matches `9123456789` -> filtered by >10M, but `091234` would match)
- Dates: "Meeting on 12/25" (matches `12`)
- Message IDs, counts: "Message #1234" (matches `1234`)
- Prices in non-transaction context: "iPhone 15 costs $999" (matches as transaction)

The `confidence` field helps somewhat, but many false positives still enter the database.

**Recommendation:**
- Require a currency prefix/suffix for matches to be valid (currently optional)
- Add negative lookbehind for `#`, `:`, phone number prefixes
- Consider minimum amount threshold > 1 (e.g., 10 or 50)

### 9. [MEDIUM] Transaction export hardcoded limit of 10,000

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` (line 1170)

```python
data = await db.get_transactions(chat_id, limit=10000, offset=0)
```

**Problem:** Chats with >10k transactions silently truncate on export. Users have no indication data is missing.

**Fix:** Use streaming or remove the limit for exports. Add a header row or footer note with total count.

### 10. [MEDIUM] Bare `except:` clauses swallow errors

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/db/adapter.py` (lines 1125, 1394)

```python
except:
    msg["raw_data"] = {}
```

**Impact:** Silently ignores unexpected errors during JSON parsing. Should be `except (json.JSONDecodeError, TypeError):` at minimum.

### 11. [MEDIUM] CORS allows `DELETE` and `PUT` but only `GET, POST` configured

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` (line 330)

```python
allow_methods=["GET", "POST"],
```

But new endpoints use `PUT` (line 1120) and `DELETE` (line 1136). Browsers making cross-origin requests to these methods will be blocked by CORS preflight.

**Fix:** Add `"PUT", "DELETE"` to `allow_methods`, or if cross-origin access is not intended, keep as-is (it works for same-origin).

### 12. [MEDIUM] CSP header missing `font-src` for Google Fonts woff2

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py` (line 349)

The CSP `font-src` includes `https://fonts.gstatic.com` -- this is correct. No issue here.

However, the CSP allows `'unsafe-inline' 'unsafe-eval'` for scripts, which weakens XSS protections. This is necessary for Vue 3 CDN + Tailwind runtime, but worth noting as a trade-off.

---

## Low Priority

### 13. [LOW] `datetime.utcnow()` is deprecated in Python 3.12+

Multiple files use `datetime.utcnow()`. Use `datetime.now(timezone.utc)` instead. Not breaking but emits deprecation warnings.

### 14. [LOW] Global search debouncing missing

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html` (line 380)

```html
@input="globalSearchMode ? performGlobalSearch() : onSearchInput()"
```

The `performGlobalSearch` function fires on every keystroke with no debounce. `onSearchInput` appears to have debouncing, but `performGlobalSearch` does not.

**Impact:** Excessive API calls during typing.

**Fix:** Add debounce wrapper similar to `onSearchInput`.

### 15. [LOW] `/ v7.0:` comments use bare `/` instead of `//`

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html` (lines 2842, 2849, 2862, 3043, etc.)

```js
/ v7.0: Search highlighting
const highlightText = ...
```

JavaScript single-line comments require `//`. A bare `/` starts a regex literal. This works by accident because the line is treated as a division expression that evaluates but discards the result. However, it can break if the next token is regex-interpretable.

**Fix:** Change all `/ v7.0:` to `// v7.0:`.

### 16. [LOW] `updateHash`/`parseHash` URL routing: no encoding for negative chat IDs

Chat IDs can be negative (e.g., `-1001234567890`). The hash `#chat=-1001234567890&msg=123` parses correctly with `URLSearchParams`-style parsing, but direct string splitting on `=` could break in edge cases. Verify parsing handles negative numbers.

---

## Positive Observations

1. **SQL injection protection:** All database queries use SQLAlchemy ORM with parameterized queries. No raw SQL string interpolation found in new code.
2. **Access control consistency:** All new endpoints properly check `config.display_chat_ids` and use `Depends(require_auth)`.
3. **CSV injection prevention:** The transaction CSV export (line 1181-1183) properly escapes formula injection characters (`=`, `+`, `-`, `@`).
4. **Cursor-based pagination:** The message pagination supports both offset and cursor-based modes, which is good for performance.
5. **Migration quality:** The Alembic migration includes proper indexes, FK constraints, unique constraints, and a clean downgrade path.
6. **Transaction model design:** One-to-one with messages via composite FK, unique constraint prevents duplicates, and `auto_detected` flag cleanly separates auto vs. manual entries.
7. **Skeleton loading:** Good UX improvement that prevents layout shift during load.
8. **Keyboard shortcuts:** Well-implemented with proper modifier key checks.

---

## Recommended Actions (Priority Order)

1. **[P0] Fix XSS** - Add HTML escaping before `linkifyText` and `highlightText`. Add `rel="noopener noreferrer"` to generated links.
2. **[P0] Fix running balance** - Pre-compute cumulative balance for paginated results.
3. **[P1] Fix JS comment syntax** - Change `/ v7.0:` to `// v7.0:` throughout index.html.
4. **[P1] Add limit validation** - Add `Query` bounds to `get_transactions`, `get_chat_media`, `export_transactions`.
5. **[P1] Batch reaction queries** - Replace per-message reaction fetches with single batch query.
6. **[P1] Fix transaction scan memory** - Stream/batch process instead of loading all messages.
7. **[P2] Add global search debounce** - Prevent excessive API calls.
8. **[P2] Narrow bare except clauses** - Use specific exception types.
9. **[P2] Review transaction detector** - Require currency marker to reduce false positives.
10. **[P3] Migrate Float to Numeric** - For monetary precision.
11. **[P3] Update `datetime.utcnow()`** - Use timezone-aware alternative.

---

## Metrics

- **Type Coverage:** N/A (Python dynamic typing; type hints present on public methods)
- **Test Coverage:** Not measured (no new tests found for transaction features)
- **Linting Issues:** JS comment syntax errors (`/` instead of `//`) x ~8 instances
- **Security Issues:** 1 critical (XSS), 0 SQL injection, 1 medium (CORS methods mismatch)

---

## Unresolved Questions

1. Is cross-origin access intended for `PUT`/`DELETE` endpoints? If not, CORS `allow_methods` is fine as-is but `PUT`/`DELETE` from a different origin will fail.
2. Is there a plan for tests covering the transaction detector and accounting endpoints?
3. The `get_message_context` endpoint logic seems incomplete/broken -- is this feature actively used, or is it dead code from an in-progress feature?
4. Should `Float` -> `Numeric` migration happen in v7.0 or be deferred?
