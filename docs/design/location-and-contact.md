# Locations and contacts: keep them and show them

> **Decision.** Every writer stores a plain location under `raw_data["geo"]` and a shared contact under `raw_data["contact"]`, beside the `venue`, `geo_live` and `poll` payloads that exist today. The listener starts keeping polls too. A message upsert stops dropping these payload keys when a later read lacks them. The viewer draws them as static cards, with no map tiles and no request to anyone until the reader clicks. A location opens on OpenStreetMap. A contact shows its name and phone, with call and copy. A new operator command, `telegram-archive backfill-payloads`, re-reads the old messages from Telegram and adds only the missing key. It is a dry run unless given `--apply`. The same command clears the leftover `.bin` paths on these rows, and never touches the disk.

## What is missing today

A plain location (`MessageMediaGeo`) and a shared contact (`MessageMediaContact`) keep nothing. `classify_media_type` names them `geo` and `contact`, the media row is metadata-only, and `raw_data` stays `"{}"`. Every location and contact row archived before this change is in that state.

Three more gaps sit next to it:

- The listener never stores a poll. `raw_data["poll"]` is built inline in the sweep's `_process_message`, with a helper only the sweep has (`_text_with_entities_to_string`).
- The upsert in `db/adapter.py` replaces `raw_data` whole when the incoming one is not empty. Only `entities` and `rich_message` survive. So `import --merge` of a forwarded message (importer `raw_data={"forward_from_name": ...}`) drops an archived poll, venue or live location.
- Rows written by releases up to v7.28.0 carry a `file_path` ending in `.bin` for geo, contact and poll. For geo and poll the file was never written. For a contact, Telethon wrote a vCard into it, so some of these files may still hold a name and phone.

Venue and live location are already captured by both live writers (`extract_extended_media_details`).

## Data shapes

All keys come from the Telethon objects. Fields Telegram does not send are absent, never guessed. Nothing here is ever logged.

```
raw_data["geo"]      = {lat, long, accuracy_radius}
raw_data["contact"]  = {first_name, last_name, phone_number, vcard, user_id}
raw_data["venue"]    = {title, address, provider, lat, long}            today
                     + {venue_id, venue_type, accuracy_radius}          added
raw_data["geo_live"] = {lat, long, period}                              today
                     + {heading, accuracy_radius, at, earlier}          added
raw_data["poll"]     = unchanged shape, now also written by the listener
```

- `GeoPointEmpty` has no coordinates. Then `lat` and `long` are absent and the card says "Location unavailable".
- `user_id` is `0` when the phone has no Telegram account. It is stored as given.
- `access_hash` is never stored.
- `geo_live.at` is the time of the read that saw this position: the message's `edit_date`, else its `date`, as ISO text. `geo_live.earlier` is a list of positions an earlier read saw, each `{lat, long, heading, accuracy_radius, at}`, oldest first. See "Live location" below.

## Writer changes

One builder in `message_utils.py`: `extract_media_payload(media, seen_at=None) -> (key, payload) | None`. `seen_at` is the message's edit date, else its date (`message_seen_at`), and becomes a live location's `at`. It handles `geo`, `contact` and `poll` by class name (so a bare `MagicMock` stays inert, like `_EXTENDED_MEDIA_TYPES`), and hands every other kind to `extract_extended_media_details`. The poll code and `_text_with_entities_to_string` move into `message_utils.py` unchanged. The venue and live location branches gain the new fields.

| Writer | Change |
| --- | --- |
| Backup sweep, `_process_message` | Calls `extract_media_payload` where it calls `extract_extended_media_details` now. The inline poll block goes. Polls still skip `_process_media`, as today. Gap fill and `backfill-topics` use the same method. |
| Listener, `on_new_message` | Same call. Polls, locations and contacts reach the WebSocket frame through `raw_data`. |
| Listener, edit of an unknown message | Falls back to `on_new_message`, so it is covered. |
| Listener, `on_message_edited` | No change. Live location updates are not followed (see below). |
| Sync, `_sync_deletions_and_edits` | No change. It writes text, `edit_hide` and reactions only. |
| Telegram Desktop import, JSON | Maps the export's location, venue, live location, contact and poll fields to the same keys. The field names are the ones Telegram Desktop's `export_output_json.cpp` writes: `location_information`, `live_location_period_seconds`, `place_name`, `address`, `contact_information` and `poll`. The export has no poll option bytes, so an imported answer's `option` is its position. |
| Telegram Desktop import, HTML | No change. The HTML has no structured fields. |
| `merge`, CLI `export` | No change. Both copy `raw_data` as it is. The viewer's export, which carries no `raw_data`, gains `media_payload` with these keys. |

