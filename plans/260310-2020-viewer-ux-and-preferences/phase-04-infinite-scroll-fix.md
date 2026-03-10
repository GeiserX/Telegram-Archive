# Phase 4: Infinite Scroll Overhaul

## Overview
- **Priority:** P1
- **Status:** Complete
- **Effort:** 2h

Fix the "must scroll to very end to trigger fetch" behavior. Currently the IntersectionObserver sentinel disappears during loading (`v-if="hasMore && !loading"`), breaking continuous scroll. Need seamless Telegram-style progressive loading.

## Root Cause Analysis

Current flow:
1. User scrolls up → sentinel (1px div at visual top) enters viewport
2. `IntersectionObserver` fires → `loadMessages()`
3. `loading.value = true` → **sentinel disappears** (v-if condition)
4. Messages load, append to array
5. `loading.value = false` → sentinel reappears
6. But scroll position shifted — sentinel no longer visible
7. User must scroll up **again** to trigger next batch

Problems:
- Sentinel has `v-if="hasMore && !loading"` — disappears during loading
- `rootMargin: '200px'` too small (≈5 messages worth)
- No scroll event fallback — only IntersectionObserver
- No message caching — re-fetches on chat switch

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `src/web/templates/index.html` | Modify | Fix sentinel, add scroll listener, increase thresholds, add cache |

## Implementation Steps

1. **Fix sentinel visibility** — don't hide during loading
   ```html
   <!-- OLD: disappears during loading -->
   <div v-if="hasMore && !loading && messages.length > 0" ref="loadMoreSentinel" class="h-1"></div>

   <!-- NEW: always visible when there are more messages -->
   <div v-if="hasMore && messages.length > 0" ref="loadMoreSentinel" class="h-1"></div>
   ```
   The `!loading` guard is already in the observer callback (`!loading.value`), so sentinel doesn't need it.

2. **Increase rootMargin** for earlier trigger
   ```javascript
   messagesScrollObserver = new IntersectionObserver(
       (entries) => {
           if (entries[0].isIntersecting && hasMore.value && !loading.value) {
               loadMessages()
           }
       },
       {
           root: messagesContainer.value,
           rootMargin: '800px'  // Was 200px — start loading 800px before reaching top
       }
   )
   ```

3. **Add scroll event fallback** in `handleScroll()`
   ```javascript
   const handleScroll = (e) => {
       const el = e.target
       // Show/hide scroll-to-bottom button (existing)
       showScrollBottom.value = el.scrollTop < -200

       // Infinite scroll fallback: flex-col-reverse means scrollTop is negative
       // When near the "top" (oldest messages), scrollTop approaches its minimum
       const distFromTop = el.scrollHeight + el.scrollTop - el.clientHeight
       if (distFromTop < 800 && hasMore.value && !loading.value) {
           loadMessages()
       }
   }
   ```
   Note: With `flex-col-reverse`, `scrollTop` is 0 at bottom (newest) and negative going up. `scrollHeight + scrollTop` gives distance from visual top.

4. **Add basic chat message cache** — prevent re-fetch on chat switch
   ```javascript
   const messageCache = ref(new Map()) // chatId → { messages, hasMore, page }

   // In selectChat(): save current chat state before switching
   if (selectedChat.value) {
       messageCache.value.set(selectedChat.value.id, {
           messages: messages.value,
           hasMore: hasMore.value,
           page: page.value,
       })
   }

   // When loading new chat: check cache first
   const cached = messageCache.value.get(chat.id)
   if (cached) {
       messages.value = cached.messages
       hasMore.value = cached.hasMore
       page.value = cached.page
       // Skip loadMessages() — already have data
   } else {
       messages.value = []
       hasMore.value = true
       page.value = 0
       await loadMessages()
   }
   ```
   - Cache size limit: keep last 10 chats, evict LRU
   - Invalidate on WebSocket new-message event for that chat

5. **Scroll position preservation** — when prepending messages at top, maintain visual position
   ```javascript
   // In loadMessages(), before appending:
   const container = messagesContainer.value
   const prevScrollHeight = container.scrollHeight
   const prevScrollTop = container.scrollTop

   // After merging messages and nextTick():
   await nextTick()
   const newScrollHeight = container.scrollHeight
   const delta = newScrollHeight - prevScrollHeight
   container.scrollTop = prevScrollTop - delta  // Compensate for added height
   ```
   This prevents the jarring jump when older messages prepend and push content down.

6. **Debounce observer callback** — prevent rapid-fire fetches during fast scroll
   ```javascript
   let loadDebounce = null
   // In observer callback:
   if (entries[0].isIntersecting && hasMore.value && !loading.value) {
       clearTimeout(loadDebounce)
       loadDebounce = setTimeout(() => loadMessages(), 150)
   }
   ```

7. **Improve loading indicator** — show subtle spinner at top without shifting content
   ```html
   <!-- Move loading indicator OUTSIDE the sentinel logic -->
   <div v-if="loading && messages.length > 0" class="flex justify-center py-2 opacity-60">
       <div class="loading-spinner w-5 h-5"></div>
   </div>
   ```

## Todo List

- [ ] Remove `!loading` from sentinel v-if condition
- [ ] Increase IntersectionObserver rootMargin to 800px
- [ ] Add scroll event fallback in handleScroll()
- [ ] Add scroll position preservation (scrollTop anchor math)
- [ ] Add debounce (150ms) on observer callback
- [ ] Add message cache (Map by chatId)
- [ ] Cache save on chat switch
- [ ] Cache restore on chat select
- [ ] Cache size limit (10 chats LRU)
- [ ] Cache invalidation on WebSocket message
- [ ] Improve loading spinner positioning
- [ ] Test with chats that have 1000+ messages

## Success Criteria

- Scrolling up smoothly loads older messages without pause
- User never has to "scroll all the way to the end"
- Switching between chats and back preserves loaded messages
- Loading spinner visible but non-disruptive during fetch
- No duplicate messages or pagination errors

## Risk Assessment

- **flex-col-reverse scroll math**: `scrollTop` is negative with reverse flex. Test carefully.
- **Cache memory**: 10 chats × 200 msgs ≈ minimal. Safe.
- **Race conditions**: `chatVersion` guard already handles stale fetches.
