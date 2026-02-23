# Telegram Chat UI Rendering Patterns - Research Report
Date: 2026-02-17 | Codebase: Telegram-Archive feat/web-viewer-enhancements

---

## 1. Chat Bubble Layout

**Telegram's real color scheme (dark mode):**
- Outgoing (own): `#2b5278` (blue-teal) — right-aligned
- Incoming (other): `#182533` (dark navy) — left-aligned
- Border-radius: `12px` uniform (no asymmetric tails in this impl)

**Current codebase state:**
```css
.message-bubble {
    display: inline-block;
    max-width: calc(100vw - 32px);  /* mobile */
    border-radius: 12px;
}
@media (min-width: 768px) { max-width: 600px; }
```
Layout uses `flex justify-end` for own, `flex justify-start` for others. Sender name shown only for groups, only on first message in a sequence.

**Gap from real Telegram:**
- No bubble tail/arrow (Telegram uses SVG path or CSS pseudo-element for the little triangle)
- Real Telegram uses asymmetric radius: outgoing bottom-right corner = `4px` (where tail attaches); incoming bottom-left = `4px`
- Tailwind config defines colors correctly but CSS doesn't apply per-message dynamically — uses `getSenderStyle(msg)` inline style function

---

## 2. Media Thumbnails Inline

**Telegram's approach:**
- Photos/videos render directly in bubble, no click required to see thumbnail
- Aspect ratio preserved; max width ~420px on desktop, full-width on mobile
- Blur placeholder (low-res thumb) shown while full image loads
- `loading="lazy"` on `<img>` tags

**Current impl:**
```html
<img :src="getMediaUrl(albumMsg)" loading="lazy"
     class="w-full h-full object-cover"
     @error="handleImageError($event, albumMsg)">
<video preload="metadata">...</video>
```
- Uses native `loading="lazy"` — correct
- No blur placeholder implemented (gap)
- `preload="metadata"` for video — correct (loads first frame for thumbnail)
- Video shows play button overlay via absolute positioned SVG

**Gap:** No `IntersectionObserver`-based lazy loading or blur-up placeholder pattern.

---

## 3. Album/Grouped Media Grid

**Telegram's grid logic (real):**
- 1 item: full-width, up to 16:9 aspect ratio capped
- 2 items: 2-column grid, 1:1 squares
- 3 items: left item spans 2 rows (tall), right col has 2 stacked squares
- 4 items: 2×2 grid
- 5 items: 2+3 layout or 3+2
- 6+ items: 3-column grid

**Current impl:**
```css
.album-grid { max-width: 400px; }
.album-grid .grid { border-radius: 12px; overflow: hidden; }
.album-item { aspect-ratio: 1; min-height: 80px; max-height: 200px; }
/* 3-item: first spans 2 rows */
.album-grid .grid-cols-2:has(.album-item:nth-child(3):last-child) .album-item:first-child {
    grid-row: span 2;
}
```
Uses `getAlbumGridClass(album)` Vue function — likely returns `grid-cols-2` for 2-4 items.

**Gaps:**
- `:has()` CSS selector has limited browser support (Chrome 105+, no Firefox <121)
- 5+ item layout not specifically handled
- `max-height: 200px` cap causes distortion on tall displays

---

## 4. Mobile Responsiveness

**Current patterns (good):**
- `viewport-fit=cover` + iOS safe area env vars (`--sat`, `--sab`, etc.)
- `-webkit-overflow-scrolling: touch` on messages container
- `overscroll-behavior-y: contain` to prevent parent scroll bleed
- `overflow-x: hidden` on body/app
- Sidebar hidden on mobile when chat selected (`hidden md:flex`)
- Back button to return to chat list

**Touch targets:**
- Scroll-to-bottom button: `44×44px` — correct (Apple HIG min)
- Chat list items: `p-3` padding — adequate

**Gap:** No swipe-to-go-back gesture (mobile-native feel). No pull-to-refresh.

---

## 5. Performance Patterns

**Current impl:**
- Native `loading="lazy"` on `<img>` — correct, browser-native
- `preload="metadata"` on video — efficient
- `isHiddenAlbumMessage()` — hides duplicate album messages in DOM (shows only first)
- Skeleton loading for chat list (pulsing gray boxes)
- No virtual scrolling — entire message list in DOM

**Real Telegram patterns:**
- IntersectionObserver for custom lazy loading with blur-up
- Virtual DOM / windowed list for 1000+ messages
- Message batching (load 50 at a time, prepend older on scroll up)
- Offscreen images unloaded from GPU memory

**Gaps:**
- No virtual/windowed list — will degrade at 500+ messages
- No IntersectionObserver wrapper (custom lazy load needed for blur placeholders)
- No "load more" / pagination — unclear if API paginates

---

## Key Findings Summary

| Feature | Current State | Gap |
|---|---|---|
| Bubble colors | Correct (`#2b5278` / `#182533`) | No tail/arrow shape |
| Bubble radius | Uniform 12px | Asymmetric corner (4px at tail) |
| Media inline | Yes, lazy | No blur placeholder |
| Album 2-item | Grid cols-2 | OK |
| Album 3-item | `:has()` CSS hack | Limited browser support |
| Album 4+ | Likely cols-2 | 5+ not verified |
| Mobile safe area | env() vars | No swipe gestures |
| Lazy loading | Native only | No IntersectionObserver |
| Virtual scroll | None | Needed for large chats |

---

## Unresolved Questions

1. Does `getAlbumGridClass()` handle 5+ items? Need to inspect Vue JS code.
2. What's the exact `isHiddenAlbumMessage()` logic — important for dedup correctness.
3. Does the API paginate messages or return all at once? (Critical for virtual scroll decision.)
4. Are `grouped_id` messages always consecutive in DB query order?
5. What's the `:has()` fallback behavior on older Firefox — does album 3-item layout break?
