# Telegram mobile

The viewer restyled to look like the Telegram iOS app, drawn on the real app with one override stylesheet (`theme.css`).

## The idea

The default becomes a light theme with Telegram's green pattern wallpaper, white incoming bubbles and light green outgoing bubbles. A dark variant follows the iOS night theme: black lists, dark grey incoming bubbles and blue outgoing bubbles with white text. Bubbles get the iOS shape: 17px corners, 6px corners where two bubbles of one sender meet, a curved tail on the last bubble of a run, and the sender's avatar beside that tail. The time sits inline at the end of the last line, so a one-line message is one line tall. The phone views were designed first (large title, 60px avatars, a slim chat bar with the chat photo on the right), and the desktop reuses the same parts with a centred reading column.

## Views

| View | Light (default) | Dark |
| --- | --- | --- |
| Chat list, desktop | `01-chat-list-desktop.png` | |
| Chat, desktop | `02-chat-desktop.png` | `dark/02-chat-desktop.png` |
| Replies and forward | `03-chat-replies.png` | `dark/03-chat-replies.png` |
| Chat, phone | `04-chat-mobile.png` | `dark/04-chat-mobile.png` |
| Chat list, phone | `05-chat-list-mobile.png` | `dark/05-chat-list-mobile.png` |
| Search | `06-search.png` | |
| Media gallery | `07-media-gallery.png` | |
| Voice note and transcript | `08-transcript.png` | `dark/08-transcript.png` |
| Theme picker | `09-theme-picker.png` | |

The light views use no theme parameter. The dark views use `--theme night`.

## Palette

| Role | Light | Dark | Source |
| --- | --- | --- | --- |
| Lists, panels | `#FFFFFF` | `#000000` | iOS day classic, iOS night |
| Bars (header, pinned) | `#F7F7F7` at 94% | `#161617` at 92% | iOS navigation bar |
| Chat background | Green gradient `#DBDDBB` `#6BA587` `#D5D88D` `#88B884` with a line pattern | Near black `#050807` with a faint green and blue glow and a line pattern | Telegram default wallpaper |
| Incoming bubble | `#FFFFFF` | `#262629` | iOS |
| Outgoing bubble | `#E1FFC7` | Gradient `#3274EC` to `#2662DC` | iOS, dark stops deepened for 4.3:1 white text |
| Text | `#000000` | `#FFFFFF` | iOS |
| Secondary text | `#737378` | `#8D8E93` | iOS `#8E8E93`, light one darkened to 4.7:1 |
| Accent (buttons, tabs, icons) | `#0088FF` | `#3E88F7` | iOS `defaultDayAccentColor`, iOS dark accent |
| Links and quotes, incoming | `#006EDC` | `#6EAEFF` | Darker than the accent for text contrast |
| Links and quotes, outgoing | `#1E8028` | `#FFFFFF` | iOS outgoing green, darkened to 4.6:1 |
| Time, incoming | black at 55% | white at 50% | iOS |
| Time, outgoing | `#008208` | white at 80% | iOS `#008C09` |
| Hairlines | black at 16% | white at 13% | iOS separator |
| Date and service pills | `#3C6032` at 65%, white text | black at 55%, white text | Telegram service pill on the wallpaper |
| Selected chat row | `#E2EEFD` | `#1A2C46` | Accent tint |
| Sender names (7) | `#D0453C` `#B35F0A` `#8A4FD6` `#2A8A26` `#16808F` `#2A7FCC` `#C4437F` | `#FF7B72` `#FFA95A` `#B98CFF` `#7DD87A` `#5DD2E0` `#6DBBF5` `#FF72B0` | Telegram's seven peer colours, darkened on light to at least 4.1:1 |
| Avatar fills (7) | Top to bottom gradients: `#FF885E`-`#FF516A`, `#FFCD6A`-`#FFA85C`, `#82B1FF`-`#665FFF`, `#A0DE7E`-`#54CB68`, `#53EDD6`-`#28C9B7`, `#72D5FD`-`#2A9EF1`, `#E0A2F3`-`#D669ED` | Same | Telegram avatar gradients, white initials |

Font: the system font (`-apple-system`, San Francisco on Apple devices), with Inter as the fallback elsewhere. Message text is 16px on a phone and 15px on a desktop.

## What changed and why

