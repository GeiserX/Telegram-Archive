# Telegram Desktop look

## The idea

Make the viewer look like Telegram Desktop, the client most people who archive their chats already use every day.
The default becomes Desktop's "Day classic" theme: a white chat list, a green pattern-colour wallpaper, white incoming bubbles and light green outgoing ones.
The dark option is Desktop's "Night" theme, with the exact values from its theme file.
Bubbles, grouping, the chat list and the header follow Desktop's shapes and sizes, so a run of messages reads as one group and a chat list row is two lines.
Everything here is a CSS override on the real app; the few things CSS cannot do are listed at the end.

Files: `theme.css` (the override), the light views in this folder, and the Night views of 02, 03 and 04 in `dark/`.
Compare with `../00-current/`.

## Font

Desktop bundles Open Sans and has an option to use the system font. The viewer does not ship Open Sans, so this design moves from Inter to the system font stack, with Open Sans first for machines that have it:
`"Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`.
The renders here use San Francisco. Message text is 14px, which is Desktop's 13px at its 110% scale step; 13px reads small in a browser.

## Palette

| Role | Day classic (default) | Night |
| --- | --- | --- |
| Chat list, header, panels | `#FFFFFF` | `#17212B` |
| Chat area | wallpaper gradient `#DBDDBB` `#6BA587` `#D5D88D` `#88B884` | `#0E1621` (flat) |
| Row hover | `#F1F1F1` | `#202B36` |
| Selected chat row (white text) | `#419FD9` | `#2B5278` |
| Search field | `#F1F1F1` | `#242F3D` |
| Incoming bubble | `#FFFFFF` | `#182533` |
| Outgoing bubble | `#EFFDDE` | `#2B5278` |
| Text | `#000000` | `#F5F5F5` (outgoing `#E4ECF2`) |
| Secondary text | `#999999` | `#708499` |
| Accent (buttons, fills) | `#40A7E3` | `#5288C1` |
| Links, reply and forward in incoming | `#168ACD` | `#71BAFA` |
| Reply and forward in outgoing | `#3A8E26` | `#83CAFF` |
| Time, incoming | `#A0ACB6` | `#6D7F8F` |
| Time, outgoing | `#6DB566` | `#7DA8D3` |
| Date and service pills | `#517C41` at 50%, white text | `#213040` at 84%, `#F5F5F5` text |
| Dividers | black at 9% | `#04080E` at 60% |
| Sender names (7 peer colours) | `#C03D33` `#CE671B` `#8544D6` `#4FAD2D` `#2996AD` `#168ACD` `#CD4073` | `#FB6169` `#FAA357` `#B48BF2` `#85DE85` `#62D4E3` `#65BDF3` `#FF5694` |
| Initials avatars | Telegram's 7 userpic gradients (red, orange, violet, green, cyan, blue, pink), white initials | same |

The other palettes stay. AMOLED, Forest and Aubergine keep their colours and get the new shapes. Day and Paper keep their colours and a flat canvas. Slate stops being the default.

## What changed and why

