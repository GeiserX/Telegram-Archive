# Phase 5: Per-Chat Background Preferences

## Overview
- **Priority:** P2
- **Status:** Complete
- **Effort:** 4h

Allow users (including viewers) to choose a custom background per chat via right-click context menu on the message area. Backgrounds are theme-defined presets (patterns/colors). Stored in localStorage per browser. Each of the 6 themes provides a set of background options.

## Architecture Decision: localStorage vs Server DB

**Chosen: localStorage**

| Factor | localStorage | Server DB |
|--------|-------------|-----------|
| Viewer tokens | Works (per-browser) | Complex (no persistent user ID for tokens) |
| Backend changes | None | New table, endpoints, migration |
| Persistence | Per-browser | Global per-user |
| Complexity | Low | High |
| Offline | Works | Doesn't |

Rationale: Share tokens are ephemeral (no persistent identity). Viewer accounts have usernames but adding a whole preferences table for backgrounds is over-engineering. localStorage is the pragmatic choice — each browser gets its own preferences.

## Background Presets Per Theme

Each theme defines 6-8 background options:

```javascript
const themeBackgrounds = {
    midnight: [
        { id: 'default', name: 'Default', css: 'none' },
        { id: 'dots', name: 'Dots', css: 'url("data:image/svg+xml,...")' },
        { id: 'grid', name: 'Grid', css: 'url("data:image/svg+xml,...")' },
        { id: 'waves', name: 'Waves', css: 'linear-gradient(...)' },
        { id: 'gradient-blue', name: 'Ocean', css: 'linear-gradient(135deg, #0d1117, #1a2332)' },
        { id: 'stars', name: 'Stars', css: 'url("data:image/svg+xml,...")' },
        { id: 'solid-darker', name: 'Darker', css: '#0e1621' },
        { id: 'solid-lighter', name: 'Lighter', css: '#1e2d3d' },
    ],
    dark: [ /* similar set with dark theme colors */ ],
    nord: [ /* nord palette gradients */ ],
    solarized: [ /* warm solarized tones */ ],
    oled: [ /* pure blacks + subtle patterns */ ],
    light: [ /* light mode backgrounds */ ],
}
```

## Storage Schema (localStorage)

```javascript
// Key: 'tg-chat-bg'
// Value: JSON object mapping chatId → backgroundId
{
    "-1001234567890": "dots",
    "-1001234567891": "gradient-blue",
    "_default": "default"  // fallback for all chats
}
```

- `_default` key = global default for all chats in this theme
- Per-chat overrides take precedence
- Theme change resets to theme's default (per-chat choices cleared only if bg doesn't exist in new theme)

## Related Code Files

| File | Action | Change |
|------|--------|--------|
| `src/web/templates/index.html` | Modify | Background presets data, context menu option, picker modal, CSS application |

## Implementation Steps

### Data Layer

1. **Define background presets** — add `themeBackgrounds` object with presets per theme
   - Each preset: `{ id, name, css, preview }` where `preview` is a small swatch color
   - SVG patterns inlined as data URIs (no external files)
   - Include: dots, grid, diagonal lines, gradient variations, solid colors

2. **localStorage helpers**
   ```javascript
   function getChatBg(chatId) {
       const prefs = JSON.parse(localStorage.getItem('tg-chat-bg') || '{}')
       return prefs[String(chatId)] || prefs['_default'] || 'default'
   }
   function setChatBg(chatId, bgId) {
       const prefs = JSON.parse(localStorage.getItem('tg-chat-bg') || '{}')
       prefs[String(chatId)] = bgId
       localStorage.setItem('tg-chat-bg', JSON.stringify(prefs))
   }
   function setDefaultBg(bgId) {
       const prefs = JSON.parse(localStorage.getItem('tg-chat-bg') || '{}')
       prefs['_default'] = bgId
       localStorage.setItem('tg-chat-bg', JSON.stringify(prefs))
   }
   ```

3. **Computed CSS for current chat**
   ```javascript
   const chatBackground = computed(() => {
       if (!selectedChat.value) return ''
       const bgId = getChatBg(selectedChat.value.id)
       const theme = currentTheme.value
       const presets = themeBackgrounds[theme] || themeBackgrounds['midnight']
       const preset = presets.find(p => p.id === bgId) || presets[0]
       return preset.css === 'none' ? '' : preset.css
   })
   ```

### Context Menu Integration

4. **Add "Choose Background" to message area context menu**
   - When right-clicking on the message area background (not on a message bubble):
     - Show "Choose Background..." option
   - When right-clicking on a message: existing options + "Choose Background..."
   - Available to ALL users (master, viewer, token) — not restricted

