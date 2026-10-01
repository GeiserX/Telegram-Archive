# Roadmap

This page lists what Telegram Archive does not do yet, written as what you will get once it lands. Entries have no dates and no target versions, and none is promised for a particular release. Some entries are partly built; those say what works today. The last section lists roadmap items that shipped in recent releases, and the [changelog](https://github.com/GeiserX/Telegram-Archive/blob/main/docs/CHANGELOG.md) has everything else.

## Security

- **An encrypted session file.** The Telegram session file is encrypted on disk, so a copied volume does not hand over the account. Today the session is a plain file in the session directory, so the volume that holds it needs the same care as the account password.
- **Length limits on every search field.** The chat list search and the search inside a chat reject oversized input. Today only the search across all messages caps the query length.

## Viewer

- **A Links tab in shared media.** A fourth tab next to Photos & Videos, Voice and Files lists every URL shared in the chat.
- **Voice waveforms.** Voice messages show a real waveform you can scrub. Today the bubble shows a play button, the file name and the duration, and the [audio player](../viewer/using-the-viewer.md#audio-player) has a plain seek bar.
- **Single-key shortcuts.** Keys such as `j` and `k` move between messages and chats. Today <kbd>Esc</kbd>, the arrow keys and <kbd>Enter</kbd> work in search, the info panel, dialogs and the lightbox, as listed under [Keyboard](../viewer/using-the-viewer.md#keyboard).
- **More languages.** The interface is available in languages other than English. Today it is English only.
- **Offline reading.** Chats you already opened stay readable in the installed app without a connection. Today the app needs the viewer to be reachable.
- **Swipe gestures on phones.** For example, a swipe takes you back to the chat list. Today the [phone layout](../viewer/using-the-viewer.md#on-a-phone) uses a back button.
- **Withdrawn reactions.** A reaction someone took back stays under the message as a faint dashed chip marked "Removed". Today the archive keeps the removal, and the viewer shows only the reactions that remain.
- **Archive counts in Statistics.** Statistics also counts the messages deleted in Telegram, edited and transcribed, and each count opens What changed for that kind. Today those counts are per chat, in the chat's info panel.
- **Counts that open What changed.** The deleted and edited counts in a chat's info panel open What changed for that chat. Today they open the chat's own Deleted only and Edited only modes, and the panel's **What changed in this chat** row opens the feed for the chat.

## Notifications

- **Mute chosen chats.** You pick chats that send no push notifications. Today a subscription gets pushes for every chat its owner can see.
- **Clean up blocked browsers.** A browser that has blocked notifications stops getting pushes and its subscription is removed. Today the Notifications switch in the main menu says the browser blocked them and stays on while a push subscription exists, so you can turn it off. Otherwise a subscription is removed only when the push service rejects it or the user logs out. See [Subscription lifecycle](../viewer/live-updates.md#subscription-lifecycle).

## Search

- **Search filters.** Narrow message results by date range, sender, media type, or whether a message has a link. Today search takes words only. [Jump to a date](../viewer/using-the-viewer.md#jump-to-a-date), the [shared media](../viewer/using-the-viewer.md#shared-media) tabs and tapping a hashtag cover part of this.
- **Search by meaning.** Find messages that say the same thing in other words. Today search matches the start of words in message text and voice transcripts.
- **Transcript excerpts in results.** A hit found in a voice transcript shows the words it matched. Today the result reads "Recording · matched in its transcript", and opening it marks the words in the transcript.

## Archive health

- **Prometheus metrics.** A metrics endpoint reports backup health, message counts and media size for Prometheus and Grafana. Today the same figures come as JSON from the statistics routes, behind any viewer login, and from [Archive status](../operations/troubleshooting.md#archive-status), behind the master login.
- **Scheduled database upkeep.** SQLite `VACUUM` and PostgreSQL `ANALYZE` run on a schedule. Today nothing runs them.
- **Checksum verification of media.** Media verification compares file checksums. Today [`VERIFY_MEDIA`](../configuration/media.md#verify-files-on-disk) checks that each file exists, is not empty and is within 1% of its recorded size.
- **An alert when mass-operation protection trips.** You get notified when [mass-operation protection](../configuration/listener.md#mass-operation-protection) blocks a burst of deletions. Today it writes a log warning only.
- **Backup reports and a Telegram bot.** A scheduled report of each backup run, or a Telegram bot that tells you the archive status and starts a backup. Today status is something you open and read.

## Integrations and export

- **Webhooks for new messages.** The [event webhook](../configuration/event-webhook.md) can fire on new messages, not only on edits and deletions. Today new messages reach only Web Push and open viewer pages.
- **API tokens for scripts.** Scripts call the viewer's HTTP API with an API token, and the `/docs` page renders. Today a script logs in the way the browser does and sends the session cookie, as the [HTTP API](../reference/api.md) page shows, and the `/docs` and `/redoc` pages render blank.
- **HTML and PDF export.** Export a chat as a readable HTML page or a PDF. Today [export](../viewer/using-the-viewer.md#export-a-chat) writes JSON.
- **Import from other tools.** Import backups made by other Telegram backup tools. Today `telegram-archive import` reads Telegram Desktop exports, and two archives can be [merged](../operations/maintenance.md#merge-two-archives).
- **Object storage for media.** Media is stored in S3-compatible object storage. Today it is stored on the local file system.

## Sign-in

- **Built-in two-factor login.** Password accounts can add a second factor. Today two-factor login needs a reverse proxy that does it and passes the user through [`AUTH_PROXY_HEADER`](../viewer/access.md#identity-from-a-reverse-proxy).
- **A guide for OIDC single sign-on.** A documented setup for an OIDC provider in front of the viewer. Today it works through `AUTH_PROXY_HEADER` and `AUTH_PROXY_ADMIN_USERS`, with no step-by-step guide.

## Transcripts and translation

- **Translation on demand.** Translate a message through a translation server you run, the same way [voice transcription](../configuration/transcription.md) sends audio to a transcription server.

## Suggest a feature

Search the [open issues](https://github.com/GeiserX/Telegram-Archive/issues) first, then open a feature request if yours is not there. To build something yourself, read [Development](../development.md).

## Recently shipped

- **8.12.0:** A channel or group that several accounts hold is listed once, tagged with every account that holds it.
- **8.4.0:** Seven color themes, picked from the sidebar header. See [Themes and wallpaper](../viewer/themes.md).
- **8.3.0:** Full-text search on SQLite and PostgreSQL, matching the start of words.
- **8.3.0:** Every message has a link you can copy, and a pasted `t.me` link opens the archived copy.
- **8.2.0:** Both images have a Docker health check, and a stalled backup scheduler reports unhealthy.
- **8.0.0:** One archive holds more than one Telegram account. See [Multiple accounts](../configuration/multiple-accounts.md).