- **Bubbles.** 16px corners, 6px on the side where a bubble joins the one above or below, and a drawn tail on the last bubble of a run. Before, only the bottom corner changed and the middle of a run looked like separate cards.
- **Time inside the last line.** The time and "edited" float at the end of the last text line, as in Desktop. A one-line message with its sender name is now 52px tall instead of 88px. With reactions, the time sits on the reactions row.
- **Padding.** 6px by 11px instead of 12px all round. Bubbles are capped at 480px on wide screens, so replies stay close together.
- **Sender colours.** Seven fixed colours for names and avatars, like every Telegram client. Neighbouring ids no longer share one hue, so the demo senders are orange, pink, violet and red instead of all yellow.
- **Avatars.** 33px, bright gradient, white initials. In a run of several bubbles the avatar lines up with the name at the top.
- **Reply quote.** A tinted block with a 3px bar in the bubble's accent: blue in incoming bubbles, green in outgoing ones. The black overlay is gone, so it reads on every palette.
- **Forward header.** Two plain lines ("Forwarded from", then the name in bold), no box, no fixed green.
- **Media.** Photos and albums run edge to edge in the bubble, 2px gaps, no inner rounding. A photo with no caption fills the bubble and carries the time on a dark pill.
- **Voice notes.** The inner card is gone. The play button sits on the bubble, blue in incoming and green in outgoing, and the music emoji is hidden.
- **Poll, reactions, link preview, code.** Drawn on the bubble with tints of the accent, not black overlays.
- **Chat list.** 62px rows, 46px avatars, name and date on line one, one muted line below. The numeric id is hidden and the username is plain text. Account chips move to the right end of line two, where Desktop shows the unread badge. The selected row is solid blue with white text.
- **Sidebar header.** The title fits on one line, the search field is Desktop's grey pill, and folders are text tabs with an underline. The list now starts at 180px instead of 250px.
- **Chat header.** One 54px bar with the title and a status line. The emoji stats strip is gone. Search collapses to an icon and opens on focus, which also fixes the phone header, where the search box used to cover the chat title. The avatar shows on phones only, as in Desktop.
- **Pinned bar.** A thin accent line instead of the pin glyph.
- **Date and service pills.** Desktop's semi-transparent pill with white bold text.
- **Empty chat pane.** The wallpaper with one "Select a chat" pill.
- **Shared media.** A plain panel with underline tabs and square tiles.
- **Theme picker.** "Telegram Day" at the top and a "Match system" entry (see below).
- **Scrollbar.** A thin thumb on a transparent track, so there is no light strip along the chat pane.

## What needs markup or JS changes

- **Themes as real options.** Add `telegram-day` (the new default) and a `system` choice that follows `prefers-color-scheme` between Telegram Day and Telegram Night. Both need entries in `KNOWN_THEMES` and `viewerThemes`, and the boot script needs a `matchMedia` listener. In this mockup the Day palette sits on the bare `:root`, so Slate disappears, and the "Match system" row in the picker is a drawn label with no action.
- **Peer colours.** Replace the hue hash in `getSenderColor` with Telegram's `id mod 7` index into the seven colours, and use the matching userpic gradient in `getAvatarFill`. The mockup emulates this with 360 attribute selectors on the inline hue; that is a stand-in, not a way to ship.
- **Avatar at the bottom of the run.** Desktop pins the avatar to the last bubble, next to the tail. The markup draws it on the first bubble (`showSenderName(index)`). Switch the avatar to `isRunEnd(index)`.
- **Chat list preview.** Desktop's second line is the last message ("Kofi: Which trailhead did you park at?"). The chat list API would need the last message and its sender. Until then the line shows the type and member count.
- **Header status line.** Show "24 members" or "private chat" instead of the raw type. Move the per-chat stats into the info panel.
- **Grouping window.** Desktop starts a new run after 15 minutes even for the same sender. Runs here span any gap, even across days.
- **Date pill.** Hide the floating pill while an inline separator for the same day is in view, so the date does not show twice.
- **"edited(1)".** Desktop shows "edited 15:08". The count could move into the tooltip.
- **Stickers.** Desktop draws stickers with no bubble. The markup does not mark a sticker apart from a photo.
- **Voice waveform.** Desktop draws the waveform. The archive would need to keep the waveform bytes from the voice attributes.
- **Wallpaper pattern.** Desktop draws a doodle pattern over the gradient. That needs a static SVG file, which `VIEWER_CHAT_BACKGROUND` can already serve; this mockup uses the gradient alone.
- **Browser colour and login.** `<meta name="theme-color">` should follow the theme (`#FFFFFF` or `#17212B`), and the login page should use the theme colours instead of its fixed blue gradient.
- **Selectors.** Much of `theme.css` has to reach elements through Tailwind class chains, because only the message parts have stable class names. Adding classes such as `chat-row`, `chat-header`, `reply-quote`, `forward-header` and `message-meta` would make any theme like this one much shorter and safer.
