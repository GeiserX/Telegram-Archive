# Code Review: Telegram-Native Chat Rendering

**Report ID:** code-reviewer-260217-2221-chat-rendering
**Commit:** 8456e7b feat(web): enhance viewer with search, media gallery, and UX improvements
**Scope:** `src/web/templates/index.html` (581 lines changed), `src/web/main.py` (131 lines changed)
**Focus:** CSS bubble rendering, JS grouping logic, template bindings, edge cases, performance

---

## Overall Assessment

Solid implementation of 4 phases bringing the viewer closer to Telegram-native look. CSS bubble tails via `clip-path` are clever and lightweight. The `isLastInSenderGroup` logic correctly accounts for `flex-col-reverse` indexing. Several issues found: one critical (XSS surface in global search), one high-priority (sender filter type mismatch), and multiple medium-priority items (dead code, missing cleanup, `contain:strict` risk).

---

## Critical Issues

### 1. XSS via `v-html` on unsanitized search snippets

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, line 481
```html
<div class="text-xs text-gray-300 truncate" v-html="highlightText(r.text_snippet, searchQuery)"></div>
```

**Problem:** `highlightText()` applies regex replacement on `text_snippet` returned from the backend `/api/search` endpoint. Unlike `linkifyText()` which now properly calls `escapeHtml()` first (good fix at line 3395), `highlightText()` does NOT escape HTML before injecting. If `text_snippet` contains user-controlled HTML/script tags from stored messages, they will be rendered.

**Impact:** Stored XSS. A malicious message containing `<img onerror=alert(1)>` in the database would execute when someone searches for it.

**Fix:**
```javascript
const highlightText = (text, query) => {
    if (!query || !text) return text
    const escaped = escapeHtml(text)  // ADD THIS LINE
    const queryEscaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return escaped.replace(new RegExp(`(${queryEscaped})`, 'gi'), '<mark class="bg-yellow-400/30 text-inherit rounded px-0.5">$1</mark>')
}
```

**Note:** The same issue applies at line 1226 when `highlightText` wraps `linkifyText`. The `linkifyText` output is already HTML (contains `<a>` tags), so `highlightText` must NOT escape it again there. Consider two variants: `highlightText` (escapes first, for raw text) and `highlightHtml` (for pre-escaped HTML).

---

## High Priority

### 2. Sender filter sends name string, backend expects integer `sender_id`

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, line 2770
```javascript
if (f.sender) url += `&sender_id=${encodeURIComponent(f.sender)}`
```

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/main.py`, line 699
```python
sender_id: int | None = None,
```

**Problem:** The search filter UI has a text input with placeholder "Sender name..." (line 267), suggesting users type a name. But the frontend passes this value as `sender_id` query parameter, and the backend expects an integer. Passing a name string to an `int` parameter will cause a 422 validation error from FastAPI.

**Impact:** Sender filter in message view is completely broken. Users get validation errors.

**Fix:** Either:
- (a) Change the input to accept sender IDs (bad UX), or
- (b) Add a `sender_name` parameter to the backend that does a LIKE match on sender name, or
- (c) On the frontend, lookup the sender ID from a name before passing it

### 3. `contain: strict` on scroll container breaks `flex-col-reverse` sizing

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, line 133
```css
.messages-scroll {
    -webkit-overflow-scrolling: touch;
    overscroll-behavior-y: contain;
    contain: strict;
}
```

**Problem:** `contain: strict` is equivalent to `contain: size layout style paint`. The `size` containment means the element's intrinsic size is treated as zero in both dimensions, ignoring its children. Combined with `flex-col-reverse`, this can cause the container to collapse to zero height on some browsers, since the browser cannot use content to determine size.

The container already has `h-full` from its parent class (line 988: `class="h-full overflow-y-auto ..."`), which provides an explicit height, partially mitigating this. However, `contain: strict` with `flex-col-reverse` is known to cause scrolling glitches in Safari and older Chrome.

**Impact:** Potential blank/collapsed message area on Safari, or scroll position jumps.

**Fix:** Downgrade to `contain: layout paint` (remove `strict`, avoid `size` containment):
```css
.messages-scroll {
    -webkit-overflow-scrolling: touch;
    overscroll-behavior-y: contain;
    contain: layout paint;
}
```

### 4. `content-visibility: auto` on `.msg-row` conflicts with `contain: layout style`

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, lines 137-141
```css
.msg-row {
    content-visibility: auto;
    contain-intrinsic-size: 0 60px;
    contain: layout style;
}
```

**Problem:** `content-visibility: auto` already implies `contain: layout style paint` when the element is off-screen. The explicit `contain: layout style` is applied even when the element IS on-screen, which means on-screen messages also have layout containment. This prevents messages from influencing each other's layout, which is generally fine for this use case, but combined with `flex-col-reverse`, it can cause issues with scroll position restoration and `scrollIntoView` (used by `scrollToMessage`).

Additionally, `contain-intrinsic-size: 0 60px` means the browser estimates 60px per row. Messages with images, albums, or long text will be significantly taller, causing visible layout shifts as messages scroll into view and expand from 60px to actual height.

**Impact:** Jarring layout shifts when scrolling, especially for media-heavy chats.

**Fix:** Increase the intrinsic size estimate or remove explicit `contain`:
```css
.msg-row {
    content-visibility: auto;
    contain-intrinsic-size: auto 120px; /* 'auto' remembers last-rendered size */
}
```

---

## Medium Priority

### 5. Dead code: `highlightedMessageId` and `message-highlight-flash` never connected

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`
- CSS class `message-highlight-flash` defined at line 257 but never applied in template
- `highlightedMessageId` ref set in `copyMessageLink()` (line 2955) but never bound to any `:class`