- **Bubble height.** Padding went from 12px on every side to 6px by 10px, and the time moved onto the last text line. A one-line message with its sender name is now 50px tall on desktop instead of 88px.
- **Grouping.** Bubbles of one sender join with 6px corners on the sender side. Only the last bubble of a run has the tail, and the avatar now sits beside it, so the avatar and the tail always mark the same bubble.
- **Tail.** A curved tail in the bubble's own colour replaces the 4px corner, the way iOS draws it.
- **Sender colours.** Each sender gets one of Telegram's seven peer colours, and a bright gradient avatar with white initials. In the demo data the five senders now show in five different colours.
- **Light theme surfaces.** White bubbles on the green wallpaper give a clear edge in the light theme. Reply quotes, forward headers, polls, file rows, reactions and code blocks take their colour from the side of the bubble (blue on incoming, green on outgoing) instead of a fixed black overlay, so they work on light and dark.
- **Forward header.** "Forwarded from" and the source are two plain lines in the accent colour, with no box.
- **Media.** Photos and albums run edge to edge with the bubble corners and a 2px album gap. A photo with no caption shows the time in a dark pill on the picture. Stickers stand on the wallpaper with no bubble.
- **Voice notes.** The play button, a waveform and the duration sit on the bubble. The card, its shadow and the music emoji are gone.
- **Chat list.** Rows are two lines: the name and time, then the type and the account chip. The raw id and the monospace username are gone. Avatars are 60px on a phone and 54px on a desktop, and a hairline under the text separates rows, as on iOS.
- **Sidebar header.** The tool icons share the top bar with the account actions, the title is a large title, the last backup line is its subtitle, and the folders are text tabs with an underline. On desktop the chat list starts at 178px instead of 250px.
- **Chat header.** One translucent bar with a hairline. Title and subtitle on the left, with the account chip on the subtitle line. The in-chat search is an icon that opens into a field on focus. On a phone the chat photo moves to the right and the title is always visible.
- **Wide screens.** Messages sit in a centred column of up to 760px.
- **Pills and buttons.** Date and service pills use the wallpaper's shade with white text. The scroll button is a plain white or dark circle.
- **Contrast.** Secondary text, times and names were checked against their backgrounds and darkened where the iOS values fall under about 4:1. Sender names reach at least 4.1:1 on white.

## What needs markup or JavaScript beyond CSS

The mockup reaches some of these with selectors that are fine for a picture but too brittle to ship.

1. **Themes.** Add the two palettes as new themes (for example `classic` and `classic-night`) in `KNOWN_THEMES` and `viewerThemes`, add a "System" choice that follows `prefers-color-scheme`, and make the light one the default. The mockup instead rewrites the Slate tokens and the Night tokens, and gives the other dark themes their dark neutral scale back.
2. **Peer colours.** `getSenderNameColor` and `getAvatarFill` should pick one of seven colour tokens by `sender_id % 7`, as the apps do. The mockup maps the 360 hash hues onto the seven colours with generated attribute selectors.
3. **Avatar at the end of a run.** Render the avatar on the row where `isRunEnd` is true, not where `showSenderName` is true. The mockup moves it with CSS anchor positioning, which only works in recent Chromium.
4. **Run classes.** Put `run-start`, `run-middle` and `run-end` classes on each row. The mockup works them out with `:has()` on neighbouring rows.
5. **Inline time.** Render the time as a floated element inside the text block, so its width is known. The mockup reserves a fixed 38px (96px when "edited" shows) at the end of the text.
6. **Chat list preview.** The list API needs the last message text and its sender, so a row can show them in place of the chat type and participant count, as Telegram does. "1 chats" also needs a singular form, and the folder counts should be a badge, not "(6)".
7. **Header.** Show "24 members" or "last seen" in place of the raw type, move the message, media and size counts into the info panel (the mockup hides them), make search a real icon button, and put export and the transcript toggle in the info page or a menu on phones (the mockup hides them under 768px).
8. **Sidebar header.** Move the history, theme and stats buttons into the top bar in the markup. The mockup pulls them up with a negative margin, which only works while the account bar is shown and the user name is short.
9. **Voice waveform.** Telegram stores a waveform with every voice note. The mockup draws the same fixed picture for every `.ogg` file.
10. **Quote colours.** Reply and forward headers should use the quoted sender's peer colour, which needs that sender's id in the reply data.
11. **Media and sticker flags.** Add classes for a bubble that is only media, and for a sticker, instead of `:has()` checks on the content.
12. **Wallpaper.** Ship the default pattern and gradient as a built-in wallpaper, and let `VIEWER_CHAT_BACKGROUND` replace it.
13. **Small leaks.** Update `<meta name="theme-color">` with the theme, theme the login page, and hide the floating date pill while the inline separator for the same day is under it.