### The upsert keeps payload keys

`MEDIA_PAYLOAD_KEYS` in `message_utils.py` lists the payload keys, one per metadata-only media type: `poll`, `geo`, `contact`, `venue`, `geo_live`, `dice`, `invoice`, `story`, `giveaway`, `giveaway_results`, `game`, `unsupported`. In every branch of the upsert (`insert_message` and `insert_messages_batch`), a payload key the archive holds and the incoming `raw_data` lacks is kept. When both hold the key, a read from Telegram (the backup or the listener) wins, so a poll's later tally replaces the earlier one, as today. An import never replaces a key the archive holds: an export carries less than Telegram served (no poll option bytes, no vCard, no venue provider or accuracy), so it only fills a key the archive lacks. This closes the `import --merge` trap.

### Live location

A live location is a message Telegram edits every few seconds while the share runs. The archive does not follow those edits. Following them would write a row per update, up to about a thousand for an eight-hour share, and the viewer draws no track.

What it keeps is every position its own reads saw. That is the listener's first capture, then each sweep or backfill read. When a read from Telegram meets a stored `geo_live`, one helper, `merge_geo_live(stored, incoming)`, decides (an import keeps the stored one, as for every payload key):

- The payload with the newer `at` becomes the top level. The other one goes into `earlier`, unless the same position is already there.
- An older read never takes the top level.
- A read with no coordinates (a stopped share can come back as `GeoPointEmpty`) never replaces coordinates. It can only update `period`.

So `earlier` grows only by reads the archive makes anyway, a handful per message, and no position the archive saw is lost.

## Viewer cards

The cards key on the `raw_data` key first and on `media.type` second. The listener writes no media row for these kinds, so a card keyed on the type would miss live captures. A row with the type and no payload yet (an old row before the backfill) gets the same card in a "details not archived" state, with no link.

Every card sits inside the normal bubble on the existing `--tg-quote-bg-in` and `--tg-quote-bg-out` tokens. The text uses the colours the extended chip uses today, which hold 4.5:1 in every theme. The pin is an inline SVG. There are no images, no tiles and no CSP change.

```
Location                         Venue
+--------------------------+     +--------------------------+
| [pin]  Location          |     | [pin]  Demo Cafe         |
|        40.416775,        |     |        1 Example Street, |
|        -3.703790   [copy]|     |        Demo City   [copy]|
+--------------------------+     +--------------------------+

Live location                    Contact
+--------------------------+     +--------------------------+
| [pin] Live location      |     | (AB)  Alex Demo          |
| [+av] Last position,     |     |       +34 600 000 000    |
|  updated today at 14:05  |     |              [call][copy]|
+--------------------------+     +--------------------------+
```

`[pin]` is the inline SVG pin. In the live card it holds the sender's avatar (`[+av]`). `(AB)` is the initials circle.

