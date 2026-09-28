# Using the viewer

This page covers every screen and control of the web viewer, in the order you meet them. Signing in and sharing are on [Logins, viewer accounts and share links](access.md), colours on [Themes and wallpaper](themes.md), and notifications on [Live updates and notifications](live-updates.md).

## Layout

The viewer has three panes:

- The **sidebar** on the left holds the chat list and search.
- The **chat pane** in the middle shows the messages of the open chat.
- The **info panel** on the right opens with the chat information button in the chat header.

The viewer only reads the archive. It never contacts Telegram. The interface is in English only.

On a desktop you can resize the sidebar and the info panel. Drag the handle between two panes, or focus the handle and press <kbd>Left</kbd> or <kbd>Right</kbd> to move it 16 px at a time.

| Pane | Width |
|------|-------|
| Chat list | 300 to 960 px, a quarter of the window by default |
| Info panel | 260 to 640 px, 320 px by default |
| Messages | always at least 360 px |

Each browser remembers its own widths.

## Chat list

The top of the sidebar shows folder tabs. **All Chats** comes first, then one tab per Telegram folder, with the folder's emoji or a folder icon and its chat count. In All Chats, an **Archived Chats** row appears when you have archived chats in Telegram.

Chats load 50 at a time. Scroll down and the next 50 load. Each row shows `ID: <id>`, the chat's Telegram id.

When you can see more than one Telegram account, each row carries a chip with the account's label. See [Multiple accounts](../configuration/multiple-accounts.md).

The sidebar also shows **Last backup** with the time of the most recent backup.

## Search

One field at the top of the sidebar searches chats and messages together. Results appear 300 ms after you stop typing.

- **Chats** match on title, first name, last name or username.
- **Messages** use full-text search that matches the start of words. The newest come first, 20 at a time, and more load as you scroll. After 5,000 matches the list stops with "Showing the first 5,000 matches".

Use <kbd>Up</kbd> and <kbd>Down</kbd> to move through both sections, <kbd>Enter</kbd> to open a result, and <kbd>Esc</kbd> to clear the field. A second <kbd>Esc</kbd> leaves the field.

A hit that matched a voice transcript, not the message text, carries the label "Matched in the transcript".

If the archive has no full-text index yet, the message section says so. To build it, run the backup on version 8.5 or later. The backup builds the index when it starts.

You can paste a Telegram link into the field. A `t.me/c/<id>/<msg>` or `t.me/<username>/<msg>` link opens that message when the chat is in the archive. Otherwise the sidebar says "That link points at a chat this archive does not hold".

Inside an open chat, the search box in the chat header searches that chat only, with the same 300 ms delay.

![Global search for "trail" with message hits in the sidebar and the chat opened at the clicked hit](../images/screenshots/search-results.png)

## Reading messages

### Text

Messages keep their Telegram formatting: bold, italic, underline, strikethrough, code, preformatted blocks, quotes and spoilers. Click a spoiler, or focus it and press <kbd>Enter</kbd> or <kbd>Space</kbd>, to reveal it.

Only `http`, `https` and `tg` links are clickable. Mentions are highlighted. Web addresses that start with `http://` or `https://` become links even without formatting. Email addresses become links when Telegram marked them as such.

A `#hashtag` or `$CASHTAG` opens a tag view with three tabs: **This Chat**, **My Messages** and **All Chats**.

### Replies and forwards

A reply shows a quote of the message it answers. Click the quote to jump to the original.

A forward shows "Forwarded from" and the source. When the source chat is also in the archive, click the header to open the original message.

![Replies with quoted context, an edited message and a forward from a channel](../images/screenshots/chat-replies-forward.png)

### Albums

Photos and videos sent together render as one grid:

| Items | Grid |
|-------|------|
| 2 | two columns |
| 3 | two on top, one below |
| 4 | two by two |
| 5 or more | three columns |

The caption comes from whichever item carries it. Only the first message of an album renders. Reactions on the other messages of the album are not shown.

![A four-photo album with its caption under a day separator](../images/screenshots/chat-album.png)

### Media

- **Round videos** play in place as a circle, muted, while they are on screen. Click one to turn the sound on or off. They never open in the lightbox.
- **GIFs** loop while they are on screen and pause when you scroll away.
- **Stickers** in `.webp` show as images up to 200 px wide. Other stickers show the text "Animated Sticker".
- **Polls and quizzes** show each answer with a percentage bar and the total votes.
- **Dice, venues, invoices, stories, giveaways, live locations and games** show as a chip.
- **Link previews** show the archived card with site name, title, description and image.

