# Phase 4: Polish & Grouping

## Context Links
- [Plan overview](plan.md)
- [Phase 1: Bubble Layout](phase-01-bubble-layout-and-tails.md) (prerequisite -- provides tail classes)
- [Telegram UI Patterns Research](research/researcher-01-telegram-ui-patterns.md)
- Target: `src/web/templates/index.html` lines 935-937 (template), 2995-3006 (showSenderName JS)

## Overview
- **Priority:** P2
- **Status:** complete
- **Description:** Group consecutive messages from the same sender: only show bubble tail on the last message in a sequence, reduce gap between grouped messages, and add smooth scroll `contain` refinement.

## Key Insights

### Telegram's Grouping Behavior
- Consecutive messages from same sender within ~1 minute: grouped visually
- Only the LAST message in a group shows the bubble tail
- Middle messages in group: uniform 12px border-radius (no asymmetric corner)
- Reduced vertical gap between grouped messages (~2px vs ~8px between different senders)
- Sender name only shown on FIRST message in group (already implemented via `showSenderName()`)

### Current State
- `showSenderName(index)` already groups by sender -- shows name only on first message in sequence (line 2995-3006)
- But no visual grouping: all messages have same gap and would all get tails after Phase 1
- Need a companion function: `isLastInSenderGroup(index)` -- only this message gets the tail

### flex-col-reverse Indexing
- `sortedMessages` is sorted newest-first: index 0 = newest (bottom), higher = older (top)
- `showSenderName(index)` checks `index + 1` (older message above) for different sender
- `isLastInSenderGroup(index)` must check `index - 1` (newer message below) for different sender
  - If index = 0 (newest): always last in group (bottom of chat)
  - If `sortedMessages[index - 1].sender_id !== msg.sender_id`: last in group
  - Else: not last, hide tail

## Requirements

### Functional
1. Only last message in consecutive sender group shows bubble tail
2. Reduced gap (~2px) between grouped messages, normal gap (~6px) between different senders
3. Non-last grouped messages: uniform 12px border-radius (no asymmetric corner)

### Non-functional
- Must work correctly with `flex-col-reverse` ordering
- Must not break service messages or date separators (they reset grouping)
- Must work with album messages (already hidden by `isHiddenAlbumMessage`)

## Architecture

### JS Changes

**Add `isLastInSenderGroup(index)` function** near `showSenderName` (after line 3006):
```javascript
const isLastInSenderGroup = (index) => {
    // With flex-col-reverse: index 0 = newest (bottom)
    // "Last in group" means the message closest to bottom in visual display
    const currMsg = sortedMessages.value[index]

    // Newest message (index 0): always last in its group (at the bottom)
    if (index === 0) return true

    const newerMsg = sortedMessages.value[index - 1]

    // If newer message is hidden album msg, check the one before
    // (album messages don't break sender groups)

    // Different sender below = this is the last in its group
    return newerMsg.sender_id !== currMsg.sender_id
}
```

**Add to return block** (around line 3517):
```javascript
isLastInSenderGroup,
```

### CSS Changes

**Add grouped message styles:**
```css
/* Grouped messages: tighter spacing */
.msg-row.msg-grouped {
    margin-top: -2px; /* tighter gap within group */
}

/* Hide tail on non-last grouped messages */
.bubble-outgoing.no-tail {
    border-bottom-right-radius: 12px; /* restore uniform radius */
}
.bubble-outgoing.no-tail::after {
    display: none;
}
.bubble-incoming.no-tail {
    border-bottom-left-radius: 12px; /* restore uniform radius */
}
.bubble-incoming.no-tail::before {
    display: none;
}
```

### Template Changes

**Update message row (line 935) -- add grouping class:**

Current (after Phase 1+3):
```html
<div v-else-if="!isHiddenAlbumMessage(msg, index)" class="msg-row flex"
    :class="isOwnMessage(msg) ? 'justify-end' : 'justify-start'">
```

New:
```html
<div v-else-if="!isHiddenAlbumMessage(msg, index)" class="msg-row flex"
    :class="[
        isOwnMessage(msg) ? 'justify-end' : 'justify-start',
        !showSenderName(index) ? 'msg-grouped' : ''
    ]">
```

Logic: `showSenderName(index)` returns false when the message above is from the same sender -- meaning this message is NOT the first in its group, so it's "grouped" (tighter spacing).

**Update bubble class (line 936) -- conditional tail:**