- **Location.** "Location", then the coordinates to 6 decimals, then "± N m" when `accuracy_radius` is there. The whole card is one link, `https://www.openstreetmap.org/?mlat=LAT&mlon=LON#map=16/LAT/LON`, with `target="_blank" rel="noopener noreferrer"`. The URL is built from numbers checked to be finite and in range, never from stored text. A "Copy coordinates" button uses `copyText`.
- **Why OpenStreetMap.** The official apps open Google Maps. OpenStreetMap needs no account, its page carries no ads or ad trackers, and it runs on open data under the OSM Foundation's privacy policy. Zoom 16 matches Telegram Desktop and Telegram Web. The viewer sends nothing until the reader clicks, so the only party that sees a coordinate is the one the reader chose.
- **Venue.** The same card, with the title on one line (semibold, cut with an ellipsis) and the address under it in up to two lines. The link uses the coordinates. Provider and venue id are not shown, since using them would mean a request to Foursquare or Google. This replaces the venue chip.
- **Live location.** The same card, with the sender's avatar inside the pin, as all four official apps draw it. Title "Live location". Subtitle "Last position, updated <time>" from `geo_live.at`, in Telegram Desktop's absolute forms ("today at 14:05", "yesterday at 09:10", "3 Mar 2025 at 18:00"), computed once per render with no timer. While `date + period` is still ahead, it adds "Sharing until <time>", or "until turned off" for period `2147483647`. It never says the share is live now. This replaces the live location chip.
- **Contact.** An initials circle in the peer colour (`getInitials` and `avatar-initials`). The name in semibold, falling back to the phone and then to "Contact". The phone as stored, with a "+" in front when it is all digits. A `tel:` link whose href keeps only "+" and digits, and a copy button with the toast "Phone copied". No phone: "Unknown number", with no call and no copy.
- **Words.** `MEDIA_TYPE_WORDS`, `replyMediaLabels` and the deleted-kind labels gain "Location" (geo and venue), "Live location" and "Contact". A venue in a reply reads "Location, <title>", from a `reply_to_media_title` the reply query takes from the target's `raw_data`, which also names the kind of a target with no media row. A reply to a location stops saying "geo".
- **No file branch.** `_attach_message_payload_urls` in `web/main.py` gives no `media.url` to a metadata-only type, and `mediaPlaceholder` never shows "File / Not downloaded yet" for one. So an old `.bin` row shows its card, not a broken download link, even before the cleanup runs.

Left out on purpose: "Add contact", "Message", directions, the countdown ring, ticking timers, the heading arrow and the accuracy circle, provider lookups, a vCard details box, and a link from a contact to its chat. `heading`, `accuracy_radius` and `vcard` are stored so any of these can come later.

A login without media access still sees coordinates and phone numbers. They are message content, like the text.

## Backfill

`telegram-archive backfill-payloads [--apply] [--chat-id N]`. It is an operator command, because the work is a one-off: once old rows are filled, new captures carry the payload, and a permanent background pass would be machinery for nothing. It follows `reclassify-round-videos`: one run per account, connect and tear down, a summary dict, counts only. One difference: it is a dry run unless given `--apply`, where `merge` and `reclassify-round-videos` write unless given `--dry-run`. Writing only when asked is the safer default for a command that changes old rows.

1. **Select.** A new adapter query lists messages that have a media row of type `geo`, `geo_live`, `venue`, `contact` or `poll` and whose `raw_data` lacks that type's key, plus rows of those types that still carry a `file_path`. It returns ids grouped by chat, ordered by message id.
2. **Read.** Per chat, `get_entity` through `call_with_flood_retry`. A chat Telegram refuses (private, admin required, unknown peer) is counted and skipped. Any other error counts as an error, and the chat's rows stay on the work list. Then `get_messages(chat, ids=batch)` in batches of 100 through `call_with_flood_retry`, which sleeps out a FloodWait and backs off on transient errors, with a one-second pause between batches. A batch that still fails counts as an error and its rows stay. A FloodWait longer than `MAX_FLOOD_WAIT_SECONDS`, or past `MAX_FLOOD_RETRIES`, stops the run for the account, since every further request would be refused and could lengthen the wait. Results are matched by `message.id`, never by position.
3. **Fill.** `extract_media_payload` builds the payload. A new adapter method, `add_missing_raw_data_keys(chat_id, message_id, payload)`, takes the row lock, adds only keys the row lacks, and returns whether it added anything. It is modelled on `_fill_missing_formatting`. It never goes through `insert_message`, so text, dates, reactions and every existing key stay as they are.
4. **Not served.** A `None` result, or a message whose media is now another kind, is counted as "not served".
5. **Report.** `[DRY RUN]` prefix when not applying. Counts per kind: filled, already present, not served, chats unavailable, leftover paths cleared, leftover paths kept.

The rows still missing a key are the work list, so an interrupted run resumes by running again, and a second run fills nothing new. There is no stored cursor. Rows Telegram no longer serves are asked for again on each run. That costs one call per 100 ids.

Not covered: a location or contact in a `SKIP_MEDIA` chat left no media row and no payload, so nothing in the archive points at it. Telegram's search filters (`InputMessagesFilterGeo`, `InputMessagesFilterContacts`) could find those later, if production shows enough of them.

