# CSS/HTML Implementation: Telegram-Style Chat Interface
**Date:** 2026-02-17 | **Context:** Existing app uses Tailwind + Vue 3, dark theme, existing `.message-bubble` / `.messages-scroll` CSS

---

## 1. Chat Bubble Techniques

### Layout (Flexbox-based, current pattern works)
```css
/* Message row */
.msg-row { display: flex; align-items: flex-end; gap: 8px; padding: 2px 16px; }
.msg-row.outgoing { flex-direction: row-reverse; }

/* Bubble */
.message-bubble {
  max-width: min(600px, calc(100vw - 80px)); /* existing: calc(100vw - 32px), tighten */
  border-radius: 18px;
  padding: 8px 12px;
  position: relative;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.msg-row.incoming .message-bubble { border-bottom-left-radius: 4px; }
.msg-row.outgoing  .message-bubble { border-bottom-right-radius: 4px; }
```

### CSS-only Bubble Tail (pseudo-element, no SVG)
```css
/* Incoming tail */
.msg-row.incoming .message-bubble::before {
  content: '';
  position: absolute;
  bottom: 0; left: -6px;
  width: 10px; height: 10px;
  background: inherit;
  clip-path: polygon(100% 0, 0 100%, 100% 100%);
}
/* Outgoing tail — mirror */
.msg-row.outgoing .message-bubble::after {
  content: '';
  position: absolute;
  bottom: 0; right: -6px;
  width: 10px; height: 10px;
  background: inherit;
  clip-path: polygon(0 0, 0 100%, 100% 100%);
}
```
**Note:** `clip-path` triangle is simpler and more reliable than border-trick tails; no subpixel gaps.

### Timestamp overlay (bottom-right, Telegram style)
```css
.bubble-meta {
  float: right;
  margin-left: 8px;
  margin-top: 4px;
  font-size: 0.7rem;
  color: rgba(255,255,255,0.5);
  white-space: nowrap;
  line-height: 1;
}
```

---

## 2. Responsive Image Thumbnails in Chat

```css
.msg-image-wrap {
  border-radius: 12px;
  overflow: hidden;
  max-width: 320px;        /* cap chat image width */
  aspect-ratio: auto;       /* preserve native ratio */
}
.msg-image-wrap img {
  display: block;
  width: 100%;
  height: auto;
  object-fit: cover;
  loading: lazy;            /* HTML attr, not CSS */
}
```

HTML pattern:
```html
<img src="thumb.jpg"
     srcset="thumb.jpg 320w, full.jpg 800w"
     sizes="(max-width: 600px) 100vw, 320px"
     loading="lazy"
     decoding="async"
     alt="">
```

- `loading="lazy"` + `decoding="async"` — zero-JS lazy load, widely supported (Chrome 76+, FF 75+, Safari 15.4+)
- `aspect-ratio` on wrapper prevents layout shift (CLS) before image loads
- `srcset` optional if serving single thumb — skip if server only generates one size (YAGNI)

---

## 3. CSS Grid for Photo Albums (Telegram-style)

Telegram album layouts:
- **1 photo**: single full-width image
- **2 photos**: 2-column equal split
- **3 photos**: 1 large left + 2 stacked right
- **4+ photos**: 2-column grid, last row may be 3-col

```css
.photo-album {
  display: grid;
  gap: 2px;
  border-radius: 12px;
  overflow: hidden;
  max-width: 320px;
}
/* 2 photos */
.photo-album.count-2 { grid-template-columns: 1fr 1fr; }
/* 3 photos: 1 tall left, 2 stacked right */
.photo-album.count-3 {
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
}
.photo-album.count-3 .album-item:first-child { grid-row: 1 / 3; }
/* 4+ photos: 2-col grid */
.photo-album.count-4  { grid-template-columns: 1fr 1fr; }
.photo-album.count-gt4 { grid-template-columns: repeat(3, 1fr); }

.album-item {
  aspect-ratio: 1;   /* square cells */
  overflow: hidden;
}
.album-item img {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}
/* Overlay "+N more" on last visible item */
.album-item.overflow { position: relative; }
.album-item.overflow::after {
  content: attr(data-more);
  position: absolute; inset: 0;
  background: rgba(0,0,0,0.55);
  display: flex; align-items: center; justify-content: center;
  font-size: 1.4rem; color: #fff; font-weight: 600;
}
```
Apply `data-more="+3"` via Vue `:attr` binding on last visible item when `photos.length > 4`.

---

## 4. Mobile-First CSS

### Touch targets
```css
/* All interactive elements: 44px min (Apple HIG / WCAG 2.5.5) */
.msg-action-btn, .bubble-img, .scroll-to-bottom-btn {
  min-width: 44px;
  min-height: 44px;
}
```
Existing `.scroll-to-bottom-btn` already 44×44 — good.

### Scroll performance
```css
.messages-scroll {
  -webkit-overflow-scrolling: touch;  /* exists */
  overscroll-behavior-y: contain;     /* exists */
  contain: strict;                    /* add: layout + style + paint + size */
  will-change: scroll-position;       /* hint compositor; remove if no perf gain */
}
```

### CSS containment per message row
```css
.msg-row {
  contain: layout style;   /* isolates reflow per row */
}
```

---

## 5. Performance CSS

### content-visibility (virtual scrolling in CSS)
```css
.msg-row {
  content-visibility: auto;
  contain-intrinsic-size: 0 60px;  /* estimated row height to prevent scroll jump */
}
```
**Warning:** `content-visibility: auto` skips rendering off-screen rows. Works great for long chat history (1000+ messages). Supported Chrome 85+, Firefox 125+, Safari 18+. Test that Vue reactivity still triggers re-render on model update — it does (DOM is updated, browser decides paint).

### GPU acceleration for media
```css
.msg-image-wrap, .photo-album {
  transform: translateZ(0);   /* promote to own layer */
  backface-visibility: hidden;
}
```

### Animation budget
```css
/* Only animate opacity/transform — never width/height/top/left */
.message-bubble.new {
  animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
```

---

## Integration Notes for Existing Codebase

| Existing code | Recommendation |
|---|---|
| `max-width: calc(100vw - 32px)` on `.message-bubble` | Change to `min(600px, calc(100vw - 80px))` — accounts for avatar + padding |
| `-webkit-overflow-scrolling: touch` on `.messages-scroll` | Keep; add `contain: strict` |
| No album grid | Add `.photo-album` CSS + Vue computed class `count-{N}` |
| No `content-visibility` | Add to `.msg-row` — biggest perf win for large history |
| No `loading="lazy"` on images | Add to all `<img>` in message templates |

---

## Unresolved Questions

1. Does the server generate image thumbnails at multiple sizes, or single size? (determines if `srcset` is viable)
2. Max photos per album group to render before "+N" overflow? (Telegram uses 10 visible max)
3. Is Safari 18+ the minimum target, or must `content-visibility` be gated behind a feature check?
4. Are sticker/GIF messages rendered differently from photos — need separate bubble radius treatment?