Current (after Phase 1):
```html
<div class="message-bubble p-3 text-sm shadow-sm text-gray-100 group"
    :class="isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming'"
    :style="getSenderStyle(msg)">
```

New:
```html
<div class="message-bubble p-3 text-sm shadow-sm text-gray-100 group"
    :class="[
        isOwnMessage(msg) ? 'bubble-outgoing' : 'bubble-incoming',
        !isLastInSenderGroup(index) ? 'no-tail' : ''
    ]"
    :style="getSenderStyle(msg)">
```

## Related Code Files
- `src/web/templates/index.html`
  - CSS: new classes after Phase 1's bubble tail CSS
  - Template: lines 935-937 (message row + bubble)
  - JS: after line 3006 (new function), ~line 3517 (return block)

## Implementation Steps

1. **Add `isLastInSenderGroup(index)` JS function** (after `showSenderName`, line 3006)
   - Check if `sortedMessages[index - 1]` (newer/below) has different `sender_id`
   - index 0 always returns true

2. **Add `.no-tail` CSS override classes** (after `.bubble-incoming` CSS from Phase 1)
   - Override asymmetric radius back to 12px
   - Hide `::after` / `::before` pseudo-elements with `display: none`

3. **Add `.msg-grouped` CSS class**
   - `margin-top: -2px` for tighter vertical spacing within group

4. **Update message row template** (line 935)
   - Add `msg-grouped` class when `!showSenderName(index)` (not first in sender group)

5. **Update bubble template** (line 936)
   - Add `no-tail` class when `!isLastInSenderGroup(index)` (not last in sender group)

6. **Register `isLastInSenderGroup` in return block** (~line 3517)

7. **Test grouping logic with edge cases:**
   - Two messages from same sender back-to-back
   - Three messages, middle one is from different sender
   - Album messages (hidden ones shouldn't affect grouping)
   - Service messages between regular messages (should reset grouping)
   - Date separator between messages from same sender (should reset grouping)

## Todo List
- [ ] Add `isLastInSenderGroup(index)` function
- [ ] Add `.no-tail` CSS overrides for both outgoing and incoming
- [ ] Add `.msg-grouped` CSS class with tighter spacing
- [ ] Update message row template with `msg-grouped` class
- [ ] Update bubble template with `no-tail` class
- [ ] Register new function in return block
- [ ] Test: two consecutive messages from same sender
- [ ] Test: messages from alternating senders
- [ ] Test: album grouping doesn't break
- [ ] Test: service message resets grouping
- [ ] Test: date separator resets grouping
- [ ] Test: single-message sender (tail shown, normal gap)

## Success Criteria
- Consecutive messages from same sender show tail only on bottom-most (newest) message
- Grouped messages have noticeably tighter spacing than messages from different senders
- Sender name still appears only on first (oldest/top) message in group
- Service messages and date separators visually break groups
- No regression in existing message rendering

## Risk Assessment
| Risk | Impact | Mitigation |
|------|--------|------------|
| `isLastInSenderGroup` not accounting for hidden album msgs | Medium | Album hidden messages are filtered by `isHiddenAlbumMessage` before rendering -- they don't appear in DOM, so visual grouping is unaffected by them in the rendered sequence. However, the index-based check still operates on `sortedMessages` which includes hidden ones. May need to check if `newerMsg` is hidden and skip. |
| Service messages don't have `sender_id` | Low | They're rendered in a different `v-if` branch (line 928) -- not in the regular message path. `showSenderName` already handles this correctly. |
| Negative margin `margin-top: -2px` on `.msg-grouped` with `flex-col-reverse` | Medium | In reverse flex, "margin-top" on an element affects the space to the visually BELOW element. Test: may need `margin-bottom: -2px` instead. Verify in browser. |

**Important note on flex-col-reverse margin:**
- In `flex-col-reverse`, elements are laid out bottom-to-top in DOM order
- `margin-top` on a row affects the space between it and the row visually ABOVE (which is actually the NEXT sibling in DOM)
- This may be counterintuitive. During implementation, test both `margin-top` and `margin-bottom` to find which produces tighter VISUAL spacing within groups

## Security Considerations
- New JS function only reads existing message data (sender_id)
- No new API calls or data mutations

## Next Steps
- After Phase 4, conduct full visual regression test across:
  - Private chat (outgoing/incoming)
  - Group chat (multiple senders)
  - Channel (single sender, all outgoing)
  - Albums with mixed photo/video
  - Long chat history (500+ messages) for performance
- Consider future enhancements: blur-up image placeholders, swipe gestures
