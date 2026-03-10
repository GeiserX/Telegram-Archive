# Phase 2: Custom Right-Click Context Menu

**Priority:** High | **Effort:** Medium | **Backend:** None

## Context

- No custom context menu exists (confirmed: no `@contextmenu` handlers)
- Browser default right-click is useless in a chat viewer context
- Telegram Web has context menus on messages and chat list items

## Requirements

### Message Context Menu (right-click on message bubble)
- **Copy Text** — `navigator.clipboard.writeText(msg.text)`
- **Copy Link** — if message contains a URL, copy first URL
- **View Sender Info** — opens user info panel (Phase 3)
- **Search in Chat** — pre-fills message search with selected text
- **Jump to Date** — scrolls to date picker with message date

### Chat List Context Menu (right-click on chat item)
- **Open in Telegram** — link to `https://t.me/{username}` or `https://t.me/c/{chat_id}`
- **Copy Chat Link** — copy Telegram link
- **Search in Chat** — switch to chat and open search

### Behavior
- Opens at cursor position, clamped to viewport edges
- Closes on: click outside, Escape key, scroll, window resize
- Styled with theme CSS variables
- Mobile: long-press triggers same menu (optional, can defer)

## Architecture

Use `<Teleport to="body">` to render menu at root level, avoiding overflow/clipping from parent containers.

## Related Code Files

- `src/web/templates/index.html` lines 1140-1160 (message bubble template)
- `src/web/templates/index.html` lines 805-846 (chat list item template)

## Implementation Steps

### Vue State (~15 lines)
```javascript
const contextMenu = ref({
  visible: false,
  x: 0, y: 0,
  type: null, // 'message' | 'chat'
  target: null // msg or chat object
})
```

### Event Handlers (~30 lines)
1. `showMessageContextMenu(event, msg)` — set position + target, prevent default
2. `showChatContextMenu(event, chat)` — set position + target, prevent default
3. `closeContextMenu()` — hide menu
4. `handleContextAction(action)` — switch on action type, execute

### Position Clamping (~10 lines)
```javascript
function clampMenuPosition(x, y, menuWidth = 200, menuHeight = 250) {
  const maxX = window.innerWidth - menuWidth - 10
  const maxY = window.innerHeight - menuHeight - 10
  return { x: Math.min(x, maxX), y: Math.min(y, maxY) }
}
```

### Template (~40 lines)
```html
<Teleport to="body">
  <div v-if="contextMenu.visible"
       class="context-menu-overlay" @click="closeContextMenu">
    <div class="context-menu"
         :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
         @click.stop>
      <!-- Message menu items -->
      <template v-if="contextMenu.type === 'message'">
        <div class="context-menu-item" @click="handleContextAction('copy')">
          <i class="fas fa-copy"></i> Copy Text
        </div>
        <!-- ... more items -->
      </template>
    </div>
  </div>
</Teleport>
```

### CSS (~30 lines)
```css
.context-menu {
  position: fixed;
  background: var(--tg-sidebar);
  border: 1px solid var(--tg-border);
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  min-width: 180px;
  padding: 4px 0;
  z-index: 9999;
}
.context-menu-item {
  padding: 8px 16px;
  cursor: pointer;
  color: var(--tg-text);
  transition: background 0.15s;
}
.context-menu-item:hover {
  background: var(--tg-accent);
  color: white;
}
```

### Template Bindings
- Add `@contextmenu.prevent="showMessageContextMenu($event, msg)"` on `.message-bubble`
- Add `@contextmenu.prevent="showChatContextMenu($event, chat)"` on chat list items
- Add `@click="closeContextMenu"` and `@keydown.escape="closeContextMenu"` on document

## Success Criteria

- [ ] Right-click on message shows custom menu with correct items
- [ ] Right-click on chat list item shows chat menu
- [ ] Menu closes on click outside, Escape, scroll
- [ ] Copy Text works (clipboard API)
- [ ] Menu respects all 6 themes
- [ ] Menu position clamped to viewport
- [ ] Browser default context menu fully suppressed in app areas

## Risk

- Viewport edge clamping: test with messages near bottom/right edges
- Mobile long-press: defer to future phase if complex
