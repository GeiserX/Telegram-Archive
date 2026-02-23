# Phase 3: Mobile Responsive & Performance

## Context Links
- [Plan overview](plan.md)
- [Phase 1: Bubble Layout](phase-01-bubble-layout-and-tails.md) (prerequisite)
- [Phase 2: Media & Albums](phase-02-media-thumbnails-and-albums.md) (prerequisite)
- [CSS Implementation Research](research/researcher-02-css-chat-implementation.md)
- Target: `src/web/templates/index.html` lines 86-90 (scroll CSS), 913 (container), 935 (message row)

## Overview
- **Priority:** P1
- **Status:** complete
- **Description:** Add CSS containment and content-visibility for performance, ensure touch targets are 44px, and tighten mobile bubble widths.

## Key Insights

### content-visibility: auto
- Skips rendering of off-screen elements -- effectively CSS-based virtual scrolling
- Requires `contain-intrinsic-size` to estimate row height and prevent scroll jumps
- Supported: Chrome 85+, Firefox 125+, Safari 18+
- **Critical concern:** The container uses `flex-col-reverse` for instant scroll-to-bottom. `content-visibility: auto` applies to child elements -- should still work because browser determines visibility based on scroll position regardless of flex direction
- Must test: scroll anchoring behavior when `content-visibility` hides/shows elements during scroll

### CSS Containment
- `contain: strict` on scroll container = `layout + style + paint + size` -- prevents reflow leaking in/out
- `contain: layout style` on individual message rows -- isolates per-row reflow
- Already has `-webkit-overflow-scrolling: touch` and `overscroll-behavior-y: contain`

### Touch Targets
- Apple HIG / WCAG 2.5.5: minimum 44x44px for interactive elements
- Current scroll-to-bottom button: 44x44 -- correct
- Chat list items: `p-3` (12px padding) -- adequate, but verify total height
- Media click targets in album: `album-item` with `aspect-ratio: 1` -- sufficient when grid has reasonable size
- Copy-link button (line 1158): very small (w-3 h-3 = 12px), only visible on hover -- acceptable for non-critical action

### Bubble Width on Mobile
- Current: `max-width: calc(100vw - 32px)` -- nearly full-width
- Telegram uses ~80% of screen width for max bubble width on mobile
- Better: `max-width: min(600px, 80vw)` -- gives visual margin on both sides
- The `80vw` value works well at 375px (iPhone SE) = 300px max, and 428px (iPhone 14 Pro) = 342px max

## Requirements

### Functional
1. Message rows outside viewport skip rendering (content-visibility)
2. Touch targets >= 44px for all primary actions
3. Bubble max-width ~80% of viewport on mobile

### Non-functional
- content-visibility must not break scroll-to-bottom behavior
- Must not break Vue reactivity or message highlight animations
- No JavaScript changes in this phase (CSS-only performance)

## Architecture

### CSS Changes

**Update `.messages-scroll` (lines 86-90):**
```css
.messages-scroll {
    -webkit-overflow-scrolling: touch;
    overscroll-behavior-y: contain;
    contain: strict;
}
```

**Add message row performance class:**
```css
/* Applied to each message row wrapper */
.msg-row {
    content-visibility: auto;
    contain-intrinsic-size: 0 60px;  /* estimated avg msg height */
    contain: layout style;
}
```

**Update `.message-bubble` mobile width:**
```css
.message-bubble {
    /* ... existing ... */
    max-width: min(600px, 80vw);
}
```
This replaces both the base `calc(100vw - 32px)` and the `@media (min-width: 768px)` override. The `min()` function handles both cases:
- Mobile 375px: min(600, 300) = 300px
- Tablet 768px: min(600, 614) = 600px
- Desktop 1200px: min(600, 960) = 600px

Can remove the `@media (min-width: 768px)` block since `min()` handles it.

### Template Changes

**Add `.msg-row` class to message wrapper (line 935):**

Current:
```html
<div v-else-if="!isHiddenAlbumMessage(msg, index)" class="flex"
    :class="isOwnMessage(msg) ? 'justify-end' : 'justify-start'">
```

