# Modern minimal

## The idea

The viewer is an archive reader, so this look puts the words first and the frame second. It uses a light default with white panels, a pale grey chat canvas and two bubble tones: white for incoming and a pale blue for outgoing. A true dark option uses neutral greys instead of navy, with one blue for links and selection. Shadows are gone, borders are hairlines, and the text is bigger with more line height, so long chats and search results are easier to scan. The chat column is capped at 760px and centred, and the time sits at the end of the last line, so a one-line message takes one line.

All images here are the real viewer with `theme.css` injected after its own styles. No markup or script was changed. Light images are in this folder and the dark ones are in `dark/`.

## Palette

The light palette replaces the built-in default (Slate, which has no `data-theme`). The dark palette takes the `night` slot in these renders. A system-driven default would need the change listed at the end.

| Token | Role | Light | Dark |
| --- | --- | --- | --- |
| `--tg-bg` | chat canvas | `#F3F4F6` | `#111315` |
| `--tg-sidebar` | panels, header | `#FFFFFF` | `#181A1D` |
| `--tg-hover` | row hover | `#F3F4F6` | `#202327` |
| `--tg-active` | selected row | `#E7EEFC` | `#1F2C42` |
| `--tg-text` | body text | `#1B1F24` | `#E6E8EB` |
| `--tg-muted` | secondary text, times | `#68707C` | `#8E959F` |
| `--tg-other` | incoming bubble | `#FFFFFF` | `#202327` |
| `--tg-own` | outgoing bubble | `#E1EBFC` | `#213452` |
| `--tg-border` | hairlines | `#E4E7EB` | `#2A2E33` |
| `--tg-accent` | accent | `#2F6AD6` | `#639BEE` |
| `--tg-accent-soft` | links, reply label | `#2A60C4` | `#80B0F5` |
| `--mm-field` | search fields (new) | `#F1F2F4` | `#202327` |
| `--mm-bubble-edge` | bubble hairline (new) | black at 7% | none |

Measured contrast: body text on the outgoing bubble is 13.8:1 (light) and 12.9:1 on the dark incoming bubble. Secondary text is 5.0:1 on white and 5.8:1 on the dark panel. Links are 5.9:1 (light) and 7.8:1 (dark). The light bubble fills are only 1.1:1 against the canvas, so each light bubble also gets a 1px hairline. That hairline is what keeps the edge visible. The dark fills stand apart on their own (1.2:1 and 1.5:1), so they have no edge.

The bubble fills are opaque (`--tg-bubble-alpha-*` set to 1). The helper tokens (`--mm-*`) also have values for the other built-in palettes, so Day, Paper, Forest, AMOLED and Aubergine still render correctly with the new layout.

## What changed and why

- **Bubbles.** The padding goes from 12px on all sides to 7px by 12px, the text from 14px to 15px (16px on a phone) at 1.5 line height, and the shadow is removed. A one-line message drops from about 88px to about 54px tall.
- **Time and reactions.** The time floats at the end of the last text line, and wraps to its own line only when the line is full. When a message has reactions, the time shares their row. This change removes one or two rows from almost every bubble.
- **Grouping.** A run of messages from one sender reads as one block. Joined corners are 6px and outer corners are 16px. The avatar moves to the top of the run, beside the name, where the markup already draws it. The first incoming bubble points at the avatar with a 4px corner. Every bubble in a run shares the same edge, and no bubble has a tail.
- **Sender names and avatars.** Names use one dark ink colour at weight 600 instead of the hashed hue, which in the demo made every sender the same yellow. Initials avatars use one quiet grey. The sender's name tells people apart, and colour no longer has to.
- **Inner blocks.** Replies are a light accent tint with a 2px accent rule. The forward header is a grey rule with a small "Forwarded from" label above a bold source. Polls, reaction chips, code, quotes, the spinner and service pills use theme tints instead of fixed black overlays or fixed green. They now read on both light and dark.
- **Voice notes.** The raised card inside the bubble is gone. The play button, file row and transcript sit directly on the bubble.
- **Reading column.** The message list has a 760px maximum width, centred in the pane, so the eye does not cross 1000px between replies on a wide screen.
- **Chat list.** Rows drop from 92px to 62px with 46px avatars. The `ID:` line is hidden, the username is no longer monospace or blue, and the account chip moves to the right of the second line. The selected row is a rounded tint inset from the edge.
- **Sidebar header.** The title fits on one line at 17px, and the emoji is gone from the Stats button. Search is a flat filled field, and the folder tabs are quiet text tabs with a tinted active tab. The first chat starts at 196px instead of 250px.
- **Chat header.** The header is one 56px bar. The type and account chips share the second line, the shadow is gone, and the per-chat stats are grey and smaller.
- **Pinned bar.** The pin icon is replaced by a thin accent rule, and the banner has the same flat surface as the header.
- **Phone header.** The chat search collapses to an icon and opens to 55% of the width on focus. The export button is hidden on phones. The chat title now shows in full, where before the search box covered it.
- **Day pills and scroll button.** These are smaller and flat, with a hairline instead of a drop shadow.
- **Scrollbar.** The track is transparent, so the light strip along the chat pane is gone.

## What needs markup or script changes

- **A real default and a system option.** Add `light` and `dark` entries to `KNOWN_THEMES` and `viewerThemes`, make `light` the fallback in the boot script, and add a `system` entry that follows `prefers-color-scheme`. The picker swatches come from `viewerThemes.dots` in script, so they still show the old Slate dots.
- **Sender colour.** The name hue and avatar fill are inline styles from `getSenderColor`. This design overrides them with `!important`. A cleaner route is a small class per sender (for example `sender-c0` to `sender-c6`, chosen by id mod 7) so a theme can decide whether names are ink or a muted palette.
- **Avatar at the end of a run.** To follow the Telegram layout, `showSenderName(index)` would need to be split from the avatar condition, and the avatar would use `isRunEnd(index)`. This design keeps the avatar at the top instead, which CSS can do on its own.
- **Chat list preview.** Rows should show the last message and its sender. The API does not return a preview today, so the second line still shows type and member count. The "1 chats" plural also needs a fix in the template.
- **Emoji in the chrome.** The per-chat stats and the voice row start with emoji that are text in the template, and the voice row shows the file name. CSS can only grey them out. They should become words or icons, plus a waveform for voice notes.
- **Export on a phone.** The CSS hides the export button on phones. The button should move into the info panel or an overflow menu, so it stays reachable.
- **Browser colour.** `<meta name="theme-color">` is fixed at navy. The script should update it from `--tg-sidebar` when the theme changes.
- **Floating day pill.** It still covers the top bubble and can double up with the inline separator for the same day. The observer should hide the inline separator that the floating pill is showing.
- **Row selectors.** Several rules target Tailwind utility classes (for example `.cursor-pointer.p-3` for chat rows and `div.justify-end.text-right` for the time row). Named classes such as `chat-row`, `message-meta` and `message-reactions` in the template would make a theme like this safe to maintain.
