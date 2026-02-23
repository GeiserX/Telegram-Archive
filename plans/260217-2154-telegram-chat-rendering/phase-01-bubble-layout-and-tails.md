# Phase 1: Bubble Layout & Tails

## Context Links
- [Plan overview](plan.md)
- [Telegram UI Patterns Research](research/researcher-01-telegram-ui-patterns.md)
- [CSS Implementation Research](research/researcher-02-css-chat-implementation.md)
- Target: `src/web/templates/index.html` lines 63-100 (CSS), 935-937 (template)

## Overview
- **Priority:** P0
- **Status:** complete
- **Description:** Add CSS bubble tails, asymmetric border-radius, and refine left/right alignment to match native Telegram dark mode.

## Key Insights
- Telegram outgoing bubbles have a small triangle tail on bottom-right, incoming on bottom-left
- Asymmetric radius: 4px on the tail corner, 12px on the other 3 corners
- Current colors already match Telegram dark mode: outgoing `hsla(210,60%,28%,0.95)`, incoming `hsla(220,20%,25%,0.80)`
- `clip-path: polygon()` on `::after`/`::before` pseudo-elements is simpler and more reliable than border-triangle tricks
- The tail pseudo-element inherits `background` from the bubble via `background: inherit`
- Skeleton loading already uses `rounded-br-md` / `rounded-bl-md` (line 917) -- shows asymmetric corners were already considered

## Requirements

### Functional
1. Outgoing bubbles: tail on bottom-right, 4px bottom-right radius, 12px on other corners
2. Incoming bubbles: tail on bottom-left, 4px bottom-left radius, 12px on other corners
3. Service messages: no tail (centered, pill-shaped -- already correct)
4. Sticker messages: no tail (no bubble background)

### Non-functional
- CSS-only solution (no JS changes for tail rendering)
- Must not break existing `inline-block` + `flex justify-end/start` layout
- Must work with `flex-col-reverse` parent container

## Architecture

### CSS Changes (lines 63-100)

**Modify `.message-bubble`:**
```css
.message-bubble {
    display: inline-block;
    max-width: min(600px, calc(100vw - 80px)); /* tighter for avatar space */
    width: auto;
    word-wrap: break-word;
    overflow-wrap: anywhere;
    border-radius: 12px;
    position: relative; /* for ::after pseudo-element */
}
```

**Add new classes:**
```css
/* Outgoing bubble: tail bottom-right */
.bubble-outgoing {
    border-bottom-right-radius: 4px;
}
.bubble-outgoing::after {
    content: '';
    position: absolute;
    bottom: 0;
    right: -6px;
    width: 10px;
    height: 10px;
    background: inherit;
    clip-path: polygon(0 0, 0 100%, 100% 100%);
}

/* Incoming bubble: tail bottom-left */
.bubble-incoming {
    border-bottom-left-radius: 4px;
}
.bubble-incoming::before {
    content: '';
    position: absolute;
    bottom: 0;
    left: -6px;
    width: 10px;
    height: 10px;
    background: inherit;
    clip-path: polygon(100% 0, 0 100%, 100% 100%);
}
```

### Template Changes (line 936)

**Current:**
```html
<div class="message-bubble p-3 text-sm shadow-sm text-gray-100 group"
    :style="getSenderStyle(msg)">
```

**New:**
```html
<div class="message-bubble p-3 text-sm shadow-sm text-gray-100 group"
    :class="isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming'"
    :style="getSenderStyle(msg)">
```

### Mobile Responsive Tweak

```css
@media (min-width: 768px) {
    .message-bubble {
        max-width: 600px;
    }
}
```
The `min()` function on mobile already handles `calc(100vw - 80px)`, so the media query override stays for desktop.

## Related Code Files
- `src/web/templates/index.html`
  - CSS: lines 63-78 (`.message-bubble` definition)
  - Template: line 936 (bubble div)
  - JS: lines 3054-3065 (`getMessageBackground`), 3084-3088 (`getSenderStyle`)

## Implementation Steps

1. **Add `position: relative` to `.message-bubble`** (line 65)
   - Required for pseudo-element positioning
   - Already has `display: inline-block` -- compatible

2. **Update `max-width`** on `.message-bubble` (line 66)
   - Change from `calc(100vw - 32px)` to `min(600px, calc(100vw - 80px))`
   - Removes need for separate `@media` override (can keep for clarity)

3. **Add `.bubble-outgoing` CSS class** after `.message-bubble` block
   - `border-bottom-right-radius: 4px`
   - `::after` pseudo-element with `clip-path: polygon(0 0, 0 100%, 100% 100%)`
   - Position: `bottom: 0; right: -6px`
   - Size: `10px x 10px`
   - `background: inherit` to pick up bubble color

4. **Add `.bubble-incoming` CSS class** after `.bubble-outgoing`
   - `border-bottom-left-radius: 4px`
   - `::before` pseudo-element with `clip-path: polygon(100% 0, 0 100%, 100% 100%)`
   - Position: `bottom: 0; left: -6px`
   - Size: `10px x 10px`
   - `background: inherit`

5. **Add dynamic class binding to template** (line 936)
   - Add `:class="isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming'"`
   - `isOwnMessage()` already exists and returns correct values

6. **Verify skeleton loading alignment** (line 917)
   - Already uses `rounded-br-md` / `rounded-bl-md` -- consistent with new asymmetric corners

7. **Test edge cases:**
   - Service messages (line 928): no bubble class applied -- unaffected
   - Album messages (line 1014): album-grid is inside bubble -- tail on parent bubble, not album items
   - Sticker messages (line 1091): inside the bubble div -- will get tail. Consider adding `no-tail` class for stickers

## Todo List
- [x] Add `position: relative` to `.message-bubble`
- [x] Update `max-width` to use `min()` function
- [x] Add `.bubble-outgoing` CSS with `::after` clip-path tail
- [x] Add `.bubble-incoming` CSS with `::before` clip-path tail
- [x] Add `:class` binding on template line 936
- [x] Add `.no-tail` override class (hides pseudo-elements) for sticker messages
- [x] Test with outgoing messages in private chat
- [x] Test with group chat (multiple senders)
- [x] Test on mobile viewport (320px width)
- [x] Verify lightbox click still works through bubble

## Success Criteria
- Outgoing bubbles show right-side tail with 4px bottom-right corner
- Incoming bubbles show left-side tail with 4px bottom-left corner
- Tail color matches bubble background (inherits via `background: inherit`)
- No layout shift or overflow caused by pseudo-elements
- Service messages and stickers have no tails
- Works on Chrome, Firefox 121+, Safari 15.4+

## Risk Assessment
| Risk | Impact | Mitigation |
|------|--------|------------|
| `clip-path` subpixel gaps at certain zoom | Low | Test at 90%/110%/150% zoom; adjust tail size if needed |
| Tail overlaps adjacent content | Low | `right: -6px` is within typical message padding; flex gap is `gap-1` (4px) |
| `background: inherit` not picking up inline style | Medium | Test with `getSenderStyle()` inline `backgroundColor`; fallback: use CSS variable |

## Security Considerations
- No user input rendered in pseudo-elements (CSS-only)
- No new API calls or data exposure

## Next Steps
- Phase 2: Media thumbnails and album grids (depends on bubble layout being stable)
- Phase 4: Consecutive message grouping will hide tails on non-last messages
