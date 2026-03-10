# Phase 5: Visual Polish & Transitions

**Priority:** Medium | **Effort:** Medium | **Backend:** Minor (last message preview)

## Context

- Current font: Inter (Google Fonts). Telegram Web uses Roboto.
- No background patterns in message area
- Limited transitions between views
- Chat list items lack visual refinement (no separators, no last message preview)

## Sub-features

### 5A: Font Swap (Inter → Roboto)

**Files:** `index.html` line ~18 (Google Fonts link), line ~82 (font-family)

Steps:
1. Change Google Fonts URL: `Inter:wght@300;400;500;600` → `Roboto:wght@300;400;500;700`
2. Update CSS: `font-family: 'Inter', sans-serif` → `font-family: 'Roboto', sans-serif`

### 5B: Message Area Background Pattern

Add subtle dot pattern using inline SVG data URI. Each theme controls opacity.

```css
.message-area {
  background-image: url("data:image/svg+xml,%3Csvg width='20' height='20' xmlns='http://www.w3.org/2000/svg'%3E%3Ccircle cx='2' cy='2' r='1' fill='%23888' opacity='0.08'/%3E%3C/svg%3E");
  background-repeat: repeat;
}
```

Add per-theme opacity override via `--tg-pattern-opacity` variable (lower for light themes, higher for dark).

### 5C: Smooth Transitions

1. **Chat switch**: Fade transition on message area when switching chats
   ```css
   .message-area { transition: opacity 0.15s ease; }
   ```

2. **Sidebar collapse/expand**: Already has some transition; ensure `transition: width 0.2s ease`

3. **Tab switching** (settings tabs, login tabs): Add `transition: opacity 0.15s ease` on tab content panels

4. **Hover micro-interactions**: Add `transform: scale(1.02)` on hover for:
   - Chat list items
   - Settings buttons
   - Avatar images

5. **Message bubble entrance**: CSS class `msg-enter` with:
   ```css
   @keyframes msgFadeIn {
     from { opacity: 0; transform: translateY(4px); }
     to { opacity: 1; transform: translateY(0); }
   }
   ```
   Only apply to newly loaded messages (real-time WebSocket), NOT to initial batch load.

### 5D: Chat List Item Polish

1. **Subtle separators**: `border-bottom: 1px solid` with low opacity
   ```css
   .chat-item { border-bottom: 1px solid color-mix(in srgb, var(--tg-border) 30%, transparent); }
   ```

2. **Active chat indicator**: Left accent bar on selected chat
   ```css
   .chat-item.active { border-left: 3px solid var(--tg-accent); }
   ```

3. **Last message preview** (requires backend):
   - Show truncated last message text below chat name
   - Media messages show `[Photo]`, `[Video]`, `[Document]`, etc.
   - Truncate to 1 line with ellipsis

4. **Avatar size**: Increase from `w-12 h-12` (48px) to `w-13 h-13` (52px) for better hierarchy

### 5D Backend: Last Message Preview

In `adapter.py` `get_all_chats()`, extend the subquery to also return `last_message_text`:

```python
# Add to the existing last_message_date subquery
last_msg_subq = (
    select(
        Message.chat_id,
        func.max(Message.date).label("last_message_date"),
        # Get text of latest message via window function
    )
    .group_by(Message.chat_id)
    .subquery()
)
```

Alternative (simpler): Run a second lightweight query to get last message text per chat after the main query. Or use a lateral join / correlated subquery.

**Simplest approach**: In the existing chat list response, add a field. Since `get_all_chats` already has `last_message_date`, add a correlated subquery for the text of that message. Performance note: this adds one subquery per chat but is bounded by `limit` (default 1000).

## Related Code Files

- `src/web/templates/index.html` — CSS + template changes
- `src/db/adapter.py` — `get_all_chats()` method (last message preview)
- `src/web/main.py` — pass `last_message_text` in `/api/chats` response

## Implementation Steps

1. Swap font (2 lines)
2. Add background pattern CSS (5 lines)
3. Add `--tg-pattern-opacity` to each theme definition (6 lines)
4. Add transition CSS rules (15 lines)
5. Add hover micro-interaction CSS (10 lines)
6. Add chat list separator + active indicator CSS (8 lines)
7. Increase avatar size in template (1 line change)
8. (Backend) Extend `get_all_chats` to return `last_message_text`
9. (Frontend) Render last message preview in chat list item template
10. Add `msg-enter` animation class, apply only to WebSocket-pushed messages

## Success Criteria

- [ ] Roboto font loads and renders correctly
- [ ] Subtle dot pattern visible in message area, themed per color scheme
- [ ] Smooth fade when switching chats
- [ ] Hover effects on interactive elements
- [ ] Chat list items have separators and active indicator
- [ ] Last message preview shown (truncated, 1 line)
- [ ] Media messages show `[Photo]`, `[Video]`, etc. in preview
- [ ] All changes work across 6 themes
- [ ] No performance regression (no animation on bulk message load)

## Risk

- **Font metrics**: Roboto vs Inter have different metrics. Some elements may need minor padding tweaks. Test all views after swap.
- **Last message subquery performance**: Test with 1000+ chats. If slow, make it optional via query param.
- **Background pattern on light themes**: Might be too visible. Tune opacity per theme.
