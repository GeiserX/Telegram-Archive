# Infinite Scroll / Progressive Loading Research Report

**Research Date:** 2026-03-10
**Context:** Vue 3 SPA chat application with cursor-based pagination
**Focus:** Practical implementation patterns for seamless upward message loading

---

## 1. IntersectionObserver vs Scroll Events

**Verdict: Use IntersectionObserver** (modern standard)

| Aspect | IntersectionObserver | Scroll Listener |
|--------|---------------------|-----------------|
| **Fire Rate** | Only when intersections change | Every pixel scrolled (costly) |
| **Main Thread** | Async, non-blocking | Blocks main thread |
| **Perf Impact** | ~5-10% better FPS | Causes jank/sluggish animations |
| **Layout Thrashing** | None | Requires scroll calculations |

**Implementation Pattern:**
- Sentinel element (empty div) placed before oldest visible message
- IntersectionObserver watches sentinel for visibility
- When visible, trigger fetch for older messages
- Use `{ threshold: [0.1] }` to trigger before fully visible (UX smoother)

---

## 2. Upward Infinite Scroll Pattern (Reverse Chronological)

**Challenge:** Loading older messages (upward) in reverse-chrono layout requires scroll anchor preservation.

**Solution:**
```
1. Note scrollHeight BEFORE fetch
2. Fetch older messages (use before_date/before_id cursor)
3. Prepend to DOM (newest at bottom, oldest at top)
4. After $nextTick():
   - newScrollHeight = element.scrollHeight
   - delta = newScrollHeight - oldScrollHeight
   - scrollTop += delta  (maintain visible position)
```

**Critical:** Wait for `$nextTick()` before adjusting scrollTop—DOM must update first.

---

## 3. Scroll Position Maintenance

**Root Cause of Jank:** DOM prepending shifts all visible content down, breaking scroll anchor.

**Best Practice (Vue 3):**
1. Store `oldScrollHeight` before fetch
2. Render prepended messages
3. In `$nextTick()`: `scrollTop += (newScrollHeight - oldScrollHeight)`
4. Optional: Show faint "loading..." message while fetching (anchor to bottom)

**For Variable-Height Messages:** More complex—anchor to a specific message element ID, measure position before/after, adjust scrollTop accordingly.

---

## 4. Message Caching Strategy

**Approach: Dual-layer cache (in-memory + session storage)**

- **Layer 1 (Volatile):** `Map<chatId, messages[]>` in reactive state
  - Fast switch between open chats (instant, no flicker)
  - Auto-clear on logout

- **Layer 2 (Persistent):** IndexedDB or SessionStorage
  - Survives page refresh within session
  - Use `chatId` as key, store `{ messages, lastCursor, timestamp }`
  - TTL: 30 mins (discard if stale)

**Switching Chats:**
1. Check volatile cache first → instant render
2. If not cached, fetch from API
3. Pre-populate on background when switching

**Avoid:** Full reload if user leaves/returns to same chat within 5 mins.

---

## 5. Telegram Web Approach (Reverse-Engineer)

**Key Observations:**
- Loads in chunked batches (~50-100 messages per fetch)
- Scroll threshold: ~5-10% from top (aggressive preloading)
- Shows subtle "Loading..." indicator above messages
- Uses virtual scrolling for 10K+ message chats (performance)
- Cursor: `offset` parameter, sometimes date-based anchoring

**Known Issue (v1.26.0):** Scroll jumps when loading—fixed in v1.26.1+ by anchoring to specific message ID.

**Practical threshold:** Trigger fetch when user scrolls within 200-300px of top (not 5% to avoid network thrashing).

---

## 6. Loading Indicator & UX Pattern

**Avoid:** Full loading spinner (jarring when prepending).

**Recommended:**
1. Subtle badge/skeleton above message list: `"Loading 25 older messages..."`
2. High z-index, opacity 0.7, auto-dismiss when done
3. OR: Disable scroll briefly + show inline placeholder

**Threshold Timing:**
- Fetch when user within 200px of top
- Show loading state BEFORE fetch (pre-emptive)
- Debounce: 300-500ms (prevents rapid re-fetches)

---

## 7. Search + Infinite Scroll Interaction

**Pattern:**
1. Search results = filtered, non-paginated context
2. Load ALL matching messages on first search
3. When user switches back to timeline view: reload cached timeline
4. Scroll position in timeline ≠ search scroll position (separate caches)

**Implementation:**
- Separate scroll containers: `<div class="timeline">` vs `<div class="search-results">`
- Each maintains own scroll state
- Clear search cache on new search term

---

## Implementation Priority (YAGNI)

**Phase 1 (MVP):**
1. IntersectionObserver sentinel at top
2. Fetch older messages on visibility
3. ScrollTop adjustment in $nextTick()

**Phase 2 (Polish):**
1. Subtle loading indicator
2. Debounce fetch requests
3. In-memory message cache (per chat)

**Phase 3 (Advanced):**
1. Virtual scrolling for 10K+ messages
2. SessionStorage persistence
3. Message search integration

---

## Unresolved Questions

- What's the optimal threshold distance (100px vs 300px) for your chat size/network?
- Do you need virtual scrolling now or later?
- Should search results paginate or load-all-at-once?