**Problem:** The copy-link visual feedback is incomplete. The ref is set and cleared after 1500ms, but no DOM element reacts to it.

**Fix:** Add to the message bubble `:class` binding:
```html
<div class="message-bubble ..."
    :class="[
        isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming',
        !isLastInSenderGroup(index) ? 'no-tail' : '',
        highlightedMessageId === msg.id ? 'message-highlight-flash' : ''
    ]"
```

### 6. Sticker messages get bubble background and tails

**Problem:** Sticker messages are rendered inside the same `.message-bubble` div with `bubble-outgoing/bubble-incoming` classes. Stickers in Telegram are rendered without a bubble background - they float freely. With this change, stickers now show a colored bubble with a tail, which looks non-native.

**Impact:** Visual regression for sticker messages.

**Fix:** Add a condition to skip bubble classes for sticker messages:
```javascript
// In the :class binding
isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming',
// becomes:
msg.media?.type !== 'sticker' ? (isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming') : '',
```
Also add transparent/no background for stickers via `getSenderStyle`.

### 7. `isLastInSenderGroup` does not account for hidden album messages

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, lines 3097-3103
```javascript
const isLastInSenderGroup = (index) => {
    const currMsg = sortedMessages.value[index]
    if (index === 0) return true
    const newerMsg = sortedMessages.value[index - 1]
    return newerMsg.sender_id !== currMsg.sender_id
}
```

**Problem:** Hidden album messages (those filtered by `isHiddenAlbumMessage`) are still in `sortedMessages` and their indices are counted. If messages A (visible), B (hidden album), C (visible) are consecutive from same sender, `isLastInSenderGroup` for A would check B (hidden) instead of C. The result is still correct if B has the same sender_id, which albums always do (same sender). So this is technically fine for albums but fragile if filtering logic changes.

**Impact:** Low risk currently. May break if new message filtering is added.

### 8. Event listeners not cleaned up on unmount

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, line 2028
```javascript
document.addEventListener('keydown', handleGlobalKeydown)
// ...
window.addEventListener('hashchange', async () => { ... })
```

**Problem:** No `onBeforeUnmount` / `onUnmounted` hook removes these listeners. The `hashchange` listener uses an anonymous arrow function, making it impossible to remove. While the SPA likely never unmounts the root component, this is a memory leak pattern that would cause issues if the component were ever conditionally rendered.

**Fix:**
```javascript
const hashChangeHandler = async () => { /* ... */ }
window.addEventListener('hashchange', hashChangeHandler)
onBeforeUnmount(() => {
    document.removeEventListener('keydown', handleGlobalKeydown)
    window.removeEventListener('hashchange', hashChangeHandler)
})
```

### 9. Vue reactivity bypass in global search toggle

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, line 464
```html
<button @click="globalSearchMode = !globalSearchMode; if(!globalSearchMode) globalSearchResults = {results:[],total:0}"
```

**Problem:** `globalSearchResults` is a `ref()`. Assigning a plain object via `globalSearchResults = {results:[],total:0}` replaces the ref itself rather than updating `.value`. This works in Vue 3 template context (auto-unwrapped), but the behavior is inconsistent with how the variable is used elsewhere (via `.value` in JS). This could silently break reactivity depending on Vue's template compiler behavior.

**Fix:** Use `.value`:
```html
@click="globalSearchMode = !globalSearchMode; if(!globalSearchMode) globalSearchResults.results = []; globalSearchResults.total = 0"
```