When a file is not in the archive, the bubble says why. The four texts and the setting behind each one are listed under [Why media is missing in the viewer](../configuration/media.md#why-media-is-missing-in-the-viewer).

Voice messages and other audio can carry a transcript that opens under the player. See [Voice transcription](../configuration/transcription.md).

### Reactions, edits and deletions

Reactions show as chips with the emoji and, when above one, the count.

An edited message shows "edited", or "edited(N)" when it was edited N times. Click it to open the **Versions** drawer, which lists up to 100 earlier texts.

A deleted message stays in place, faded, with a "deleted" marker. This only happens with `DELETION_MODE=soft`, the default.

### Service messages

Joins, title changes and other chat events show as centred pills. Click the name of the person who did it to open their details.

When a group was converted to a supergroup, a banner says "This group continues as a supergroup" or "Migrated from <title>".

## Moving through time

### Pinned messages

A banner under the chat header shows the current pinned message. When more than one message is pinned, the banner also shows the position as "N of M". Click the text to scroll to that message and move the banner to the next pin. This only works for a pinned message already loaded in the pane. For older pins, click the pin button to open the **Pinned Messages** view.

### Jump to the newest message

A round button with a down arrow appears in the bottom right corner. It shows when you scroll more than 200 px above the newest message, or when new messages arrive that you have not seen. A badge counts the unseen messages, up to 99+. Click it to return to the newest message.

### Jump to a date

While you scroll, a pill at the top of the pane shows the day you are looking at. Click it, or any date separator, to open **Jump to Date**. Days that have messages carry a dot. The latest date you can pick is today in `VIEWER_TIMEZONE`. Press <kbd>Esc</kbd> to close the dialog.

### Links to a message

Every message has an address of the form `/?chat=<ref>&msg=<id>`. Opening it loads the chat around that message.

To get the link, open the info panel, select the message and click **Copy message link**. On a plain `http` address the browser does not allow copying, so the link appears in a notice instead.

The link names the chat by a random-looking 22-character reference, never by its Telegram id.

## Forum topics

Opening a forum shows its topics first. Each topic has its icon, a pinned or closed marker when it applies, and its message count. Topic 1 is **General**. When the archive holds no topics for the forum, a **View all messages** button opens the whole chat.

![A forum with its topics in the sidebar and one topic open](../images/screenshots/topics.png)

## Shared media

The **Shared Media Gallery** button in the chat header opens the chat's files in three tabs:

| Tab | Holds |
|-----|-------|
| Photos & Videos | photos, videos, GIFs and round videos, as a grid |
| Voice | voice messages and audio, with a filter box that matches the file name or the transcript |
| Files | documents, each with download and go-to-message buttons |

Items load 50 at a time. Click **Load more** for the next page.

Photos, videos, GIFs and image documents open in the lightbox. Press <kbd>Esc</kbd> to close it and <kbd>Left</kbd> or <kbd>Right</kbd> to move between items. The download button is hidden for no-download logins. Other files open in a new tab.

How thumbnails are made and cached is on [Media downloads](../configuration/media.md).

![Shared Media with the Photos & Videos tab open](../images/screenshots/media-gallery.png)

## Info panel

The info panel shows the open chat:

- avatar, name, and member or subscriber count
- earlier profile photos the archive recorded
- description or bio, username and Telegram ID
- account chips, when more than one account is visible
- shortcuts into the shared media

With the panel open, click a message to select it. The panel then also shows:

- sender
- sent time
- edit time
- the message it replies to
- forward source
- message id
- album id
- pinned or deleted state
- its files, with a download button

With the panel open, <kbd>Up</kbd> and <kbd>Down</kbd> move the selection to the next message, and <kbd>Esc</kbd> closes the panel.

The master login also sees each file's **Archive path** and a **Copy path** button. Other logins do not.

### Open and Show in folder

Two more buttons, **Open** and **Show in folder**, appear for the master login when the viewer host sets `MEDIA_OPEN_CMD` or `MEDIA_OPEN_PATH_CMD`. Use them only when the viewer runs directly on your own computer, outside Docker.

Each variable holds a shell command. The placeholders `%PATH%`, `%DIR%` and `%FILENAME%` are replaced with the shell-quoted file path, its folder and its name. **Open** only accepts images, videos, audio and PDF files. The viewer removes variables whose names look like secrets from the command's environment.

```bash
# macOS example for a native run
export MEDIA_OPEN_CMD='open %PATH%'
export MEDIA_OPEN_PATH_CMD='open -R %PATH%'
```

!!! warning "These buttons run a shell command on the viewer host"
    Anyone who can sign in as the master can trigger that command. The stock `docker-compose.yml` does not pass these variables to the viewer on purpose. Leave them unset on any viewer that other people can reach.

## Audio player

Playing a voice message or an audio file opens one player bar for the whole app. It has previous and next buttons, a seek bar and speeds of 0.5x, 1x, 1.5x and 2x. The speed buttons are hidden on screens narrower than 640 px.

Voice and music each keep their own speed. Playback continues when you switch chats. When one item ends, the player moves on to the next audio in the chat.

## What changed

The clock button in the sidebar header opens **What changed**. It lists deletions, edits and new voice transcripts, newest first. Pick a window of **Last 24 hours**, **7 days**, **30 days** or **All time**. Entries load 50 at a time.

Deletions appear here only with `DELETION_MODE=soft`.

## Export a chat

The **Export Chat to JSON** button in the chat header downloads the open chat as a JSON file. You can set a from date, a to date, both or neither. A to date on its own includes that whole day.

The file is named `<title>_export.json`. It holds the chat, the filters you used, the messages and their earlier versions. It holds no media files.

No-download logins cannot export.

## Statistics

The **Stats** dropdown in the sidebar header shows the number of chats, messages and media files, the storage used and when the numbers were calculated.

The numbers are cached. They are recalculated:

- by the viewer once a day at `STATS_CALCULATION_HOUR`, 3 by default, in `VIEWER_TIMEZONE`;
- by the viewer once at startup, if they were never calculated;
- by the backup after each run;
- on request by the master, through `POST /api/stats/refresh`.

Logins restricted to some chats see counts for their own chats only. The message, media and size figures in a chat's header are cached for 60 seconds.

`SHOW_STATS=false` hides the dropdown. The statistics API still answers.

!!! note "Setting the stats hour under Docker"
    The stock `docker-compose.yml` does not pass `STATS_CALCULATION_HOUR` to the viewer. Add it to the viewer's `environment:` block. See [Environment variables](../reference/environment-variables.md).

`VIEWER_TIMEZONE` defaults to `Europe/Madrid`. It sets every time the viewer displays. An unknown zone name falls back to UTC with a warning.

### Archive status

The master login has an **Archive Status** button, the heart icon next to the user name at the top of the sidebar. It opens a panel with:

| Row | Shows |
|-----|-------|
| Backup | running now, the time of the last run, or never ran |
| Listener (one per account) | active since when, or not running |
| Media pipeline | files downloaded, pending, given up and skipped by settings |
| Stats freshness | when the statistics were last calculated |
| Transcription | off, on with no server configured, server not detected yet, or the server's name and version |
| Database | SQLite or PostgreSQL, and its size |

## On a phone

Below 768 px wide, the layout changes:

- The sidebar hides once a chat is open, and the chat takes the full width.
- A back button in the chat header returns to the chat list.
- The info panel covers the whole screen.
- Header buttons get 44 px touch targets.
- The resize handles are hidden.

![The chat list on a phone](../images/screenshots/chat-list-mobile.png){ width="300" }

## Install as an app

Browsers that support it can install the viewer as an app. It then opens in its own window without the browser toolbar.

It does not work offline. The app needs the viewer to be reachable, as the web page does.

After you upgrade the viewer, reload any open tabs or app windows. An open page keeps using the old version until it reloads.

## Keyboard

| Key | Where | Does |
|-----|-------|------|
| <kbd>Esc</kbd> | search field | clears the field, then leaves it |
| <kbd>Esc</kbd> | info panel, lightbox, Versions drawer, Jump to Date, sender details | closes it |
| <kbd>Up</kbd> <kbd>Down</kbd> | search results | moves between chats and messages |
| <kbd>Up</kbd> <kbd>Down</kbd> | open info panel | selects the previous or next message |
| <kbd>Left</kbd> <kbd>Right</kbd> | lightbox | previous or next item |
| <kbd>Left</kbd> <kbd>Right</kbd> | focused resize handle | resizes the pane by 16 px |
| <kbd>Enter</kbd> | search results | opens the highlighted result |
| <kbd>Enter</kbd> <kbd>Space</kbd> | spoiler, round video | reveals the spoiler, toggles the sound |
| <kbd>Tab</kbd> | Jump to Date, sender details | stays inside the dialog |

There are no single-letter shortcuts such as `j` and `k`.