New:
```html
<div v-else-if="!isHiddenAlbumMessage(msg, index)" class="msg-row flex"
    :class="isOwnMessage(msg) ? 'justify-end' : 'justify-start'">
```

Also add `.msg-row` to service message wrapper (line 928):
```html
<div v-if="msg.raw_data?.service_type === 'service'" class="msg-row flex justify-center my-2">
```

And date separator (line 1166):
```html
<div v-if="showDateSeparator(index) && !isHiddenAlbumMessage(msg, index)" class="msg-row date-separator">
```

## Related Code Files
- `src/web/templates/index.html`
  - CSS: lines 63-78 (`.message-bubble`), 86-90 (`.messages-scroll`)
  - Template: line 913 (scroll container), 928 (service msg), 935 (regular msg row), 1166 (date separator)
  - JS: none changed in this phase

## Implementation Steps

1. **Add `contain: strict` to `.messages-scroll`** (line 89, after `overscroll-behavior-y`)
   - This isolates the scroll container's reflow from the rest of the page

2. **Add `.msg-row` CSS class**
   - `content-visibility: auto` -- browser skips rendering off-screen rows
   - `contain-intrinsic-size: 0 60px` -- estimated height so scroll bar stays stable
   - `contain: layout style` -- isolates per-row reflow

3. **Update `.message-bubble` max-width** (line 66)
   - Change from `calc(100vw - 32px)` to `min(600px, 80vw)`
   - Remove the `@media (min-width: 768px)` block (lines 74-78) -- no longer needed

4. **Add `.msg-row` class to template elements:**
   - Service message div (line 928)
   - Regular message div (line 935)
   - Date separator div (line 1166)

5. **Test scroll behavior:**
   - Open a chat with 100+ messages
   - Scroll up quickly -- verify no visual glitches
   - Use scroll-to-bottom button -- verify it still works
   - Send a message via WebSocket -- verify it appears at bottom

6. **Test content-visibility compatibility:**
   - Verify `message-highlight-flash` animation still fires on search result scroll
   - Verify `scrollToMessage()` function works (forces element into view)

## Todo List
- [ ] Add `contain: strict` to `.messages-scroll`
- [ ] Create `.msg-row` CSS with `content-visibility: auto` + `contain-intrinsic-size`
- [ ] Update `.message-bubble` max-width to `min(600px, 80vw)`
- [ ] Remove `@media (min-width: 768px)` override for `.message-bubble`
- [ ] Add `.msg-row` class to service message template
- [ ] Add `.msg-row` class to regular message template
- [ ] Add `.msg-row` class to date separator template
- [ ] Test: scroll-to-bottom works with content-visibility
- [ ] Test: message highlight animation works after search
- [ ] Test: scroll-to-message works (reply click, search result)
- [ ] Test: WebSocket new message appears correctly
- [ ] Test: mobile viewport 375px bubble width ~300px
- [ ] Test: 500+ messages chat -- verify performance improvement

## Success Criteria
- Chrome DevTools Performance tab shows reduced paint/layout time for 500+ message chats
- Bubble width is ~80% of viewport on mobile devices
- All scroll-related features work: scroll-to-bottom, scroll-to-message, load-more sentinel
- `message-highlight-flash` animation fires correctly after search
- No visual regression on desktop

## Risk Assessment
| Risk | Impact | Mitigation |
|------|--------|------------|
| `content-visibility: auto` breaks `flex-col-reverse` scroll anchoring | High | Test thoroughly; if broken, remove content-visibility and keep only `contain` |
| `contain: strict` on scroll container prevents overflow detection | Medium | Test scroll sentinel IntersectionObserver still triggers; if not, downgrade to `contain: layout paint` |
| `contain-intrinsic-size: 0 60px` causes scroll jump on album/media messages | Medium | Could increase to `0 120px` for better estimate; or use different size for media-heavy chats |
| `min(600px, 80vw)` not supported in very old browsers | None | `min()` supported since Chrome 79, FF 75, Safari 11.1 -- all within target |

## Security Considerations
- CSS-only changes -- no security implications
- No new data exposure or API calls

## Next Steps
- Phase 4: Polish & grouping (consecutive message tail hiding)
- If `content-visibility` causes issues, document and defer to future release
