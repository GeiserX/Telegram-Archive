# Phase 3: Performance & UX

## Context Links

- [plan.md](plan.md)
- [Phase 1: Search Enhancement](phase-01-search-enhancement.md)
- [Phase 2: Message Display](phase-02-message-display-improvements.md)
- [Research: Virtual Scrolling](research/researcher-01-modern-chat-ui.md#2-performance-patterns-for-large-message-histories)
- Frontend: `src/web/templates/index.html` (message list ~line 780-1050, scroll logic ~line 2400-2550)

## Overview

- **Priority:** HIGH
- **Status:** complete
- **Effort:** ~2h
- **Description:** Virtual scrolling for message lists, image lazy loading with IntersectionObserver, keyboard shortcuts, and skeleton loading states.

## Key Insights

- **Current rendering:** All fetched messages are in DOM. Typical fetch is 50 per page, but infinite scroll accumulates hundreds of DOM nodes over time.
- **Vue 3 CDN limitation:** Cannot use `vue-virtual-scroller` npm package. Must implement pure computed-range virtual scroll.
- **Existing IntersectionObserver:** Already used for `loadMoreSentinel` (infinite scroll trigger) and GIF autoplay. Pattern established.
- **Existing keyboard handling:** Lightbox has Escape + arrow key support. No global shortcuts for search or navigation.
- **Flex-col-reverse:** Messages use `flex-direction: column-reverse` for bottom-anchored scroll. Virtual scroll must account for this.
- **Component splitting:** Per validation, extract new features as separate `.js` files loaded via `<script>`. This phase should establish the pattern: extract virtual-scroll logic, keyboard handler, and skeleton components into `src/web/static/js/` files.

<!-- Updated: Validation Session 1 - Added component splitting strategy per user decision -->

## Requirements

### Functional
1. Virtual scrolling: only render messages visible in viewport + buffer (50 above/below)
2. Image lazy loading via IntersectionObserver (replace `loading="lazy"` attribute)
3. Keyboard shortcuts: Esc (close panels), Ctrl/Cmd+F (focus search), Ctrl/Cmd+K (global search)
4. Skeleton screens for initial chat load and message loading
5. Smooth scroll-to-message animation for search results and replies

### Non-Functional
- DOM node count < 150 at any time regardless of total messages loaded
- Image load triggered only when within 500px of viewport
- Keyboard shortcuts documented in a help modal (? key)
- No jank during fast scrolling (requestAnimationFrame for scroll handler)

## Architecture

### Virtual Scroll Strategy

Given `flex-direction: column-reverse`, the approach:

1. Track `scrollTop` of messages container
2. Compute visible range from scroll position + container height
3. `visibleMessages` computed property returns slice of `messages` array
4. Spacer divs above/below maintain scroll height consistency
5. Buffer: render 25 extra messages above and below viewport

```
[spacer-top: height = hiddenTopCount * avgMsgHeight]
[rendered messages (visible + buffer)]
[spacer-bottom: height = hiddenBottomCount * avgMsgHeight]
```

**Average message height estimation:** Start with 80px default, refine using ResizeObserver on rendered messages. Store per-message heights in a Map for accuracy.

### Image Lazy Loading

Replace `loading="lazy"` attribute with IntersectionObserver directive:
- Create Vue directive `v-lazy-src` that sets `src` when element enters viewport
- Root margin: `500px` (preload 500px before visible)
- Placeholder: blurred gradient matching chat theme color
- Reuse existing observer pattern from GIF autoplay code

### Keyboard Shortcuts

Global keydown listener on `document`:
- `Escape`: close active panel (lightbox > search > filter > chat detail)
- `Ctrl/Cmd+F`: focus message search input (prevent browser default)
- `Ctrl/Cmd+K`: open global search
- `?` or `Ctrl/Cmd+/`: show keyboard shortcuts help modal
- `Alt+Up/Down`: navigate between chats

## Related Code Files

### Modify
- `src/web/templates/index.html` - virtual scroll, lazy loading directive, keyboard handler, skeletons

### Create
- None

## Implementation Steps

### Virtual Scrolling

1. **Add scroll tracking state**:
   - `scrollTop`, `containerHeight` refs
   - `messageHeights` Map for measured heights
   - `avgMessageHeight` computed (default 80px, refined from measurements)

2. **Compute visible range**:
   ```js
   const visibleStart = computed(() => {
     const scrollOffset = scrollTop.value
     let accumulated = 0, idx = messages.value.length - 1
     while (idx >= 0 && accumulated < scrollOffset - bufferPx) {
       accumulated += messageHeights.get(messages.value[idx].id) || avgMessageHeight.value
       idx--
     }
     return Math.max(0, idx)
   })
   const visibleEnd = computed(() => {
     // similar, add containerHeight + bufferPx
   })
   const visibleMessages = computed(() => messages.value.slice(visibleStart.value, visibleEnd.value + 1))
   ```

3. **Add spacer divs**:
   - Top spacer: sum of heights for messages above visible range
   - Bottom spacer: sum of heights for messages below visible range
   - Use `padding` on container instead of extra divs (cleaner)

4. **Attach scroll listener** with `requestAnimationFrame` debounce:
   ```js
   const onScroll = () => {
     if (rafPending) return
     rafPending = true
     requestAnimationFrame(() => {
       scrollTop.value = container.scrollTop
       containerHeight.value = container.clientHeight
       rafPending = false
     })
   }
   ```

5. **Measure message heights** with ResizeObserver:
   - Observe each rendered message element
   - Store actual height in `messageHeights` Map
   - Unobserve when message leaves DOM

### Image Lazy Loading

6. **Create `v-lazy-src` directive**:
   ```js
   app.directive('lazy-src', {
     mounted(el, binding) {
       const observer = new IntersectionObserver((entries) => {
         if (entries[0].isIntersecting) {
           el.src = binding.value
           observer.unobserve(el)
         }
       }, { rootMargin: '500px' })
       observer.observe(el)
     }
   })
   ```

7. **Replace `loading="lazy"` attributes** on images:
   - Change `:src="getMediaUrl(msg)"` to `v-lazy-src="getMediaUrl(msg)"`
   - Add placeholder background: `bg-gray-700 animate-pulse`

### Keyboard Shortcuts

8. **Add global keyboard handler** in `onMounted`:
   ```js
   document.addEventListener('keydown', (e) => {
     if (e.key === 'Escape') handleEscape()
     if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); focusMessageSearch() }
     if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); openGlobalSearch() }
     if (e.key === '?' && !isInputFocused()) showShortcutsHelp.value = true
   })
   ```

9. **Build shortcuts help modal**:
   - Simple overlay listing all shortcuts in a grid
   - Dismiss with Escape or click outside

### Skeleton Screens

10. **Add chat list skeleton**:
    - 8 placeholder rows with pulsing avatar circle + text lines
    - Show while `loadingChats` is true and `chats.length === 0`

11. **Add message skeleton**:
    - 5 placeholder bubbles alternating left/right with pulsing blocks
    - Show while `loading` is true and `messages.length === 0`

12. **Smooth scroll-to-message**:
    - `scrollToMessage(msgId)` with `behavior: 'smooth'`
    - Add temporary highlight class (yellow flash) for 2 seconds
    - CSS: `@keyframes messageHighlight { from { background: rgba(59,130,246,0.3) } to { background: transparent } }`

## Todo List

- [ ] Add scroll tracking state and computed visible range
- [ ] Implement spacer divs for virtual scroll
- [ ] Add RAF-debounced scroll listener
- [ ] Add ResizeObserver for message height measurement
- [ ] Create `v-lazy-src` directive with IntersectionObserver
- [ ] Replace `loading="lazy"` with `v-lazy-src` on images
- [ ] Add placeholder pulse animation for lazy images
- [ ] Implement global keyboard handler
- [ ] Build keyboard shortcuts help modal
- [ ] Add chat list skeleton (8 rows)
- [ ] Add message list skeleton (5 bubbles)
- [ ] Implement smooth scroll-to-message with highlight animation
- [ ] Test virtual scroll with 500+ messages accumulated
- [ ] Test keyboard shortcuts don't conflict with input fields
- [ ] Test lazy loading on slow connections (throttled DevTools)

## Success Criteria

- DOM node count stays < 150 even with 1000+ messages loaded
- Images load only when approaching viewport (verify in Network tab)
- Ctrl+F focuses message search, Esc closes panels in correct order
- Skeleton screens appear during initial load
- Scroll-to-message smoothly navigates and briefly highlights target
- No scroll jank during fast scrolling

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Virtual scroll flicker with `flex-col-reverse` | High | Extensive testing; fallback to simple DOM recycling if needed |
| Height estimation inaccuracy causes scroll jumps | Medium | ResizeObserver refines heights; keep buffer large (25 msgs) |
| Keyboard shortcuts conflict with browser defaults | Medium | Only override Ctrl+F (well-established pattern); test across browsers |
| IntersectionObserver not supported in old browsers | Low | < 1% affected; `loading="lazy"` as fallback |

## Security Considerations

- Keyboard shortcuts must not trigger when typing in input/textarea fields
- Lazy loading directive must sanitize URLs (no `javascript:` protocol)

## Next Steps

- Virtual scroll infrastructure enables Phase 4 media gallery grid rendering
- Skeleton screens pattern reusable for any new loading states
- Keyboard shortcut system extensible for future features
