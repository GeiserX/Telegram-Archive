# Refined slate

## The idea

This option keeps the viewer's dark slate look and fixes the details that make it feel like an admin dashboard.
Bubbles get Telegram's proportions: 7 by 11 px padding, 16 px corners, 6 px corners where bubbles join, and the time tucked into the end of the last line.
A run of messages from one sender reads as one group, with the avatar, the name and a small tail together at the top of the run.
The chat list and the headers lose the noise (ids, monospace, a third line, emoji stats) and get shorter, so more chats and messages fit on screen.
The accent becomes Telegram blue (#3390EC), and the inner boxes that were black overlays now take their colour from the theme, which also fixes the Day palette.

Everything here is one CSS file, [theme.css](theme.css), loaded after the page's own styles. The default stays dark. The `day/` folder shows the same CSS on the Day palette, which this file repairs too.

## Palette (Slate)

| Token | Value | Hex | Used for |
|---|---|---|---|
| `--tg-bg` | 16 23 33 | #101721 | chat canvas |
| `--tg-sidebar` | 23 32 45 | #17202D | chat list, headers, day pills |
| `--tg-hover` | 32 43 58 | #202B3A | row hover, search fields |
| `--tg-active` | 41 78 120 | #294E78 | selected chat row |
| `--tg-other` | 30 40 55 | #1E2837 | incoming bubble |
| `--tg-own` | 43 82 122 | #2B527A | outgoing bubble (Telegram Night's blue) |
| `--tg-text` | 231 236 242 | #E7ECF2 | primary text |
| `--tg-muted` | 133 147 166 | #8593A6 | secondary text |
| `--tg-border` | 45 57 74 | #2D394A | borders |
| `--tg-accent` | 51 144 236 | #3390EC | accent (Telegram Web) |
| `--tg-accent-strong` | 40 121 209 | #2879D1 | filled buttons, white text on top |
| `--tg-accent-soft` | 106 179 243 | #6AB3F3 | links, reply bar, forward line |
| `--rs-meta-in` | 125 142 162 | #7D8EA2 | time on incoming bubbles |
| `--rs-meta-out` | 150 186 224 | #96BAE0 | time on outgoing bubbles |
| peer names | Telegram Night set | #FB6169 #FAA357 #B48BF2 #85DE85 #62D4E3 #65BDF3 #FF5694 | sender names, picked by id mod 7 |

The neutral scale (`--tg-n100` to `--tg-n950`) moves from Tailwind gray to a slate-tinted scale so borders and icons match the canvas. Bubbles are opaque in every palette.

Day gets a light slate canvas (#E2E8EF) so white incoming bubbles stand out, Telegram's day-blue outgoing bubble (#DEF1FD), the same accent, and a selected row filled with the accent and white text.

## What changed and why

| Area | Before | After | Why |
|---|---|---|---|
| Bubble size | 12 px padding, time on its own line, reactions on another; a one-line message was about 88 px tall | 7 by 11 px padding, 15 px text; the time floats at the end of the last line, or on the reactions row | About twice as many messages per screen, closer to Telegram |
| Grouping | avatar on the first bubble, tail on the last, 14 px corners everywhere | avatar, name and tail together at the top of the run; 6 px corners on the joined side; 2 px between bubbles, 8 px between senders | A run reads as one block, and the avatar sits next to the tail |
| Sender colours | a hash of the id gave neighbouring ids the same yellow and olive | Telegram's seven peer colours, bright avatar gradients with white initials | Senders are easy to tell apart (see the note below) |
| Reply | black box, 4 px corners | accent tint with a 3 px bar, 6 px corners | Reads on any palette, matches Telegram |
| Forward | green label in a black box | one accent line: "Forwarded from Harbor Town Weekly" | The green was unreadable on light themes |
| Poll, voice note | inner box, card with gradient and shadow | drawn straight on the bubble; the time sits on the poll footer | No boxes inside boxes |
| Media | photos inset 12 px with 8 px corners | albums and photos run to the bubble edge; top corners follow the bubble | The Telegram look, and no odd frame around pictures |
| Stickers | inside a bubble | no bubble, time as a small pill | Same as every Telegram client |
| Chat list rows | 92 px, three lines, monospace id, monospace blue username | 66 px, two lines; id hidden, username in plain text, account chip in the bottom-right corner | Less noise, 40% more chats on screen |
| Sidebar header | about 250 px before the first chat, title wraps | about 190 px, title on one line, 36 px pill search, no chart emoji | Chats start higher |
| Chat header | 90 px, three lines, emoji stats in a box, 256 px search | 56 px, two lines: title, then "group · 72 msgs · 13 media · <1 MiB"; 220 px pill search, 36 px icon buttons | Calmer, and the stats read as a subtitle |
| Phone header | the search box covered the avatar and the title | search collapses to an icon and opens over the header on focus; export leaves the phone header | The chat name is visible |
| Wide screens | bubbles spread across 1000 px | an 820 px reading column, centred | Shorter eye travel between replies |
| Day pills, service messages | 16 px pill with a heavy shadow | 24 px pill, no shadow, 12.5 px text | Quieter |
| Scrollbar | track painted in the sidebar colour | transparent track | No light strip along the pane |
| Code, quotes, spinner | black overlays | theme ink and accent tints | Readable on light palettes |

## What needs markup or JS changes

The mockup does all of this with CSS, but a few parts only work through tricks or demo-only rules. A real change should move them into the template.

1. **Peer colours.** `getSenderColor` and `getAvatarFill` hash the id into a hue. The proposal is `id mod 7` into Telegram's seven colours, with a light and a dark set, and a white-initials gradient for the avatar. The mockup maps the demo senders by name to preview this (section 9 of the CSS). Those rules must not ship.
2. **Avatar and tail at the top of the run.** Done with `:has()` sibling selectors on the run classes. It would be simpler to add `message-run-start` next to `message-run-break` in the template and style that. If the Telegram placement (avatar and tail at the bottom) is preferred, change the avatar's `v-if="showSenderName(index)"` to `isRunEnd(index)`; CSS cannot move the avatar to another row.
3. **Inline time.** Done with an invisible spacer after the text and an absolutely placed time. The spacer widths are fixed guesses for "15:02", "edited" and "deleted". A template change should put the time inside the text block as a floated span, so it measures itself.
4. **Header stats.** The emoji are clipped with a negative indent. Remove the emoji from the markup and render the stats as one subtitle string with the type ("group · 24 members · 72 msgs"). Show the member count, not the raw type.
5. **Chat list rows.** The id is hidden, not removed. Remove it from the markup. The second line should show the last message and its sender, as Telegram does, which needs the API to return it.
6. **Phone header.** Export is hidden on phones. It should move to the info panel or an overflow menu, not disappear. The collapsing search should become a real search button that opens a field.
7. **Voice notes.** The file name ("voice.ogg") still shows. Voice notes should show a waveform and the duration, and keep the name for music files only.
8. **Theme picker dots and browser colour.** The Slate dots in `viewerThemes` and `<meta name="theme-color">` still hold the old values. Both need the new palette, and the meta tag should follow the chosen theme.
9. **Chat avatar placeholders.** The fixed blue-to-purple gradient with `--tg-ink` initials should use the same peer colours as sender avatars.
10. **Tokens instead of overrides.** Bubble radius, padding and text size are fixed utilities today. Moving them to variables (as `--rs-radius`, `--rs-pad-x` and `--rs-pad-y` do here) would let every theme share them.

## Views

Standard views (Slate): `01` to `09` in this folder. Day palette: `day/02-chat-desktop.png`, `day/04-chat-mobile.png`, `day/08-transcript.png`. Baseline for comparison: `../00-current/`.
