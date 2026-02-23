# Phase 2: Media Thumbnails & Albums

## Context Links
- [Plan overview](plan.md)
- [Phase 1: Bubble Layout](phase-01-bubble-layout-and-tails.md) (prerequisite)
- [Telegram UI Patterns Research](research/researcher-01-telegram-ui-patterns.md)
- [CSS Implementation Research](research/researcher-02-css-chat-implementation.md)
- Target: `src/web/templates/index.html` lines 234-254 (album CSS), 998-1135 (media template), 2395-2436 (album JS)

## Overview
- **Priority:** P0 (video thumbnails), P1 (album grids, aspect ratio)
- **Status:** complete
- **Description:** Fix video thumbnail rendering, improve album grid layouts for 1-5+ items matching Telegram's mosaic, and add aspect-ratio CSS to prevent layout shift.

## Key Insights

### Video Thumbnail Problem
- Current: `<video preload="metadata">` relies on browser extracting first frame -- unreliable
- Chrome: usually works. Safari: often shows black until play. Firefox: inconsistent
- Album videos (line 1024-1028): no `poster` attribute, no play indication beyond overlay SVG
- Standalone videos (line 1072-1088): same `preload="metadata"` issue
- **Fix:** Add `poster` attribute using same media URL with `#t=0.1` fragment, or add a CSS fallback background

### Video Thumbnail Strategy
- `preload="metadata"` + `poster` attribute cannot both point to same video URL efficiently
- Best approach: add a CSS gradient/icon background to the video container so there's always a visible placeholder
- The `#t=0.1` trick (`<video src="url#t=0.1">`) works in Chrome/Firefox to seek to 0.1s -- but not in Safari
- Simplest reliable fix: style the video container with a dark gradient + centered play icon so even without a rendered frame, the user sees a clickable video placeholder

### Album Grid Gaps
- Current `getAlbumGridClass()` returns: 1->cols-1, 2->cols-2, 3->cols-2, 4->cols-2, 5+->cols-3
- Missing: 3-item layout (first item tall left, two stacked right) needs `grid-row: span 2` on first child
- Current `:has()` CSS selector for 3-item layout (line 252) -- Firefox <121 doesn't support `:has()`
- 5+ items: `grid-cols-3` is adequate but `aspect-ratio: 1` with `max-height: 200px` causes inconsistent sizing