### 10. `msg-grouped` negative margin creates overlapping click targets

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, lines 119-121
```css
.msg-row.msg-grouped {
    margin-bottom: -2px;
}
```

**Problem:** Negative margin causes grouped messages to overlap by 2px. With `flex-col-reverse`, `margin-bottom` pushes elements upward (visually). The overlap may cause issues with touch targets on mobile - tapping the edge of one bubble could trigger the click handler of the overlapping bubble below.

**Impact:** Minor UX issue on touch devices.

**Fix:** Use `gap` reduction instead of negative margin, or ensure `position: relative` with proper `z-index` stacking.

---

## Low Priority

### 11. `escapeHtml` uses DOM createElement - works but creates garbage

**File:** `/home/dgx/Desktop/tele-private/Telegram-Archive/src/web/templates/index.html`, lines 3390-3394
```javascript
const escapeHtml = (text) => {
    const div = document.createElement('div')
    div.textContent = text
    return div.innerHTML
}
```

This is functionally correct and safe but creates a DOM element per call. For search highlighting on every message render, a string-replace approach would be more efficient:
```javascript
const escapeHtml = (text) => text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
```

### 12. `album-3` CSS relies on implicit grid auto-flow

The album-3 layout uses `grid-cols-2` with `grid-row: span 2` on the first child. This relies on CSS Grid auto-placement to flow the 2nd and 3rd items into the second column. The behavior is correct with the default `grid-auto-flow: row` but should be explicitly set for clarity.

### 13. Footer attribution change

The footer was changed from the original author credit to "Made with frustration by phenix" with a different GitHub link. This is not a code quality issue, but worth noting for the project maintainer to confirm this is intentional.

---

## Positive Observations

1. **`linkifyText` XSS fix** - The addition of `escapeHtml()` before URL linkification (line 3395) correctly prevents XSS through message text. Previously, raw text was passed through regex and injected via `v-html`. Good security improvement.

2. **`rel="noopener noreferrer"`** added to linkified URLs - prevents reverse tabnabbing.

3. **`decoding="async"`** on images - correct use for non-blocking image decoding, improves scroll performance.

4. **`isLastInSenderGroup` logic** - Correctly inverts `showSenderName` logic for bottom-tail placement. The `flex-col-reverse` indexing (index 0 = newest/bottom) is properly handled.

5. **`getAlbumLayoutClass` rename** - Better name than `getAlbumGridClass`. The `album-3` class approach is more reliable than the previous `:has()` pseudo-class selector, which has inconsistent browser support.

6. **Skeleton loading** - Nice UX improvement over plain "Loading..." text for both chat list and messages.

7. **Hash-based routing** - Clean implementation with `parseHash`/`updateHash` for deep linking.

8. **Keyboard shortcuts** - Properly guards against firing when typing in inputs (line 2931).

---

## Recommended Actions (Prioritized)

1. **[Critical]** Fix XSS in `highlightText` - add `escapeHtml` for raw text inputs, keep separate path for pre-escaped HTML
2. **[High]** Fix sender filter type mismatch - backend expects `int`, frontend sends `string` name
3. **[High]** Change `contain: strict` to `contain: layout paint` on `.messages-scroll`
4. **[High]** Use `contain-intrinsic-size: auto 120px` instead of `0 60px` on `.msg-row`
5. **[Medium]** Connect `highlightedMessageId` to template `:class` binding
6. **[Medium]** Suppress bubble tails/background for sticker messages
7. **[Medium]** Extract `hashchange` handler to named function for cleanup
8. **[Medium]** Fix Vue reactivity in global search toggle button
9. **[Low]** Replace DOM-based `escapeHtml` with string-replace version
10. **[Low]** Investigate `msg-grouped` negative margin on touch devices

---

## Metrics

| Metric | Value |
|--------|-------|
| Lines Changed | ~581 (template) + ~131 (backend) |
| New CSS Rules | 14 |
| New JS Functions | 13 |
| New Vue Refs | 11 |
| Dead Code Items | 2 (CSS class + ref unused) |
| XSS Vectors | 1 (search snippet v-html) |
| API Mismatches | 1 (sender_id type) |
| Browser Compat Risks | 2 (contain:strict + content-visibility) |

---

## Unresolved Questions

1. Is the footer attribution change from "Sergio Fernandez" to "phenix" intentional and authorized by the project owner?
2. Does the `/api/search` endpoint return raw message text in `text_snippet`, or is it pre-sanitized? If pre-sanitized, the XSS risk in `highlightText` is reduced but still present for edge cases.
3. Are there plans to add `sender_name` search to the backend, or should the frontend be changed to a sender-ID picker?