5. **Update `showCtxMenu()` to detect background clicks**
   ```javascript
   // If clicked on empty area (not a message), show background-only menu
   if (e.target.closest('.messages-scroll') && !e.target.closest('[data-msg-id]')) {
       ctxMenu.value = {
           visible: true, x, y,
           type: 'background',
           target: null
       }
   }
   ```

### Background Picker Modal

6. **Create background picker modal**
   ```html
   <!-- Background picker overlay -->
   <div v-if="showBgPicker" class="fixed inset-0 z-[9995] bg-black/40" @click="showBgPicker = false">
       <div class="fixed bottom-0 left-0 right-0 bg-tg-sidebar rounded-t-2xl p-4 max-h-[60vh] overflow-y-auto"
            @click.stop>
           <div class="text-center font-semibold text-tg-text mb-3">Choose Background</div>
           <div class="grid grid-cols-4 gap-3 mb-4">
               <div v-for="bg in currentThemeBackgrounds" :key="bg.id"
                    @click="selectBackground(bg.id)"
                    class="aspect-square rounded-xl cursor-pointer border-2 transition"
                    :class="activeBgId === bg.id ? 'border-tg-accent' : 'border-transparent'"
                    :style="{ background: bg.preview || bg.css }">
                   <div class="h-full flex items-end justify-center pb-1">
                       <span class="text-[10px] text-white drop-shadow">{{ bg.name }}</span>
                   </div>
               </div>
           </div>
           <div class="flex gap-2">
               <button @click="selectBackground(activeBgId, true)"
                   class="flex-1 py-2 rounded-lg bg-tg-accent text-white text-sm">
                   Set for all chats
               </button>
               <button @click="showBgPicker = false"
                   class="px-4 py-2 rounded-lg bg-tg-hover text-tg-text text-sm">
                   Cancel
               </button>
           </div>
       </div>
   </div>
   ```

7. **selectBackground function**
   ```javascript
   function selectBackground(bgId, setDefault = false) {
       if (setDefault) {
           setDefaultBg(bgId)
       } else if (selectedChat.value) {
           setChatBg(selectedChat.value.id, bgId)
       }
       showBgPicker.value = false
   }
   ```

### CSS Application

8. **Apply background to message area**
   - The `.msg-area-pattern` class currently has a hardcoded SVG dots pattern
   - Replace with dynamic inline style:
   ```html
   <div ref="messagesContainer" class="..."
        :style="chatBackground ? { backgroundImage: chatBackground } : {}">
   ```
   - Remove the static `.msg-area-pattern` background or make it the default fallback

9. **Watch for theme changes** — when theme changes, re-evaluate backgrounds
   ```javascript
   watch(currentTheme, () => {
       // Re-trigger chatBackground computed
       // If current bg doesn't exist in new theme, reset to default
   })
   ```

## Background Preset Designs

### Midnight Theme (example)
| ID | Name | Type | Description |
|----|------|------|-------------|
| `default` | Default | Pattern | Current dots pattern |
| `dots-lg` | Large Dots | Pattern | Bigger spaced dots |
| `grid` | Grid | Pattern | Subtle grid lines |
| `diagonal` | Diagonal | Pattern | Diagonal lines |
| `gradient-deep` | Deep Blue | Gradient | `#0d1117` → `#1a2332` |
| `gradient-purple` | Purple Night | Gradient | `#17212b` → `#2d1b4e` |
| `solid-dark` | Solid Dark | Solid | `#0e1621` |
| `plain` | Plain | None | Theme default bg color |

### Repeat for other themes with appropriate color palettes

## Todo List

- [ ] Define `themeBackgrounds` object with presets for all 6 themes
- [ ] Implement localStorage helpers (get/set per chat, set default)
- [ ] Add `chatBackground` computed property
- [ ] Add "Choose Background" to context menu (background area + message right-click)
- [ ] Create background picker modal (grid of swatches)
- [ ] Implement `selectBackground()` (per-chat + global default)
- [ ] Apply dynamic background to message container
- [ ] Remove hardcoded `.msg-area-pattern` SVG
- [ ] Watch theme changes for background compat
- [ ] Design 6-8 presets per theme (6 themes = ~40 presets total)
- [ ] Add Vue refs: `showBgPicker`, `activeBgId`

## Success Criteria

- Right-click on message area → "Choose Background" option appears
- Background picker shows theme-appropriate presets
- Selecting a background applies immediately to current chat
- "Set for all chats" applies to all chats as default
- Per-chat overrides persist across page reloads
- Works for all user types (master, viewer, token)
- Theme change preserves compatible background choices
