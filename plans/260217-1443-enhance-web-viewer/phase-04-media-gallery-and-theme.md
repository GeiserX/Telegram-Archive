# Phase 4: Media Gallery

## Context Links

- [plan.md](plan.md)
- [Phase 3: Performance & UX](phase-03-performance-and-ux.md)
- Frontend: `src/web/templates/index.html` (media rendering ~line 870-1010, Tailwind config ~line 248-267)
- DB Adapter: `src/db/adapter.py` (Media model queries)
- Models: `src/db/models.py` (lines 145-184 - Media model)

## Overview

- **Priority:** LOW
- **Status:** complete
- **Effort:** ~1.5h
- **Description:** Grid-view media browser per chat and improved video player. Light theme removed per validation (dark-only).

## Key Insights

- **Media query:** DB has `media` table with `chat_id`, `type`, `file_path`, `width`, `height`. Can query all media for a chat efficiently via `idx_media_message` index.
- **Tailwind dark mode:** Already configured as `darkMode: 'class'` with `class="dark"` on `<html>`. Switching to light requires removing class + defining light color tokens.
- **CSS variables:** Theme colors hardcoded in Tailwind config (`tg.bg: '#0f172a'` etc.) and inline styles. Need to extract to CSS variables for theme switching.
- **Video player:** Currently uses native `<video>` controls. Custom controls would add significant complexity for low value. Better: just improve the lightbox video experience.

## Requirements

### Functional
1. Media gallery view per chat: grid of photos/videos with lazy loading
2. Filter gallery by type (all, photos, videos, documents)
3. Improved lightbox video: show duration, allow seeking, fullscreen button

### Non-Functional
- Gallery loads first 50 media items, infinite scroll for more

## Architecture

### Backend Changes

**New endpoint** (`src/web/main.py`):
```
GET /api/chats/{chat_id}/media?type=photo&limit=50&offset=0
```
Returns: `{ media: [{id, message_id, type, file_path, file_name, width, height, duration, date}], total, has_more }`

### DB Adapter Changes

**New method** (`src/db/adapter.py`):
- `get_chat_media(chat_id, media_type=None, limit=50, offset=0)` - query media table joined with messages for date, filtered by type

### Frontend Changes

- Media gallery panel (replaces or overlays message view)
- Grid layout using CSS Grid with responsive columns
- Theme toggle: CSS variables + Tailwind class swap
- Light theme color palette definition
- Video player improvements in lightbox

## Related Code Files

### Modify
- `src/web/main.py` - add media gallery endpoint
- `src/db/adapter.py` - add `get_chat_media` method
- `src/web/templates/index.html` - gallery UI, theme system, video player

### Create
- None

## Implementation Steps

### Backend

1. **Add `get_chat_media` method** to `src/db/adapter.py`:
   ```python
   async def get_chat_media(self, chat_id: int, media_type: str | None = None,
                            limit: int = 50, offset: int = 0) -> dict:
       # Query Media joined with Message for date
       # Filter: Media.chat_id == chat_id, Media.file_path IS NOT NULL
       # Optional: Media.type == media_type
       # Order by Message.date DESC
       # Return {media: [...], total: count, has_more: bool}
   ```

2. **Add `/api/chats/{chat_id}/media` endpoint** to `src/web/main.py`:
   - Accept `type`, `limit`, `offset` query params
   - Respect `DISPLAY_CHAT_IDS`
   - Call `db.get_chat_media()`

### Frontend - Media Gallery

3. **Add gallery toggle button** in chat header:
   - Grid icon button next to existing header buttons
   - Toggle `showMediaGallery` ref

4. **Build media gallery panel**:
   - Overlays message view when active (same container, different content)
   - Type filter tabs: All | Photos | Videos | Documents | Audio
   - CSS Grid: `grid-template-columns: repeat(auto-fill, minmax(120px, 1fr))`
   - Each cell: square thumbnail with aspect-ratio cover
   - Videos show duration overlay + play icon
   - Documents show icon + filename
   - Click opens lightbox (reuse existing)

5. **Gallery infinite scroll**:
   - Reuse IntersectionObserver pattern from chat list
   - Load 50 items per page
   - Use `v-lazy-src` directive from Phase 3

<!-- Updated: Validation Session 1 - Removed light theme (steps 6-10). Dark-only per user decision. -->

### Frontend - Video Player

6. **Improve lightbox video experience**:
    - Show video duration from `msg.media.duration` as overlay before play
    - Add fullscreen button (use Fullscreen API)
    - Show loading spinner while video buffers
    - Keep existing click-to-play behavior

## Todo List

- [ ] Add `get_chat_media` method to adapter
- [ ] Add `/api/chats/{chat_id}/media` endpoint
- [ ] Add gallery toggle button in chat header
- [ ] Build media gallery grid panel
- [ ] Add type filter tabs (All/Photos/Videos/Docs/Audio)
- [ ] Implement gallery infinite scroll
- [ ] Improve lightbox video with duration overlay and fullscreen
- [ ] Test gallery with 500+ media items
- [ ] Test theme persistence across page reloads

## Success Criteria

- Media gallery shows grid of thumbnails, filterable by type
- Gallery scrolls infinitely loading 50 items at a time
- Clicking gallery item opens existing lightbox
- Video in lightbox shows duration and has fullscreen button

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Gallery query slow for chats with 10k+ media | Medium | Paginate with LIMIT/OFFSET; index on (chat_id, type) already exists |

## Security Considerations

- Media gallery endpoint must check `DISPLAY_CHAT_IDS`
- Theme preference stored in localStorage only (no server call)
- Fullscreen API requires user gesture (browser handles this)

## Next Steps

- Audio waveform visualization deferred (optional, high complexity for low value)
- Gallery could later support bulk download/export
- Theme system enables future high-contrast accessibility mode