## Leftover `.bin` paths

This is part of the backfill, not a migration. A migration runs on every install at startup and would drop the only pointer to a contact vCard that may be the last copy of a contact Telegram no longer serves.

For each row of a metadata-only type with a `file_path`, under `--apply`, except a row Telegram did not answer in this run because of an error (it stays on the work list untouched), and only when the media folder exists and holds files where the command runs (otherwise every path would read as missing, so every path is kept):

- The payload key is present (it was already there, or this run filled it): clear the path.
- The key is missing, the row is a contact, and the path names a regular, non-empty file: read it as a vCard, take the name, phone and full text into `raw_data["contact"]`, then clear the path. The path is cleared only once the row holds a `contact` key. The contents are never logged.
- The key is missing and the file is missing, empty or a dangling link: clear the path, since it points at nothing. Missing means "no such file" in a folder that exists inside the media folder. A permission error, a missing folder or a path outside the media folder keeps the path.
- Otherwise (a non-empty file that does not parse): keep the path and count it as kept.

Clearing sets `file_path`, `file_name` and `download_date` to NULL and `downloaded` to 0. The row stays. The disk is left alone: dangling links stay, and any vCard files stay in `_shared`. The shared-media-integrity work treats metadata-only rows as having no file, so it will not flag them. An operator who wants those files gone can delete them by hand.

## What it deletes, overwrites or forgets

- **Deletes.** Nothing. No row and no file.
- **Overwrites.** A poll tally is replaced by a newer read's tally from Telegram, as today. An import replaces no payload. A live location's top-level position is replaced by a newer read's, and the old one moves into `earlier`. The backfill overwrites nothing. The cleanup nulls a `file_path` only when the payload is kept or the path points at nothing.
- **Forgets.** The positions of a live share between two archive reads, which the archive never saw. A path to a file that was never written. A vCard file's link to its row, but only after its content is in `raw_data["contact"]`.

## Tests to add

Demo coordinates and demo names only, in tests, docs and screenshots.

- `extract_media_payload`: geo with and without `accuracy_radius`, `GeoPointEmpty`, contact with every field and with empty names and phone, poll with the same shape as today (the existing poll tests stay green), the new venue and live location fields, `MagicMock` inert.
- Both writers: sweep and listener store identical `geo`, `contact` and `poll` payloads for the same message. The listener's WebSocket frame carries them.
- Upsert, on SQLite and on the real PostgreSQL engine: an archived poll, geo or contact survives an `import --merge` read that lacks it. `merge_geo_live` keeps the newer read on top, moves the older one into `earlier`, ignores an older read, and keeps coordinates against a read with none.
- `add_missing_raw_data_keys`: adds a missing key, never replaces a present one, returns False the second time.
- `backfill-payloads`: a dry run writes nothing. `--apply` fills. A second run fills zero. `None` and changed-kind results count as not served. A refused chat counts as unavailable. A FloodWait is slept out, and one too long to sleep out stops the run with no further request. Results returned out of order still land on the right ids. Cleanup clears only the cases above, recovers a demo vCard, keeps an unparseable file's path, and every file is still on disk afterwards. Multi-account runs as in `reclassify-round-videos`.
- No PII in logs: extend `tests/test_no_account_pii_in_logs.py` with a demo phone and demo coordinates through both writers and the backfill.
- Viewer, with the JS harness in `tests/test_frontend_bootstrap.py`: each card from its `raw_data` key, the fallback from `media.type` alone, the OpenStreetMap URL from numbers, bad coordinates giving "Location unavailable" and no link, the contact fallbacks, the `tel:` href with only "+" and digits, the live states (ahead, ended, until turned off), the reply and pinned words, and no file branch for a metadata-only row with a `file_path`. `web/main.py` gives such a row no `media.url`.
- Contrast: the new text colours at 4.5:1 on both quote tokens in every theme, with the helper in `tests/test_sender_avatars.py`.
- Demo seed: `scripts/generate_dummy_db.py` gains a location, a venue, a live location and a contact, so `docs/design/rig/shoot.mjs` can take each card in Telegram Day and Night, desktop and phone.

The build also adds `backfill-payloads` to `docs/reference/cli.md` and a line under "## [Unreleased]" in `docs/CHANGELOG.md`.