### Aspect Ratio for Images
- Current photos: `max-h-64 max-w-full object-cover` (line 1044) -- fixed height crops tall images
- Better: let aspect ratio flow naturally with a max-width cap (like Telegram's ~320px width cap)

## Requirements

### Functional
1. Video thumbnails always show a visible placeholder (gradient bg + play icon) even when browser doesn't render frame
2. Album grid: 1 item = full-width, 2 = side-by-side, 3 = 1 tall + 2 stacked, 4 = 2x2, 5+ = 3-col grid
3. Regular (non-album) photos preserve aspect ratio without harsh `max-h-64` crop
4. Add `decoding="async"` to all `<img>` tags for non-blocking decode

### Non-functional
- No JavaScript changes for video thumbnail rendering (CSS-only fallback)
- Album grid must work without `:has()` selector (Firefox <121 fallback)
- No new network requests for thumbnails (no server-side thumb generation)

## Architecture

### CSS Changes

**Replace album CSS (lines 234-254):**
```css
/* Album Grid Styles */
.album-grid {
    max-width: 400px;
}
.album-grid .grid {
    border-radius: 12px;
    overflow: hidden;
}
.album-item {
    aspect-ratio: 1;
    min-height: 80px;
    overflow: hidden;
}
.album-item img,
.album-item video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
}

/* 3-item album: first item spans 2 rows */
.album-grid .album-3 .album-item:first-child {
    grid-row: span 2;
}

/* Video placeholder background for albums */
.album-item-video {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
}

/* Standalone video container */
.video-container {
    position: relative;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    border-radius: 12px;
    overflow: hidden;
    min-height: 120px;
}

/* Regular photo: preserve aspect ratio */
.msg-photo {
    border-radius: 12px;
    max-width: 400px;
    width: 100%;
    height: auto;
    object-fit: contain;
    cursor: pointer;
}
```

### Template Changes

**Album grid (line 1015) -- use explicit class instead of `:has()` hack:**

Current:
```html
<div class="grid gap-1" :class="getAlbumGridClass(getAlbumForMessage(msg))">
```

New:
```html
<div class="grid gap-1" :class="getAlbumLayoutClass(getAlbumForMessage(msg))">
```

**Album video item (line 1024-1028) -- add placeholder class:**
```html
<div class="album-item relative overflow-hidden rounded-lg cursor-pointer hover:opacity-90 transition"
    :class="{ 'album-item-video': albumMsg.media?.type === 'video' }"
    @click="openMedia(albumMsg)">
```

**Regular photo (line 1042-1045) -- use `.msg-photo` class:**
```html
<img v-else-if="msg.media?.type === 'photo' && !msg.raw_data?.grouped_id"
    :src="getMediaUrl(msg)"
    loading="lazy"
    decoding="async"
    class="msg-photo hover:opacity-90 transition"
    @click="openMedia(msg)" @error="handleImageError($event, msg)">
```

**Standalone video (lines 1072-1088) -- wrap in `.video-container`:**
```html
<div v-else-if="msg.media?.type === 'video'"
    class="video-container relative cursor-pointer group"
    @click="openMedia(msg)">
    <video preload="metadata"
        class="rounded-lg max-h-64 w-full pointer-events-none"
        @error="handleMediaError($event, msg)">
        <source :src="getMediaUrl(msg)" type="video/mp4">
    </video>
    <!-- Play overlay (existing) -->
    <div class="absolute inset-0 flex items-center justify-center bg-black/20 group-hover:bg-black/40 transition rounded-lg">
        <div class="w-14 h-14 bg-white/90 rounded-full flex items-center justify-center shadow-lg">
            <svg class="w-8 h-8 text-gray-800 ml-1" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z"/>
            </svg>
        </div>
    </div>
</div>
```

### JS Changes

**Replace `getAlbumGridClass` (lines 2426-2436) with `getAlbumLayoutClass`:**

```javascript
const getAlbumLayoutClass = (album) => {
    if (!album) return 'grid-cols-1'
    const count = album.length
    switch (count) {
        case 1: return 'grid-cols-1'
        case 2: return 'grid-cols-2'
        case 3: return 'grid-cols-2 album-3'  // triggers .album-3 CSS for row span
        case 4: return 'grid-cols-2'           // 2x2
        default: return 'grid-cols-3'          // 3-col for 5+
    }
}
```

This replaces the `:has()` CSS selector approach with an explicit class (`album-3`), fixing Firefox compatibility.

Also rename in the return block (line 3517):
```javascript
getAlbumLayoutClass,  // was getAlbumGridClass
```

## Related Code Files
- `src/web/templates/index.html`
  - CSS: lines 234-254 (album grid styles)
  - Template: lines 1012-1045 (album + photo rendering), 1072-1088 (standalone video)
  - JS: lines 2395-2436 (album functions), 3508-3517 (return block)

## Implementation Steps

1. **Update album CSS** (lines 234-254)
   - Remove `.album-item { max-height: 200px }` -- let grid handle sizing
   - Remove `:has()` selector rule (line 252-254)
   - Add `.album-3 .album-item:first-child { grid-row: span 2 }`
   - Add `.album-item-video` background gradient
   - Add `.video-container` class with gradient + min-height

2. **Add `.msg-photo` CSS class**
   - `max-width: 320px`, `border-radius: 12px`, `height: auto`
   - Replaces Tailwind `max-h-64 max-w-full object-cover`

3. **Rename `getAlbumGridClass` to `getAlbumLayoutClass`** in JS (line 2426)
   - Add `album-3` class for 3-item layouts
   - Update return block reference (line 3517)

4. **Update album template** (line 1015)
   - Change `:class="getAlbumGridClass(...)"` to `:class="getAlbumLayoutClass(...)"`

5. **Add `album-item-video` class** to album video items (line 1017)
   - Conditional `:class` based on `albumMsg.media?.type === 'video'`

6. **Update regular photo template** (line 1042-1045)
   - Replace Tailwind classes with `.msg-photo`
   - Add `decoding="async"` attribute

7. **Wrap standalone video in `.video-container`** (line 1072-1088)
   - Add the class to existing wrapper div
   - Gradient background ensures visible placeholder even without frame render

8. **Add `decoding="async"` to all `<img>` tags** in message template
   - Album images (line 1020), regular photos (line 1042), document images (line 1104), stickers (line 1093)

## Todo List
- [ ] Remove `:has()` CSS hack, add `.album-3` class-based rule
- [ ] Remove `max-height: 200px` from `.album-item`
- [ ] Add `.album-item-video` CSS with gradient background
- [ ] Add `.video-container` CSS with gradient background + min-height
- [ ] Add `.msg-photo` CSS class for standalone photos
- [ ] Rename `getAlbumGridClass` -> `getAlbumLayoutClass` in JS + return block
- [ ] Update album template to use `getAlbumLayoutClass`
- [ ] Add `:class` for video items in album template
- [ ] Wrap standalone video in `.video-container`
- [ ] Update regular photo to use `.msg-photo` class
- [ ] Add `decoding="async"` to all `<img>` tags
- [ ] Test: album with 2 photos
- [ ] Test: album with 3 photos (first item should be tall)
- [ ] Test: album with 4+ photos
- [ ] Test: standalone video placeholder shows before frame loads
- [ ] Test: regular photo preserves aspect ratio (no harsh crop)

## Success Criteria
- Video containers always show visible content (gradient bg) even when browser doesn't render first frame
- Album 3-item layout: first item tall (spans 2 rows), two stacked on right
- Regular photos display at natural aspect ratio, max 400px wide
- No `:has()` CSS dependency -- works on Firefox <121
- All images have `decoding="async"` for non-blocking decode
- Lightbox click-to-open still works for all media types

## Risk Assessment
| Risk | Impact | Mitigation |
|------|--------|------------|
| Removing `max-height: 200px` from album items causes oversized grids | Medium | Grid `aspect-ratio: 1` constrains cells; test with portrait/landscape mixes |
| `.msg-photo` `max-width: 400px` too narrow on desktop | Low | Validated at 400px; can increase to 420px if needed |
<!-- Updated: Validation Session 1 - photo max-width changed from 320px to 400px -->
| Renaming JS function breaks other references | Low | Search all usages -- only in template (line 1015) and return block (line 3517) |
| Video gradient bg looks out of place if video frame loads fast | None | Gradient only visible during the brief load time -- acceptable |

## Security Considerations
- No new user-input rendering
- No new API endpoints
- `decoding="async"` is a standard HTML attribute -- no security implications

## Next Steps
- Phase 3: Mobile responsive tweaks and performance (content-visibility)
- Test album grids with real data before Phase 3
