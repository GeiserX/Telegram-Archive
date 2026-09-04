# Changelog

All notable changes to this project are documented here.

For upgrade instructions, see [Upgrading](#upgrading) at the bottom.

## [8.8.0] - 2026-09-04

An optional wallpaper behind the messages.

### Added

- **`VIEWER_CHAT_BACKGROUND` puts a picture behind the conversation**, the way the apps let you set a chat background. Name a file the viewer serves from `/static` and mount it into the container; leave it unset and the viewer looks exactly as it does today. While it is on, the message bubbles turn opaque, because the themes draw them translucent over a flat colour and a photo behind running text is not readable, and the image is tinted with the palette's own background colour, so one picture works under a light and a dark theme instead of only one of them. Contributed by [@WalterLederer](https://github.com/WalterLederer) in [#432](https://github.com/GeiserX/Telegram-Archive/pull/432).

### Note

The file name is validated as a bare name, so it cannot address anything outside the static directory or add declarations to the stylesheet it is baked into. A name that does not resolve simply leaves the pane plain.

## [8.7.0] - 2026-09-04

Search results are emphasised inside the message, not only in the sidebar.

### Added

- **Matches are marked inside the message.** Opening a search result already jumped to the message and flashed it; the words that matched are now emphasised in the message itself, so a hit in a long message no longer has to be hunted for. The in-chat filter does the same for every row it shows, on each page the scroll loads. The marks end when their search does, and they survive the three-second refresh and a newly loaded page. Contributed by [@WalterLederer](https://github.com/WalterLederer) in [#431](https://github.com/GeiserX/Telegram-Archive/pull/431).

### Fixed

- **Search emphasis follows the rule the search itself used.** The sidebar preview marked whole words that start with any word of the query, which is what the archive matches on, but nothing else did. Matching now runs through one rule everywhere, so a two-word query emphasises both words wherever they appear, a prefix emphasises the whole word it found, and a query is no longer emphasised inside an unrelated word that was never a match.
- **Accented words are emphasised.** On SQLite, the archive folds accents when it searches: a search for `mas` finds a message reading "más". The preview and the message showed that hit with nothing emphasised at all. Both fold accents now, which also fixes the sidebar preview shipped in 8.6.0.
- **A word split by formatting is emphasised.** A word written half in bold, or inside a link, is one word to the archive and several pieces to the browser, so it was matched by the search and then emphasised nowhere.

### Note

No database change, and no change to what the search finds — only to what is emphasised once it has found it. The viewer half takes effect as soon as you pull the viewer image.

## [8.6.0] - 2026-09-03

Search finds messages across the whole archive, from the one field the sidebar already had.

### Added

- **Global message search.** The sidebar's search field now looks for message text across every chat you can see, not only chat names. Results come in two sections, Chats then Messages, the way the official clients show them: each message hit carries the chat's avatar and name, the sender, the matched words emphasised in a snippet windowed on the first match, the topic for forum hits, and the list loads more as it is scrolled. Arrow keys move through the hits, Enter opens one and Esc clears the field. Opening a hit lands on the message inside its chat and topic, with the results kept in the sidebar. Contributed by [@WalterLederer](https://github.com/WalterLederer) in [#429](https://github.com/GeiserX/Telegram-Archive/pull/429).
- **`GET /api/search/messages` and `GET /api/chats/{ref}`.** The search route answers word-prefix matches newest first, scoped in SQL by the same entitlements as the chat list, and pages by offset up to 5,000 hits. The chat route resolves one chat by its opaque ref, which is how deep links now reach chats beyond the sidebar's first page.

### Fixed

- **Search stays fast on any archive.** On PostgreSQL the planner cannot see how selective a word-prefix term is, so it walked the whole date index for rare or absent words: 2 to 9 seconds on a 2.9M-message archive. The search now counts hits through the index first, capped, and takes the path that is bounded for that answer, so common, rare and absent words all answer in under 100 ms there. SQLite reads the sorted hit set directly.
- **A deep link with no message id opens the chat, and a failed jump no longer leaves an empty pane.** The jump-to-message path skips the chat's normal first page because the window around the message replaces it; that skip now happens only when there is a message to jump to, and when the window cannot be loaded the chat falls back to its newest page. The playbar's jump to the playing track had the same race as notification links and gets the same fix.
- **Deep links reach every chat.** A notification or shared link into a chat outside the sidebar's first thousand said "Could not open that chat". Chats resolve by ref now.
- **Light themes: text on solid accent buttons and tabs is white in all thirteen places, not two.** Only the active folder tab had been fixed for Day and Paper; the other controls kept dark text on the blue.

### Note

No database change. The viewer half takes effect as soon as you pull the viewer image; the backup image carries no behaviour change.

## [8.5.0] - 2026-08-30

Round videos are circles, and media brought in from a Telegram Desktop export works like every other file.

### Added

- **Round videos are recognised and shown as circles.** Telegram's circular video messages were archived as ordinary videos, because neither the scheduled backup nor the live listener ever looked at the flag that marks one. They are now typed `video_note`, and the viewer plays them in place as a circle that autoplays while it is on screen, the way every official client does. Click one to toggle its sound. Archives built from a Telegram Desktop export already held round videos under this type and showed them as a grey file row; those are fixed by the same change, with no re-import needed.
- **`reclassify-round-videos` corrects archives captured before this release.** Round videos already in an archive are still typed as ordinary videos, and roundness is not recoverable from the stored file. The command asks Telegram which messages are round (a server-side filtered search, so a chat with none costs a single request) and corrects those rows in place. Nothing is downloaded, re-keyed or deleted. Run it with the viewer idle: a tab left open holds the old media URLs and will say "Media not found" for a re-typed video until it is reloaded.
- **One media classifier instead of two.** The backup and the listener each carried their own copy of the rules that decide what a piece of media is, which is how round videos ended up unimplemented in both at once. There is now one, and a test fails if it is ever forked again.

### Fixed

- **A media row is identified by its message, not by a string that spells its type.** `Media.id` was minted on every capture from the chat, message and type, so it cached a judgement and was then used as the row's identity. When the judgement changed the backup stopped talking about the row it already had: it inserted a second row, and left the first marked not-downloaded with its retry counter untouched, so that row was re-requested from Telegram on every cycle and could never reach the attempt cap. The id is now an opaque token a row keeps for life, and only the type is corrected.
- **A corrupted imported file is no longer deleted without a replacement.** With `VERIFY_MEDIA=true`, verification moved a suspect file aside, asked the backup to fetch a replacement, and got back a record claiming success that had never looked at the disk. It then deleted the file it had moved aside. For media imported from a Telegram Desktop export, which Telegram cannot re-serve, that was the only copy. Reuse is now decided by the file actually being there.
- **Imported media plays again in the viewer.** A file ingested from a Telegram Desktop HTML/JSON export was on disk, marked downloaded, and still showed as "Media not found" in the message list, in the gallery and on the download button. The viewer worked out a file's URL by slicing the chat id off the front of its database key. That fits the key the API sweep writes and not the one the importer writes. It now builds the URL from the message and type the row actually carries, and looks the row up by those columns, so the spelling of the key stops mattering. Two smaller symptoms of the same cause go with it: the gallery's "load more" stopped dead at the first imported item, and imported round videos appeared under no gallery tab at all. Reported by [@WalterLederer](https://github.com/WalterLederer) in [#423](https://github.com/GeiserX/Telegram-Archive/issues/423), with the debug trace and the row dump that made it a short hunt.
- **`VERIFY_MEDIA=true` no longer re-downloads an entire imported archive.** The importer stores a file's location relative to the media root. Verification read it as a plain path, which resolved against the working directory instead, so every imported file looked missing and was fetched again. On a 1 TB import that meant a 1 TB download and a doubled disk footprint. The same mismatch made the delete path skip the file while still removing its database row, orphaning bytes nothing ever reclaimed, and it could blank the recorded location of a file that was sitting on disk the whole time. All of these now resolve against the media root, and a path that would climb out of the archive resolves to nothing. ([#310](https://github.com/GeiserX/Telegram-Archive/issues/310))
- **A media URL no longer carries a chat id back to the browser.** A row the viewer could not build a key for kept its raw storage key, which contains the chat id, and the gallery sent it back as a pagination cursor. ([#423](https://github.com/GeiserX/Telegram-Archive/issues/423))
- **[`scripts/restore_chat.py`](../scripts/restore_chat.py) finds imported attachments.** It reported them as missing, because it joined their location to the backup root rather than the media root.

### Note

This release rewrites nothing. No database rows change, no files move, no media ids change. An archive already damaged by the re-download bug above keeps its duplicate rows and duplicate files. This stops more being made. The viewer half takes effect as soon as you pull the viewer image, with no backup run needed.

## [8.4.1] - 2026-08-26

### Fixed

- **The viewer no longer leaves a dead band below the message list on phones.** 8.4.0 sized the layout root to `100dvh`, meaning to keep bottom-anchored content clear of iOS Safari's retractable toolbar. Combined with the safe-area padding and `overflow: hidden` that `body` already carries, the dynamic unit resolved to less than the visible area, so the app rendered short and the space under the newest message went to waste. The layout root is back to what it was before 8.4.0. The scroll-to-latest button, whose clipping 8.4.0 also addressed, stays fixed — that was a separate cause and a separate fix.

## [8.4.0] - 2026-08-26

The viewer's colors are yours now, and two capture bugs are gone.

### Added

- **Seven color themes, picked from the sidebar header.** Slate (the palette the viewer has always had) stays the default; Telegram Night, AMOLED, Forest and Aubergine join it, plus two light modes — Day and Paper. The choice is remembered per browser, applied before the page paints, and shareable as a `?theme=` link. See [docs/VIEWER-THEMES.md](VIEWER-THEMES.md). ([#420](https://github.com/GeiserX/Telegram-Archive/pull/420))
- **`VIEWER_DEFAULT_THEME`** picks the palette a deployment hands to browsers that have not chosen one. A viewer's own choice always wins over it. ([#420](https://github.com/GeiserX/Telegram-Archive/pull/420))

### Fixed

- **The listener no longer misses the first message from a chat it has never backed up.** A new DM from someone you had never spoken to, or a group you had just joined, stayed invisible until the next scheduled sweep discovered it. The listener now reads the chat's type straight off the event — no extra network call — and applies exactly the decision the scheduled backup would. Chats your exclude lists or include-whitelists keep out of the archive are still never captured. Thanks to [@jordanfelle](https://github.com/jordanfelle). ([#416](https://github.com/GeiserX/Telegram-Archive/issues/415))
- **The scroll-to-latest button is whole again on phones.** It was rendering half off the bottom-left edge — a `relative` utility on the button was overriding its own positioning — and the app sized itself to `100vh`, which on phones includes the strip behind the retractable browser toolbar. ([#419](https://github.com/GeiserX/Telegram-Archive/pull/419))
- The example `docker-compose.yml` pinned 8.1.0, the last release that shipped amd64-only images, so anyone copying it onto an ARM machine got a version that could not run there. Thanks to [@skywinder](https://github.com/skywinder). ([#417](https://github.com/GeiserX/Telegram-Archive/pull/417))

### Internal

- A release can no longer ship stale pins or half its architectures. The image pins in `docker-compose.yml`, the README and the migration helper must name the version being released, and both publish workflows now read each pushed tag's manifest back from the registry and fail unless amd64 and arm64 are both in it. ([#418](https://github.com/GeiserX/Telegram-Archive/pull/418))

## [8.3.2] - 2026-08-25

Dependency maintenance. Nothing about how the archive behaves changes.

### Changed

- **Telethon 1.44.0** (scheme layer 227). Two of its fixes land on exactly this workload: it no longer times out handling server salts, and no longer burns CPU when Telegram closes a connection from its end. ([#412](https://github.com/GeiserX/Telegram-Archive/pull/412))
- FastAPI, Uvicorn, websockets, Alembic, BeautifulSoup, ijson and pywebpush each move up a minor or two; ruff, pytest and pre-commit follow on the development side. ([#412](https://github.com/GeiserX/Telegram-Archive/pull/412), [#410](https://github.com/GeiserX/Telegram-Archive/pull/410))
- Dependency updates now arrive with `uv.lock` already updated. They had been proposed against `pyproject.toml` alone, which CI refuses to resolve because it installs with `uv sync --locked` — so every one of them arrived with red checks that had nothing to do with the dependency. ([#411](https://github.com/GeiserX/Telegram-Archive/pull/411))

## [8.3.1] - 2026-08-24

### Fixed

- **The viewer rendered raw template over the whole UI.** 8.3.0's template lost the export modal's closing tags (a modal was spliced in mid-element); browsers recovered by force-closing open elements, which pushed every later modal outside Vue's mount target — they showed as un-compiled `{{ }}` overlays with dead buttons, covering the app on every browser. The structure is restored, and a new structural test makes the whole class unshippable: the template must parse perfectly balanced, with every modal inside the mount target. ([#408](https://github.com/GeiserX/Telegram-Archive/pull/408))

## [8.3.0] - 2026-08-23

Fidelity and scale: the archive now keeps what official clients show — formatting, forward origins, link previews, the media kinds that used to vanish — searches it through a real text index, and survives gigabyte imports. Every change shipped through its own reviewed PR.

### Added

- **Real full-text search.** Searching a chat (or everything) uses a proper text index — SQLite FTS5 with trigger-maintained sync, PostgreSQL stored `tsvector` with a GIN index — with official-app word-prefix semantics and diacritics folding (`cafe` finds `Café`). Databases without the index layer, and punctuation-only searches, keep the old substring behavior. Query tokenization was proven against each engine's real parser, hostile input included. ([#404](https://github.com/GeiserX/Telegram-Archive/pull/404))
- **Message formatting survives the archive.** Bold, italic, strikethrough, code, quotes, spoilers, custom links and the rest are captured as entities on both the sweep and the real-time listener — edits included — and the viewer renders them: spoilers blur until clicked, links are scheme-checked, malformed nesting is clipped safely. Formatting-only edits no longer masquerade as content edits. ([#402](https://github.com/GeiserX/Telegram-Archive/pull/402))
- **Forwards keep their origin.** Who a message was forwarded from is stored — channel-post pointer included — and the viewer header links to the origin when it is archived, resolving it across pages and for metadata-only forwards. ([#400](https://github.com/GeiserX/Telegram-Archive/pull/400))
- **Nine media kinds stop vanishing.** Venues, dice, invoices, stories, giveaways and their results, live locations, games and unsupported media render as labeled chips instead of empty bubbles; invoice amounts format by their currency's minor units. ([#401](https://github.com/GeiserX/Telegram-Archive/pull/401))
- **Link-preview images download like any photo.** A webpage embed's image — photo- or document-backed — is fetched and rendered on the preview card instead of being silently dropped. ([#403](https://github.com/GeiserX/Telegram-Archive/pull/403))
- **Gigabyte exports import, and interrupted imports resume.** `result.json` streams one message at a time (memory stays flat no matter the file size), progress checkpoints per chat, and re-running the same command on the same file skips finished chats and replays the interrupted one to a state identical to an uninterrupted run — no `--merge` needed. Truncated files fail loudly with progress preserved. ([#406](https://github.com/GeiserX/Telegram-Archive/pull/406))
- **Imported media is adopted, not re-downloaded.** When the API sweep reaches a message whose file arrived via a Telegram Desktop import, it re-keys the imported record and reuses the on-disk file — no duplicate row, no duplicate download. `backfill-topics <chat_id>` restores forum topics on imported chats (exports carry no topic metadata) by resetting the sweep cursor and re-sweeping text-only with the footguns disabled. ([#405](https://github.com/GeiserX/Telegram-Archive/pull/405))
- **Every message has a shareable link.** Copy a deep link to any archived message, and paste a `t.me` link into the search box to jump straight to the archived copy. ([#399](https://github.com/GeiserX/Telegram-Archive/pull/399))
- **An archive status panel that answers "is it healthy right now".** Sync recency, media backlog, listener liveness and storage at a glance. ([#398](https://github.com/GeiserX/Telegram-Archive/pull/398))
- **A what-changed feed.** The deletions and edits the archive preserved, browsable in one place. ([#397](https://github.com/GeiserX/Telegram-Archive/pull/397))

### Fixed

- The export command's advertised date-range flags actually filter the export stream. ([#396](https://github.com/GeiserX/Telegram-Archive/pull/396))
- Chat filter ids missing the `-100` prefix auto-correct instead of silently matching nothing, and a numeric env-var typo names the variable instead of reading like a Python bug. ([#395](https://github.com/GeiserX/Telegram-Archive/pull/395), [#390](https://github.com/GeiserX/Telegram-Archive/pull/390))
- Excluding the General topic actually excludes its messages — Telegram omits the topic marker for General, which used to bypass the filter. ([#394](https://github.com/GeiserX/Telegram-Archive/pull/394))
- A notification burst can no longer grow the viewer's task set without bound. ([#391](https://github.com/GeiserX/Telegram-Archive/pull/391))
- The importer no longer erases captured usernames and phone numbers: user updates write only the fields the import actually observed. ([#406](https://github.com/GeiserX/Telegram-Archive/pull/406))
- Two tests that could never fail now can — the version-dedup ON CONFLICT clause and the max-message-id checkpoint are both mutation-gated. ([#393](https://github.com/GeiserX/Telegram-Archive/pull/393), [#392](https://github.com/GeiserX/Telegram-Archive/pull/392))

Upgrading needs no manual steps: migration 028 (the search index) runs automatically and is safe to re-run; existing rows are indexed on first upgrade.

## [8.2.0] - 2026-08-22

The largest maintenance release to date: one headline feature (the outbound event webhook, [#336](https://github.com/GeiserX/Telegram-Archive/issues/336)) on top of a systematic sweep of the whole codebase — capture, database, viewer, importers, CI and operations. Every change below shipped through its own reviewed PR.

### Added

- **Outbound event webhook — get pinged the instant a message is edited or deleted.** Opt-in via `EVENT_WEBHOOK_ENABLED`: the real-time listener fires an HTTP request the moment it commits an edit or deletion, with a fully templated body (`{placeholder}` / `{placeholder|filter}` substitution, auto-escaped for the declared Content-Type), per-chat filtering, custom method and headers — point it at ntfy, Gotify, Slack, Discord or anything you run. Deleted text is included in **both** `DELETION_MODE=soft` and `hard`: the row is snapshotted inside the deleting transaction, before destruction. Delivery is fire-and-forget with bounded retry (3 attempts, 5s timeout, no redirects, 100 in-flight cap); the URL is treated as a capability secret and never logged. Sweep-detected changes deliberately do not fire it, and startup warns about flag combinations under which a selected event can never fire. ([#336](https://github.com/GeiserX/Telegram-Archive/issues/336), [#388](https://github.com/GeiserX/Telegram-Archive/pull/388))
- **Tap a #hashtag or $cashtag to see everything using it.** Hashtags and cashtags in archived messages are now links that run a scoped search. ([#321](https://github.com/GeiserX/Telegram-Archive/pull/321))
- **Link previews are archived with the message.** The preview Telegram rendered (title, description, site name, image) is captured alongside the text, so a link that later dies keeps its preview in the archive. ([#323](https://github.com/GeiserX/Telegram-Archive/pull/323))
- **A dead archiver no longer looks healthy.** The backup container exposes a real liveness signal tied to the scheduler actually ticking, instead of "the process exists". ([#327](https://github.com/GeiserX/Telegram-Archive/pull/327))
- **Gap-fill reports history missing before the earliest archived message.** Leading holes — history that predates the first archived message — are detected and reported (never auto-fetched), and probe errors are counted honestly. ([#363](https://github.com/GeiserX/Telegram-Archive/pull/363))

### Fixed

**Capture correctness**

- Quoted-reply excerpts are captured with the message instead of being dropped. ([#362](https://github.com/GeiserX/Telegram-Archive/pull/362))
- A peerless deletion event can never tombstone a channel message by id collision again. ([#326](https://github.com/GeiserX/Telegram-Archive/pull/326))
- Whitelist mode no longer wipes every other chat's `archived` flag with a fabricated `0`. ([#334](https://github.com/GeiserX/Telegram-Archive/pull/334))
- The deletion/edit sync pairs Telegram's response by message id, never by list position, so a partial response can no longer mis-attribute edits. ([#373](https://github.com/GeiserX/Telegram-Archive/pull/373))
- The incremental-backup cursor is monotonic — a stale sweep can no longer move it backwards and re-open a window that was already captured. ([#366](https://github.com/GeiserX/Telegram-Archive/pull/366))
- A message that vanishes mid-download finally counts against the media retry cap instead of retrying forever. ([#360](https://github.com/GeiserX/Telegram-Archive/pull/360))
- Gap-fill sees bot conversations. ([#351](https://github.com/GeiserX/Telegram-Archive/pull/351))
- `MAX_MEDIA_SIZE_MB` caps full-resolution photos too, and `0` (or negative) means "no limit" instead of "skip every file". ([#354](https://github.com/GeiserX/Telegram-Archive/pull/354), [#361](https://github.com/GeiserX/Telegram-Archive/pull/361))
- A failed chat-list refresh no longer silently stops all real-time capture. ([#339](https://github.com/GeiserX/Telegram-Archive/pull/339))
- Imports stop rewriting captured chats, speak the system's chat-type vocabulary, and a partial export can no longer amputate older history. ([#355](https://github.com/GeiserX/Telegram-Archive/pull/355), [#335](https://github.com/GeiserX/Telegram-Archive/pull/335))
- HTML imports no longer shift every message by the exporter's timezone, and offset-bearing datetimes everywhere are converted to UTC instead of relabelled. ([#364](https://github.com/GeiserX/Telegram-Archive/pull/364), [#365](https://github.com/GeiserX/Telegram-Archive/pull/365))

**Database & migrations**

- The SQLite→PostgreSQL migration can no longer silently lose rows mid-copy: the batched read is keyset-ordered, and the script says plainly that the backup container must be stopped during the copy. ([#368](https://github.com/GeiserX/Telegram-Archive/pull/368))
- A failed media-sharding relocate no longer leaves archived media unopenable — relocation is transactional with a content-hash adoption guard and an orphan sweep at migration start. ([#371](https://github.com/GeiserX/Telegram-Archive/pull/371))
- Deleting a chat purges its push subscriptions and serializes concurrent deletes (`FOR UPDATE`), and excluding a chat purges it no matter which folder it lives in. ([#357](https://github.com/GeiserX/Telegram-Archive/pull/357), [#356](https://github.com/GeiserX/Telegram-Archive/pull/356))
- Media verification never destroys a file before its replacement exists, and verification streams the table instead of materialising it. ([#325](https://github.com/GeiserX/Telegram-Archive/pull/325), [#332](https://github.com/GeiserX/Telegram-Archive/pull/332))
- The crash-loop guards in migrations 007–013 have regression tripwires, and `DB_TYPE` letter-case can no longer skip migrations. ([#347](https://github.com/GeiserX/Telegram-Archive/pull/347), [#346](https://github.com/GeiserX/Telegram-Archive/pull/346))

**Viewer & web**

- Clicking the playing audio track no longer shows someone else's conversation in a forum, and audio errors surface instead of failing silently. ([#367](https://github.com/GeiserX/Telegram-Archive/pull/367))
- A restricted viewer can no longer see archive-wide totals when the stats cache is thin — stats fail closed. ([#369](https://github.com/GeiserX/Telegram-Archive/pull/369))
- Share-token verification no longer freezes the viewer's event loop. ([#324](https://github.com/GeiserX/Telegram-Archive/pull/324))
- All frontend assets are vendored — no CDN can ship script into an archive session — and the shipped debug page is gone, along with exception text in route logs. ([#342](https://github.com/GeiserX/Telegram-Archive/pull/342), [#322](https://github.com/GeiserX/Telegram-Archive/pull/322))
- WebSocket connections and per-socket subscriptions are capped, and the realtime notifier rides the listener's own engine instead of a process-global. ([#341](https://github.com/GeiserX/Telegram-Archive/pull/341), [#358](https://github.com/GeiserX/Telegram-Archive/pull/358))
- A broken media volume reports what it is instead of "Database temporarily unavailable". ([#353](https://github.com/GeiserX/Telegram-Archive/pull/353))
- A GIF that finishes downloading mid-session starts playing without a reload. ([#359](https://github.com/GeiserX/Telegram-Archive/pull/359))

**Operations, Docker & CI**

- `docker stop` ends in a clean shutdown instead of SIGKILL, and container logs rotate instead of filling the disk. ([#328](https://github.com/GeiserX/Telegram-Archive/pull/328))
- A late backup tick runs instead of being silently dropped. ([#331](https://github.com/GeiserX/Telegram-Archive/pull/331))
- Every documented `.env` variable actually reaches the containers, and real-time pushes reach the viewer on the stock SQLite compose stack. ([#333](https://github.com/GeiserX/Telegram-Archive/pull/333), [#343](https://github.com/GeiserX/Telegram-Archive/pull/343))
- The backup image stops shipping a 182 MB C toolchain, and the viewer image ships only what the viewer can import. ([#352](https://github.com/GeiserX/Telegram-Archive/pull/352), [#338](https://github.com/GeiserX/Telegram-Archive/pull/338))
- CI publishes the multi-arch images it set up QEMU for, and a dispatch republish rebuilds the tagged commit instead of re-stamping a version onto main. ([#337](https://github.com/GeiserX/Telegram-Archive/pull/337), [#340](https://github.com/GeiserX/Telegram-Archive/pull/340))
- The viewer's shutdown no longer hangs into SIGKILL on a dead database socket. ([#372](https://github.com/GeiserX/Telegram-Archive/pull/372))

### Performance

- Opening a big forum's topic list stops aggregating every message row: the widened covering index takes the scoped aggregate from 19.0 ms to 1.3 ms at 60k rows. ([#370](https://github.com/GeiserX/Telegram-Archive/pull/370))
- A cold gallery grid decodes each missing thumbnail once, not once per concurrent request, and gallery pages stop sorting every media row in the chat. ([#348](https://github.com/GeiserX/Telegram-Archive/pull/348), [#349](https://github.com/GeiserX/Telegram-Archive/pull/349))
- Video thumbnails warm at capture time instead of first view. ([#350](https://github.com/GeiserX/Telegram-Archive/pull/350))
- Media hashing and thumbnailing leave the event loop; per-message reaction locks and sender re-upserts that were guaranteed no-ops are skipped. ([#329](https://github.com/GeiserX/Telegram-Archive/pull/329), [#330](https://github.com/GeiserX/Telegram-Archive/pull/330))
- A new realtime message stops re-walking every loaded message object in the viewer. ([#359](https://github.com/GeiserX/Telegram-Archive/pull/359))

### Test infrastructure

- The suite's dark branches got lit: shared-store dedup matching, the viewer lifespan restart path, and real EXPLAIN-plan assertions for the new indexes. ([#344](https://github.com/GeiserX/Telegram-Archive/pull/344), [#345](https://github.com/GeiserX/Telegram-Archive/pull/345), [#312](https://github.com/GeiserX/Telegram-Archive/pull/312))

### Fixed (review-cycle hardening, merged after the sweep)

- The realtime viewer listener re-raises cancellation instead of swallowing it, and its teardown is bounded — a dead database socket can no longer hang viewer shutdown into SIGKILL. ([#372](https://github.com/GeiserX/Telegram-Archive/pull/372))
- Chat-action handlers carry precise types, forward ids use marked form everywhere, and `VIEWER_TIMEZONE`/`STATS_CALCULATION_HOUR` validate at construction. ([#374](https://github.com/GeiserX/Telegram-Archive/pull/374), [#375](https://github.com/GeiserX/Telegram-Archive/pull/375), [#376](https://github.com/GeiserX/Telegram-Archive/pull/376))
- A premium-flood answer (`FloodPremiumWaitError`) pauses the reaction re-sweep and the connect retry exactly like a plain flood, in every catch site including the fallback fetch. ([#377](https://github.com/GeiserX/Telegram-Archive/pull/377))
- `DATABASE_TIMEOUT` finally reaches SQLite's `busy_timeout` — and `nan`/`inf`/sub-millisecond values can neither abort startup nor silently disable the wait. ([#378](https://github.com/GeiserX/Telegram-Archive/pull/378))
- Dead capture code removed, and the mass-operation story now matches what actually runs; non-positive limiter settings fail loudly instead of silently disarming the mass-deletion guard. ([#379](https://github.com/GeiserX/Telegram-Archive/pull/379), [#380](https://github.com/GeiserX/Telegram-Archive/pull/380))
- The stock two-container SQLite stack live-updates with zero config: the internal push secret is auto-shared through the volume both containers already mount, published atomically behind a private-file read contract. ([#381](https://github.com/GeiserX/Telegram-Archive/pull/381))
- Push notifications reuse the sender-name the listener already resolved, and forwarded-source names are resolved once per source per run with FIFO eviction at the cache cap. ([#382](https://github.com/GeiserX/Telegram-Archive/pull/382), [#383](https://github.com/GeiserX/Telegram-Archive/pull/383))
- The hygiene pre-commit hooks run in CI (tokenless, read-only job), so a fork PR can no longer bypass them. ([#384](https://github.com/GeiserX/Telegram-Archive/pull/384))
- A media row whose file vanished before download still gets its retry row, parallel downloads pay the flood-limited auth export once per DC instead of once per file (and a cancelled build closes every sender), and gallery pages count messages — not media rows — against the page LIMIT. ([#385](https://github.com/GeiserX/Telegram-Archive/pull/385), [#386](https://github.com/GeiserX/Telegram-Archive/pull/386), [#387](https://github.com/GeiserX/Telegram-Archive/pull/387))

## [8.1.0] - 2026-08-19

### Added

- **Capture filters resolve per account.** 8.0 made credentials per-account but left every capture filter global, so an account added to a whitelisted install captured only the intersection of that whitelist with its own dialogs — observed shrinking a real second account from 2,057 chats to 312, silently. Every filter can now be overridden per account the way sessions already resolve: `TG_ACCOUNT_<N>_CHAT_IDS`, `TG_ACCOUNT_<N>_CHAT_TYPES`, the include/exclude lists (global and per-type), `TG_ACCOUNT_<N>_PRIORITY_CHAT_IDS` and `TG_ACCOUNT_<N>_SKIP_MEDIA_CHAT_IDS` — the indexed variable wins for that account, the global one is the fallback, so an install without overrides behaves exactly as before. An empty indexed value inherits (Compose's `${VAR:-}` idiom injects empty strings, and silently clearing a whitelist would widen capture); the literal token `none` is the explicit-empty override, so `TG_ACCOUNT_2_CHAT_IDS=none` runs account 2 type-based while account 1 keeps its whitelist. Startup logs each account's effective scope as counts, so an inherited filter is visible on day one. ([#313](https://github.com/GeiserX/Telegram-Archive/issues/313), [#319](https://github.com/GeiserX/Telegram-Archive/pull/319))

### Fixed

- **Per-account state no longer collides between accounts.** Five metadata caches were keyed globally while chat ids stopped being globally unique in 8.0: followed-migration ids leaked from one account's sweep into the other's live-accept set, two whitelisted accounts overwrote each other's unresolved-id suppression, reaction-resweep cursors and per-chat failure records crossed accounts, and one account's listener stopping cleared the "listener active" flag while the other still listened. All five are account-scoped now — account 1 keeps the bare legacy keys, so existing installs read their history unchanged — and the viewer reports listener status across accounts (active when any listener is up, since the earliest). ([#319](https://github.com/GeiserX/Telegram-Archive/pull/319))

## [8.0.3] - 2026-08-19

### Fixed

- **Chats that two accounts share receive live updates again.** Since 8.0 a chat id can belong to two accounts, and the realtime path resolved each event by bare chat id: once a second account shared the chat, the lookup became ambiguous and the viewer dropped the frame. Dropping was the right refusal — a frame naming a chat a subscriber is not entitled to would leak it — but the consequence was that every shared chat went realtime-dead for everyone, steadily, until the next page load. Events now carry the account that captured them, the viewer resolves the (account, chat) pair — the primary key, which cannot be ambiguous — and the short-lived resolution cache keys on the same pair, so one account's ref is never served to the other account's subscribers. A payload without the account (an older backup container mid-upgrade) keeps the old drop-on-ambiguity guard, so a mixed-version deploy degrades to the previous behavior instead of ever leaking. ([#315](https://github.com/GeiserX/Telegram-Archive/issues/315), [#317](https://github.com/GeiserX/Telegram-Archive/pull/317))

### Changed

- **Dropped `idx_messages_chat_id`, an index no query plans against.** It shipped with the initial schema and is a strict prefix of two later composites — `(chat_id, id)` and `(chat_id, date DESC)` — either of which serves a bare chat-id predicate. Measured on two live archives, including a 1.78M-row production instance: zero steady-state scans; the only period it ever carried plans was while the composite indexes were accidentally absent, which is exactly the drift migration `024` now heals on its own. Migration `025` drops it where present, saving one index write per archived message plus its disk footprint, and `downgrade()` restores it. Contributed by [@jordanfelle](https://github.com/jordanfelle), with the `pg_stat_user_indexes` evidence to prove it. ([#316](https://github.com/GeiserX/Telegram-Archive/pull/316))

## [8.0.2] - 2026-08-18

### Fixed

- **The chat list could take minutes, or never finish, on an archive missing an index it was never told it lacked.** `get_all_chats` reads each chat's last message through a correlated `MAX(messages.date)` subquery — one seek per chat against `idx_messages_chat_date_desc`. Where that index is absent, every listed chat instead scans its own messages, so listing chats costs a read of the archive: on the installation that reported this, chat-list queries ran for twenty to thirty-five minutes and stacked up until the database served nothing else. The index has been declared in models.py since migration `002`, which is exactly why nobody noticed it could be missing. `Base.metadata.create_all(checkfirst=True)` — how the SQLite→PostgreSQL move builds its target schema — skips a table *and every index on it* once the table exists, `checkfirst` being per-table and not per-index; an index added to models.py after a database was created therefore never appears in it, while the migration that would have added it is skipped too because the database was stamped past that revision. Migration `024` recreates `idx_messages_chat_date_desc` and `idx_messages_chat_pinned` wherever they are absent, and is a no-op on the databases that have them. Restoring them took that query from over twenty minutes to 39 ms on the affected archive. ([#314](https://github.com/GeiserX/Telegram-Archive/pull/314))
- **A viewer restricted to a few chats made the server read every chat in the archive, four times per page load.** Entitlements were applied in Python after the fact: the chat list, the folder list, the archive counter and the statistics panel each loaded every chat row and then filtered, so the narrower a viewer's grant, the more work their page cost — a restricted account paid an 18× penalty over an unrestricted one on the same page. Visibility now compiles to SQL, from the same `ChatScope` definition the per-row check uses, so the two cannot drift apart: a viewer entitled to one chat touches one row instead of 4,784, with 2,262× less I/O. An empty grant emits an explicitly false predicate rather than relying on how a database renders `IN ()` — access control should not rest on that. Unrestricted access is unchanged, byte-for-byte the same statements as before, and 6,006 comparisons across 546 grant configurations confirmed the SQL filter admits and refuses exactly what the Python one did. ([#314](https://github.com/GeiserX/Telegram-Archive/pull/314))

## [8.0.1] - 2026-08-16

### Fixed

- **An archive holding messages whose chat no longer exists can now upgrade.** The first real production archive to attempt the 8.0 upgrade was refused by it: years of Telegram's group→supergroup renumbering had left messages and sync rows whose `chats` row was long gone, and recreating the composite foreign keys re-checks every row, so migration `022` aborted on the first orphan it met — on PostgreSQL these rows hide behind a `convalidated` flag that still reads true, because the bulk copies that let them in (the SQLite→PostgreSQL mover among them) run with enforcement disabled and nothing ever re-checks. The transaction rolled back cleanly, exactly as designed, but a refusal keeps that history hostage on 7.x forever — and an archive old enough to carry renumbering scars is precisely the archive 8.0 exists for. Migration `022` now creates a neutral placeholder chat for every distinct chat id that `messages`, `sync_status`, `forum_topics` or `chat_folder_members` still references and `chats` no longer holds — typed by the id pattern alone, empty title, never anything derived from message content — before it touches those tables. The placeholders ride the same machinery as every real chat (account 1, minted ref, recreated keys), so the formerly orphaned history becomes first-class and the viewer serves it under its placeholder chat: after this upgrade you may find chats with no title in your sidebar, and they are exactly that recovered history. SQLite archives carried the same orphans *through* `022` silently instead of failing on them; the same step now heals them too, so both backends land on the same end state. The one case this cannot reach is a SQLite archive that already completed the 8.0.0 upgrade with orphans aboard — `022` never runs twice, so that history stays as unreachable as it was on 7.x. ([#308](https://github.com/GeiserX/Telegram-Archive/pull/308))

## [8.0.0] - 2026-08-16

### Breaking

- **One archive can now hold more than one Telegram account, and the database is rewritten once to make that safe.** Every chat, message, media row, sync cursor, forum topic and folder is now keyed by the account that captured it. Migration `022` performs the rewrite in a single transaction on both SQLite and PostgreSQL; your existing history becomes account 1, and nothing on disk moves — media stays exactly where it is. The keys had to change rather than gain a decorative column, because a chat id, a message id, a topic id and a folder id are each only unique *within* one account: a second account's edit history hashed identically to the first's and was silently discarded, its folders (which every account numbers from 2) took over the first account's membership rows, and its copy of a shared message read the first account's "outgoing" flag. Read **[docs/UPGRADING-8.0.md](UPGRADING-8.0.md)** before you upgrade, and take a backup — that guide exists because this is the one release where the way back is a restore. ([#302](https://github.com/GeiserX/Telegram-Archive/pull/302))
- **Downgrading past migration `022` is refused, deliberately.** Once a second account exists the old keys cannot identify a row: two messages that differ only by account collapse onto one key, so a reversal would have to choose whose copy of every shared message, folder and edit history to destroy. An *interrupted* upgrade needs no rollback at all — the migration is one transaction, so a failure leaves a byte-for-byte intact 7.x database that the previous image still runs. After a *successful* upgrade the way back is the backup taken before it, which is the only honest answer and the reason the guide asks for one first. ([#302](https://github.com/GeiserX/Telegram-Archive/pull/302))
- **Every viewer URL changes shape, so existing bookmarks and share links stop working.** Chat-scoped routes, the WebSocket protocol, media, thumbnails and avatars now address a chat by an opaque 22-character ref minted per chat, instead of by its Telegram chat id — so no chat id reaches a URL, the browser history or a reverse proxy's access log. One dependency resolves the ref and enforces access in a single query, replacing ten per-route permission preambles: an unknown ref, a malformed one and a chat you are not allowed to see all answer an identical 404, so nothing distinguishes them. Open the chat from the sidebar and bookmark it again; share links must be minted again. The archive itself is untouched. ([#304](https://github.com/GeiserX/Telegram-Archive/pull/304))
- **Admin writes now carry `allowed_accounts` and `allowed_chat_refs` in place of `allowed_chat_ids`.** A write still sending the old field is rejected with a 400 that names the replacement, rather than being reinterpreted — an unconverted `[123, 456]` read under the new meaning would be "account 123, chat 456" and would *grant* access rather than deny it. The rules are: `null` means unrestricted, a list means exactly those and nothing else, and anything unparseable means nothing at all. Migration `022` converts every restricted viewer, session and share token as part of the rewrite, so existing grants keep working without being re-entered. The 7.x `allowed_chat_ids` column is never read as a grant again; 8.0 keeps writing a deny-only marker into it purely so a rollback to a 7.x image cannot widen anyone's access. ([#302](https://github.com/GeiserX/Telegram-Archive/pull/302), [#304](https://github.com/GeiserX/Telegram-Archive/pull/304))
- **`POST /api/push/subscribe` takes `chat_ref` where it took `chat_id`.** The browser resubscribes on its own the next time you open the viewer; a subscription made by your own script needs the field renamed. ([#304](https://github.com/GeiserX/Telegram-Archive/pull/304))

### Added

- **One archive, many Telegram accounts.** Accounts are declared as indexed environment variables (`TG_ACCOUNT_1_*`, `TG_ACCOUNT_2_*`, …), each with its own session file, and the scheduled sweep runs them one after another rather than at once — two accounts on one connection is how you get rate-limited on both. Each account keeps its own chats, its own sync cursors and its own folder numbering, and the viewer serves every account a login is entitled to see. Upgrading needs no configuration at all: if no indexed account is declared, account 1 is synthesized from the `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_PHONE` you already have, and it adopts the session you already logged in with — so a single-account install keeps running exactly as before, without a re-login. Account rows are owned by the Telegram user id, not the variable position, so re-ordering the variables can never split or steal an account's data. ([#302](https://github.com/GeiserX/Telegram-Archive/pull/302), [#305](https://github.com/GeiserX/Telegram-Archive/pull/305))
- **Search is dramatically faster on PostgreSQL.** A trigram GIN index (`pg_trgm`) backs message search, so a substring query stops scanning the whole table. PostgreSQL only — SQLite search is unchanged — and migration `023` creates both the extension and the index. Contributed by [@jordanfelle](https://github.com/jordanfelle). ([#301](https://github.com/GeiserX/Telegram-Archive/pull/301))

### Fixed

- **An initial backup of an account with many chats can now finish.** `get_dialogs()` pages internally in chunks of about a hundred, and with the app-wide flood threshold at zero a rate limit on any single page aborted the entire call — after which the retry restarted pagination from page one, re-walked every page that had already succeeded, and tripped the same later page again, every run forever. Floods are now absorbed in place up to `DIALOG_FLOOD_SLEEP_THRESHOLD` seconds (default 60) and the same page resumes, which is what already fixed the equivalent failure for media downloads. Verified against a production account of ~1,900 dialogs whose backup had never once completed. Contributed by [@jordanfelle](https://github.com/jordanfelle). ([#295](https://github.com/GeiserX/Telegram-Archive/issues/295), [#296](https://github.com/GeiserX/Telegram-Archive/pull/296))
- **One unreadable attachment no longer freezes a chat's backup forever.** Telegram returns an empty document for a file it can no longer retrieve; that object is truthy but carries no attributes, so walking it raised an error which aborted the whole dialog in the scheduled sweep and dropped the message outright — text included — in the real-time listener. Because the cursor is checkpointed before the offending message, every later run resumed at the same message and failed identically, while the run still reported "Backup completed successfully". An unusable document reference is now treated exactly like a missing one, in all five places that walked it. ([#283](https://github.com/GeiserX/Telegram-Archive/pull/283))
- **A single message that cannot be processed no longer stalls its chat, and is never skipped in silence.** Per-message failures are contained and counted instead of aborting the dialog, and the cursor is held at the first failed id so it can never checkpoint past unprocessed history. A message that has failed on two separate runs is then passed over, and the ids passed over are recorded durably, in the existing metadata table — so the freeze has an exit and the skip leaves a trace. The first failure still behaves exactly as before, so a transient error keeps its retry. Measured before the fix, one chat's runs kept growing — 10, 8, 10, 12, 14, 16 messages re-fetched — while the cursor never moved. ([#286](https://github.com/GeiserX/Telegram-Archive/pull/286), [#292](https://github.com/GeiserX/Telegram-Archive/pull/292))
- **The record of a passed-over message is now written before the cursor is allowed to move past it.** The write could fail — it had no retry for a locked database while the listener writes the same file — and the failure was swallowed while the cursor advanced anyway, recreating the exact silent skip the record exists to prevent, under a warning claiming the ids had been kept. Metadata reads and writes now retry lock contention like the sync cursor does, the give-up branch advances only once the record has actually landed, and the frozen half of the record is cleared when its message finally succeeds. ([#297](https://github.com/GeiserX/Telegram-Archive/pull/297))
- **A link in a message now goes to the host it displays.** Message text was escaped and then decoded again while building the link, and the browser decoded it a second time, so a character reference in the original text became a real character in the target: a message reading `https://good.example&#x40;evil.example/` rendered as a link to "good.example" while resolving to `evil.example`, because `&#x40;` became `@` and demoted the visible host to a username. Anyone who could message the archived account could plant one. The escape-then-decode round trip is gone rather than patched — the raw text is split on the URL pattern and each piece escaped exactly once — so there is no decode step left to exploit. Verified in a real browser across 36 payloads. ([#291](https://github.com/GeiserX/Telegram-Archive/pull/291))
- **The viewer's access control can no longer be bypassed, on either transport.** The WebSocket route resolved who you were differently from the HTTP routes, so with proxy auth and password auth both enabled a socket with no credentials was accepted with no chat restrictions at all, while a genuine restricted viewer got none of its own. Both now resolve through one shared resolver. The thumbnail route authorized the raw request string but resolved a different path from it, so a percent-encoded `..` read media from chats the viewer may not see; both media routes now check and normalize once, up front, so the string that is authorized is the string that selects the file. ([#290](https://github.com/GeiserX/Telegram-Archive/pull/290))
- **An archived HTML or SVG attachment can no longer run as a script against your viewer session.** Only images, video, audio and PDFs are still served inline; everything else downloads. Access-controlled media and thumbnails are no longer marked cacheable by shared proxies, and viewers who may not download are no longer handed thumbnail URLs that refuse them. ([#290](https://github.com/GeiserX/Telegram-Archive/pull/290))
- **A crafted image can no longer exhaust the viewer's or the archiver's memory.** Thumbnail generation is gated on decoded pixel count rather than file size, so a file that is small on disk and enormous once decoded — which any Telegram contact can send — is refused. The gate is format-aware, so large JPEGs still produce thumbnails, and it now covers the video lane, where a crafted file renamed with a video extension previously skipped the check entirely. The same gate was added to the pre-generated thumbnails in the backup process. Thumbnails are written to a temp file and atomically replaced, so a concurrent reader can never cache a torn image, and a failed video thumbnail is remembered instead of being retried on every request. ([#286](https://github.com/GeiserX/Telegram-Archive/pull/286), [#287](https://github.com/GeiserX/Telegram-Archive/pull/287))
- **One unreachable push endpoint no longer stalls the viewer for everyone.** The web-push fan-out runs off the event loop, concurrently, with a real timeout. Password, token and share-token hashing moved off the event loop too, and avatar lookup no longer scans a directory there, so one request can no longer freeze every other request and WebSocket frame. A broadcast now iterates a snapshot, so a client connecting or disconnecting mid-send no longer aborts delivery to everyone else. ([#287](https://github.com/GeiserX/Telegram-Archive/pull/287), [#290](https://github.com/GeiserX/Telegram-Archive/pull/290))
- **Re-importing a Telegram Desktop export over an already-archived chat no longer erases what was captured.** The upsert set every column, so an `import --merge` NULLed the ones the importer did not supply: reply and topic pointers, album grouping, migration markers and forward provenance. The importer also no longer asserts values it cannot know, so a re-import preserves the captured ones while a fresh import still gets correct defaults. ([#288](https://github.com/GeiserX/Telegram-Archive/pull/288))
- **Opening the chat list, the media gallery or a forum topic list stops scanning whole histories.** `/api/chats` no longer aggregates the entire messages table on every request. Chat and message pagination now order by a total key, so rows can no longer repeat or vanish between pages, and the second, unused media read per message page is gone. ([#288](https://github.com/GeiserX/Telegram-Archive/pull/288))
- **A failed login is always recorded.** Values are clamped to their column widths and NUL bytes replaced, so a crafted username can no longer make the audit insert fail silently on PostgreSQL. ([#288](https://github.com/GeiserX/Telegram-Archive/pull/288))
- **Typing in a search box no longer fires a full-chat scan on every keystroke.** The chat-header and message search boxes debounce and cancel superseded requests. Media-gallery, topic-list, statistics and pinned-message loads are all guarded against stale responses, so a slow answer that outlives the chat you were in no longer paints the previous chat's data — counts, pinned banner and all — into the new one. ([#289](https://github.com/GeiserX/Telegram-Archive/pull/289), [#298](https://github.com/GeiserX/Telegram-Archive/pull/298))
- **Clicking a notification always does something.** The service worker focuses an open tab when it can and otherwise opens the deep link, instead of relying on a message channel that may not be listening yet. ([#289](https://github.com/GeiserX/Telegram-Archive/pull/289))
- **The viewer no longer creates database tables underneath a running migration.** The viewer image ships no migrations and cannot run them, yet it built the schema on every SQLite start — and since compose starts both containers together, it could add tables while the backup container was migrating or inspecting the file, crash-looping what is usually the only copy of someone's Telegram history. The viewer now builds a schema only into a database with no tables at all. Fresh installs are unaffected. ([#294](https://github.com/GeiserX/Telegram-Archive/pull/294))
- **The two schema paths are now provably identical, and both databases are tested for real.** SQLite was built by the models and PostgreSQL only by migrations, so the two drifted with nothing able to notice: 54 differences on SQLite and 50 on PostgreSQL when first measured. One was a live bug — `media.file_path` was 500 characters on a real PostgreSQL install but unbounded in the models, so a long media path hard-failed there and worked on SQLite. Migration `021` aligns them: nullability on 35 columns, the declared defaults, that widening, a missing foreign key on reactions, eight SQLite integer widths, and an empty table left behind by an older migration. Every step reads the live schema first and does nothing where the shape already matches. ([#293](https://github.com/GeiserX/Telegram-Archive/pull/293))
- **Revoking access now closes the channels that were already open.** Logging out, deactivating or deleting a viewer, revoking a share token and session expiry used to stop future logins while the principal's open WebSocket kept receiving events and its push subscriptions kept firing notifications that carry sender names and message text — nothing ever deleted the subscription rows, and delivery trusted the grant snapshot taken at subscribe time. Every revoking path now closes the matching sockets, purges the principal's push subscriptions, and push delivery double-checks that a subscription's owner still exists before sending; logging out also unsubscribes the browser itself. A fully idle expired socket can persist up to one cleanup sweep — the deliberate price of keeping session checks off the message-delivery path. ([#306](https://github.com/GeiserX/Telegram-Archive/pull/306))
- **Migration `021` hardened before it ever shipped.** Databases provisioned by the models never gained the audit-log indexes, so the admin audit page scanned in full forever; the revoked-token backfill resurrected share tokens 7.x treated as dead, and now fails closed instead; and a crash immediately after a rebuild step left a temporary table behind that crash-looped every restart, which is now cleared defensively before each rebuild. ([#300](https://github.com/GeiserX/Telegram-Archive/pull/300))
- **A percent-encoded database URL, or a raw `%` in a PostgreSQL password, no longer crash-loops the backup container.** Dead configuration calls that choked on it are gone, and credentials are decoded before use. A separate crash loop is fixed alongside it: the container's SQLite path chain omitted `DATABASE_DIR`, so such an install was inspected at one path and migrated at another, and the initial migration then ran again against the real database. ([#285](https://github.com/GeiserX/Telegram-Archive/pull/285), [#293](https://github.com/GeiserX/Telegram-Archive/pull/293))
- **An archived attachment can no longer end up permanently unopenable.** Concurrent media ingest briefly published an intermediate name into the shared store, so a chat's symlink could be left pointing at a name that no longer existed; publishing is now atomic. The shared-media sharding migration moved relative symlinks a directory deeper without rewriting their targets, breaking every one it touched — targets are now rewritten as part of the move, and the migration is safe to re-run. A single unreadable file no longer aborts that migration, and with it the container's startup. ([#286](https://github.com/GeiserX/Telegram-Archive/pull/286))
- **A dead session fails immediately instead of burning the whole retry ladder**, a failed or cancelled live download no longer leaves an orphaned `.part` file behind, and the listener detaches its handlers when it stops, so a restart leaves one listener attached to the shared client rather than one more each time. ([#286](https://github.com/GeiserX/Telegram-Archive/pull/286))
- **More identifiers kept out of the logs.** An unhandled viewer error no longer writes the request path — which carries the chat id and the sender's filename — into the log, and the redaction sits inside the framework's own error handling, so the exception never reaches its unconditional traceback either. On the capture side, Telegram errors whose text spells out a chat or user id now log the error type only, closing at the sites that were missed the same leak class 7.33.2–7.33.4 addressed. Chat ids, media paths and message payloads are no longer written to the browser console. ([#286](https://github.com/GeiserX/Telegram-Archive/pull/286), [#289](https://github.com/GeiserX/Telegram-Archive/pull/289), [#290](https://github.com/GeiserX/Telegram-Archive/pull/290), [#297](https://github.com/GeiserX/Telegram-Archive/pull/297))

### Changed

- **The gates that publish images now test what they ship.** Both the merge gate and the publish gates run against a real PostgreSQL server, and the job fails if the PostgreSQL half of the suite skips — a silently skipped backend is how the schema drift above survived. Dependencies install from the lockfile, so the tests exercise the exact set the images ship rather than a fresh resolution. Third-party actions that can see the Docker Hub token are pinned to full commit hashes, code scanning now covers the viewer's JavaScript as well as the Python, and the publish path filters include the scripts and migrations that are baked into the image. ([#285](https://github.com/GeiserX/Telegram-Archive/pull/285), [#293](https://github.com/GeiserX/Telegram-Archive/pull/293), [#299](https://github.com/GeiserX/Telegram-Archive/pull/299))
- **Third-party assets in the viewer are pinned by content hash**, closing the CDN-compromise route into an authenticated viewer origin. ([#289](https://github.com/GeiserX/Telegram-Archive/pull/289))
- Documentation caught up with the code: the shipped compose file pinned an image 26 minor releases behind, the environment-variable table omitted variables that exist, and the example database URL pointed outside the mounted volume — where following it lost the archive when the container was recreated. ([#284](https://github.com/GeiserX/Telegram-Archive/pull/284))

## [7.33.6] - 2026-08-14

### Changed
- The README banner is now a single image built around the project logo, replacing the previous banner-plus-standalone-logo pair. Documentation only — nothing in the backup or viewer changed. ([#282](https://github.com/GeiserX/Telegram-Archive/pull/282))

## [7.33.5] - 2026-08-14

### Fixed
- **Media whose Telegram filename contains characters Windows rejects now downloads instead of failing five times and giving up.** A file name carrying a newline, a carriage return or any of the characters Windows reserves (`<`, `>`, `:`, `"`, `|`, `?`, `*`) could not be created there, so every attempt raised the same error and the item was retried to exhaustion on every run. Such names are now cleaned on every platform — not only on Windows — so a name computed on one machine matches the one computed on another and archives stay portable; trailing dots and spaces are stripped and reserved device names (`CON`, `NUL`, `COM1` and friends, including their superscript variants) are prefixed. Existing archives are unaffected: files already stored keep serving under their stored path. ([#280](https://github.com/GeiserX/Telegram-Archive/issues/280))

### Changed
- Dependency updates: `aiohttp` and `cryptography`.

## [7.33.4] - 2026-08-02

### Fixed
- **Error text no longer puts identifiers back into log lines that had just stopped naming them.** A filesystem error stringifies with the file it failed on, and a media path contains the chat-id folder, so any handler that logged the raw error reintroduced the identifier the previous two releases removed — including one line that shipped in 7.33.3 believing it was fixed. Filenames are now reduced to their basename where the parent folder is the identifier, so you can still see which file failed; avatar filenames are dropped outright, because there the identifier *is* the name. Error messages are kept wherever they cannot contain a path — a flood wait, an RPC reason — since that is where their diagnostic value is. Applied across the download, cleanup, thumbnail, deduplication and migration paths, plus the timeout and subprocess errors that print an entire command line. ([#277](https://github.com/GeiserX/Telegram-Archive/pull/277))

### Changed
- The check for this now fails on the whole class of mistake — any handler around a filesystem call that interpolates a raw error — rather than on the specific lines known to have leaked. It found seven further sites when it was introduced.

## [7.33.3] - 2026-08-02

### Fixed
- **Chat ids and chat titles are no longer written to the logs.** Deletion and import lines drop the id, the startup lines report how many chats are configured rather than which ones, and the viewer reports "N configured chat(s) not found" instead of naming each. The manually-run restore script deliberately keeps its ids: they are the operator's own arguments and the line is a confirmation before a destructive send. ([#274](https://github.com/GeiserX/Telegram-Archive/issues/274))

### Notes
- This stops new leakage only. Logs already written still contain chat ids and titles — rotate or delete them if that matters for your deployment.

## [7.33.2] - 2026-08-01

### Fixed
- **Your own account's first name, last name, username, phone number and Telegram id are no longer written to the logs.** Eleven places did so, all of them older than the rule that forbids it. The reason those lines existed — telling the operator that authentication succeeded and which account answered — is kept and improved: setup now compares the authenticated account against the configured phone number and reports whether they agree, instead of printing who it is. This covers the interactive setup flow too, since it logs through the same handler as the daemons and that stream may well be shipped off the machine.
- **Setup now fails when the stored session belongs to a different account.** It previously reported success in silence — a stale session from another account looked exactly like a clean login, and nothing further down the line checks identity, only authorization. A number written with `00` or with `+` is treated as the same number.
- A login credential that was printed to stdout, despite already being written to a private file, is no longer printed. ([#272](https://github.com/GeiserX/Telegram-Archive/issues/272))

### Changed
- A check now scans the whole source and script trees for any logging call reading an identifying attribute, rather than pinning the known lines, so this cannot quietly return.

## [7.33.1] - 2026-08-01

### Fixed
- **The filename in a voice or audio message is visible again.** The per-message download button added in 7.32.0 was being stretched to the full width of the bubble by a rule meant for inline media, which left its sibling text column at zero width — so the filename did not merely shift, it disappeared. The button is now 36×36 at every screen width, and the two links that are meant to span the bubble still do. ([#270](https://github.com/GeiserX/Telegram-Archive/issues/270))

## [7.33.0] - 2026-07-31

### Fixed
- **Audio auto-advance now plays through a chat of any size.** It previously stopped after roughly forty tracks in a large chat: the queue was collected by paging backwards from the newest item with a page cap, and in a big chat that walk never reached the track you were playing. The queue is now anchored on the playing track and extended in whichever direction playback is moving, so no page cap is needed. Views that are not a straight slice of the timeline — pinned-only, in-chat search, a topic pane, a jump window — seed the queue from the playing track rather than from a neighbouring row that is not really its neighbour. ([#266](https://github.com/GeiserX/Telegram-Archive/issues/266))
- **Audio bubbles are no longer stiltingly tall.** The duration and the playback status wrapped one character per line, because the bubble's text-wrapping rule let them collapse to a single glyph wide. ([#267](https://github.com/GeiserX/Telegram-Archive/issues/267))
- **A reply quote now shows who is being replied to** and what kind of media it was, rather than falling back to the word "Message" — which was every reply to a photo, voice note or file. Resolved for a whole page in one query, and degrading cleanly when the replied-to message is not in the archive. The pinned list shows the same. ([#268](https://github.com/GeiserX/Telegram-Archive/issues/268))
- **Media downloads recover from a long connection outage instead of failing until the next scheduled run.** After about a minute of failures Telethon marks itself disconnected and refuses every later request; only the scheduled job reconnected, so the listener could restart into the same error indefinitely. Connections now heal before the retry budget is spent, across every kind of call, and a chat is no longer abandoned on the first connection error while its messages are being read. ([#265](https://github.com/GeiserX/Telegram-Archive/issues/265))
- Connection logs no longer carry the account holder's name or phone number.

## [7.32.0] - 2026-07-31

### Added
- **Every media message has its own download button**, and downloads arrive under the file's original name rather than the internal storage name. This also repairs the download button in the media gallery. ([#261](https://github.com/GeiserX/Telegram-Archive/issues/261))

### Fixed
- **The audio queue can no longer skip across a hole in the timeline.** A page of tracks that could not be tied back to the one playing is now left unused instead of being merged, and media pages order correctly when several items share the same timestamp (they previously sorted as text: 9, 99, 8, 89, 80, 7). Jumping from the playbar to a track outside the loaded window now loads the messages around it. ([#257](https://github.com/GeiserX/Telegram-Archive/issues/257))
- **Media whose filename contains `#` or `?` loads instead of returning "not found."** URLs are now percent-encoded per path segment on both the client and the server. ([#258](https://github.com/GeiserX/Telegram-Archive/issues/258))
- **Service messages captured before 7.28.0 show their text again.** They were stored with empty text and are now rendered at display time from what was captured. "added" and "removed" messages deliberately say "Someone" rather than naming anyone: the affected person was never stored, and the sender is the admin who acted, so naming them would state the wrong person. ([#259](https://github.com/GeiserX/Telegram-Archive/issues/259))
- **The person named in a service message opens the sender popup**, for the actions where that person is the subject of the sentence. ([#260](https://github.com/GeiserX/Telegram-Archive/issues/260))
- **The playbar shows the message's date converted to your configured viewer timezone**, so it can no longer report the wrong calendar day near midnight. ([#262](https://github.com/GeiserX/Telegram-Archive/issues/262))
- **Voice notes captured by the real-time listener now carry their duration**, along with file size, type and dimensions, exactly like ones picked up by a scheduled backup — they previously rendered without a duration. Rows captured before this keep their empty duration until they are captured again. ([#263](https://github.com/GeiserX/Telegram-Archive/issues/263))
- **A media file that goes missing is retried again.** A failed re-download used to leave the row marked as downloaded with a path pointing at nothing, so the every-run retry never saw it and the file was gone for good.

## [7.31.2] - 2026-07-30

### Fixed
- **A failed request while loading more audio is no longer treated as "you have reached the oldest clip."** Pressing "previous" at the head of the queue silently restarted the current track when the request failed — a rate limit, an expired session, a server error — with nothing shown. The failure is now surfaced, the button keeps working so a later press can succeed, and only genuinely running out of older clips restarts the track. ([#254](https://github.com/GeiserX/Telegram-Archive/issues/254))

## [7.31.1] - 2026-07-29

### Added
- **Audio auto-advance continues past the messages currently loaded on screen.** The player pages the chat's voice and music history on its own — separately from the message list, so the two cannot interfere — and "previous" at the top of the queue fetches an older page on demand. Voice notes and music stay in separate queues. ([#254](https://github.com/GeiserX/Telegram-Archive/issues/254))

### Fixed
- Closing the player while it was fetching more of the queue no longer disables "previous" for the rest of the session.

## [7.31.0] - 2026-07-29

### Added
- **One global voice and audio player, replacing the separate player in every message bubble.** Starting a clip now stops the one before it, playback keeps going when you scroll away, and a persistent playbar gives you play/pause, previous/next, seeking, elapsed and total time, playback speed, the sender, chat and timestamp, and a close button. Clicking the playbar's details takes you to the source message. ([#250](https://github.com/GeiserX/Telegram-Archive/issues/250))
- **Playback continues automatically to the next clip.** Voice notes and music are separate queues, matching the official clients, so a run of voice messages never spills into a music file.
- **Playback speed (0.5×, 1×, 1.5×, 2×) is remembered per media type across sessions** and survives a change of track.
- Operating-system and lock-screen media keys work where the browser supports them.

### Notes
- Auto-advance is off for viewer accounts that are not permitted to download media, and stops after repeated load failures.
- Auto-advance does not load more messages into the list — that is left to the existing scroll behaviour — and the queue does not span chats.

## [7.30.0] - 2026-07-29

### Added
- **Sender details show the sender's photo at a readable size**, with the usual initials-on-gradient fallback when there is no photo.
- **The chat header avatar opens sender details in one-to-one chats.** Deliberately limited to private chats: in a group the header photo belongs to the group, not to a person. ([#240](https://github.com/GeiserX/Telegram-Archive/issues/240))

## [7.29.2] - 2026-07-29

### Fixed
- **The floating date while you scroll a chat shows the day you are actually looking at.** Every day's separator pinned itself to the top of the message list at the same time, so several dates stacked in one place and whichever painted last won — routinely a day unrelated to what was on screen. There is now a single date indicator above the message list that follows the topmost visible message, stays correct after an older page loads, reads the oldest loaded day when you are at the very beginning of a chat's history, and still opens the date picker when clicked. ([#249](https://github.com/GeiserX/Telegram-Archive/issues/249))

## [7.29.1] - 2026-07-28

### Fixed
- **Invalid or permanently unavailable peer identifiers no longer consume transient retry delays.** Terminal Telegram peer errors now fail immediately, allowing whitelist-based backup sweeps to skip stale entries and continue without repeated exponential waits.

## [7.29.0] - 2026-07-28

### Added
- **Historical jump windows now paginate in both directions.** Scrolling toward newer messages repeatedly loads forward pages and automatically returns to live updates when the current tail is reached. Topic scope, stale requests, and transient failures remain isolated.
- **Jump to Date now marks days containing archived messages.** Month availability is timezone- and topic-aware, uses the existing message-date index on SQLite and PostgreSQL, and leaves empty days selectable.

### Fixed
- **The Jump to Date month selector is readable in dark mode** on native browser dropdowns, including Windows Chromium.
- **Calendar navigation is accessible and race-safe.** The dialog supports keyboard focus and Escape, announces loading/errors/nearest-date jumps, works on short mobile viewports, and ignores cancelled or superseded date requests.

### Notes
- No database migration or configuration change is required.

## [7.28.0] - 2026-07-27

### Added
- **Historical sender names are preserved per message.** New imports and captures store the sender label seen with the message instead of resolving every old message through a mutable user profile. Existing rows keep their current behavior, and a stored snapshot is filled once but never rewritten by later sweeps.
- **Sender details are available from group avatars.** The dialog shows the archived name, a different latest-known profile name when available, and the numeric Telegram ID.

### Fixed
- **Telegram Desktop media imports are confined to the selected export and configured media directories.** Absolute paths, parent traversal, and escaping symlinks are rejected before files are read or written.
- **Imports now honor `MEDIA_MAX_FILENAME_BYTES`** for long and multibyte media names, and one failed media copy is skipped without aborting the remaining message history.
- **Imported supergroups show sender names and avatars** like live-captured groups.
- **Message runs have compact spacing within one sender's sequence and a larger gap when the sender changes.**

### Upgrade note
- Migration `020` adds the nullable `messages.sender_name` snapshot column automatically. Existing messages are not backfilled.

## [7.23.0] - 2026-07-18

### Added
- **Real-time reaction capture (`LISTEN_REACTIONS`, opt-in, default off).** With the listener enabled, reactions are now reconciled the moment they change instead of waiting for the next scheduled backup (previously up to the whole `SCHEDULE` interval behind). Reactions are stored as per-emoji aggregate counts; a removed reaction is retained (tombstoned) rather than silently dropped. Capture is best-effort by design — Telegram gives a user client no delivery guarantee for reaction updates, so the scheduled backup remains the reconciliation backstop. ([#219](https://github.com/GeiserX/Telegram-Archive/issues/219))
- `REACTION_DEBOUNCE_SECONDS` (default **1.5**) — coalesces a burst of reaction updates on the same message into a single write and broadcast, so a popular message can't thrash the database.

### Fixed
- **Reactions no longer lag the scheduled backup, and an add-then-remove between sweeps is preserved** instead of vanishing without a trace. ([#219](https://github.com/GeiserX/Telegram-Archive/issues/219))
- **A message whose edit timestamp changed only because of a reaction no longer shows a phantom "edited" marker.** Telegram bumps a message's edit date server-side when reactions change; the archive now advances its stored edit date only on a real text change (fixed for both the real-time listener and the scheduled/backfill paths), so "edited" reflects genuine edits and real edits are never hidden.
- **Reaction removals down to zero are now persisted**, and a reaction's first-seen timestamp is preserved across re-scans (the scheduled backup previously reset it every run and never cleared reactions back to empty).

### Changed
- Reaction storage moved to per-emoji aggregate counts with a retain-on-removal tombstone; per-user reaction attribution is intentionally not persisted (a user client only sees a small, unreliable preview of who reacted, so it cannot be tracked accurately). Broadcast-channel reactions are aggregate-only by Telegram policy.

### Upgrade note
- Migration `018` (adds `reactions.removed_at` and a `(chat_id, message_id)` index) runs automatically on first start; it is idempotent and requires no action.

## [7.22.0] - 2026-07-15

### Fixed
- **Jumping to a message (from the media gallery, a reply, or the calendar) now actually lands on the target instead of the latest messages.** The window request was silently ignored by the backend and returned the newest page; the target row also had no scroll anchor, so even a correct window wasn't scrolled to. The window is now fetched correctly (with context on both sides of the target), the target is anchored and highlighted, the realtime poll and the WebSocket live-update path are both paused while viewing a detached window, and calendar date-jumps use the same path (removing an older gap-fill loop that failed for dates far back). ([#213](https://github.com/GeiserX/Telegram-Archive/issues/213))
- **Anonymous access is now read-only.** When the viewer is exposed without authentication, it no longer grants administrative capabilities (creating viewers, minting share tokens, reading the audit log, changing settings) — those require the master account.
- **Push subscriptions can no longer point the server at internal addresses.** Subscription endpoints are validated by resolving the host and rejecting private/loopback/link-local addresses.
- **Media captured by the scheduled backup is now timestamped in UTC** like the realtime and import paths (it was previously written in the host's local time).
- **Realtime updates survive database blips.** The Postgres notification listener no longer leaks a connection on each reconnect and is retried correctly after a failed restart.
- Database-unavailable conditions (including "database is locked") now return 503 instead of 500, and chat-search wildcard characters are handled literally.

### Added
- Message-list API accepts `after_id` (newer-than cursor) and honors a lone `before_id` (older-than cursor) for jump-to-message windows.

### Changed
- **Loading a page of messages is dramatically cheaper** — reactions and reply previews are fetched in two batched queries per page instead of one-per-message (previously up to ~100 queries for a 50-message page, on an endpoint the viewer polls every few seconds). Opening a chat's statistics is cached briefly. Logs no longer include chat, topic, or message identifiers or message content.

### Upgrade note
- Migration `017` (adds an index for jump-to-message lookups) runs automatically on first start; it is idempotent and requires no action.

## [7.21.0] - 2026-07-14

### Fixed
- **Jumping to a message from the media gallery no longer snaps back to the latest messages** — clicking a media item loaded the correct history window and then the 3-second refresh immediately reset the view to the newest messages, so the target was never shown. The realtime refresh is now paused while you're viewing a jumped-to history window, and the "scroll to latest" button reliably returns you to live. ([#213](https://github.com/GeiserX/Telegram-Archive/issues/213), [#214](https://github.com/GeiserX/Telegram-Archive/pull/214))
- **Media is no longer silently lost on Synology encrypted (eCryptfs) shares** — such shares cap a filename at ~143 bytes (not the usual 255), so media with long original names — especially non-Latin (Cyrillic/CJK) — failed to write and was re-fetched from Telegram on every run forever. Filenames are now length-budgeted (byte-aware, extension preserved, non-Latin safe) to fit; the unique file-id prefix and extension are always kept, only the decorative part is shortened. ([#212](https://github.com/GeiserX/Telegram-Archive/issues/212), [#215](https://github.com/GeiserX/Telegram-Archive/pull/215))
- **A permanently-unwritable media file no longer taxes every backup run forever** — failed downloads are now retried a bounded number of times and then skipped with a logged count, instead of being re-fetched indefinitely.

### Added
- `MEDIA_MAX_FILENAME_BYTES` (default **143**) — usable filename-component byte budget for the media store. Raise to 255 on plain ext4/xfs/btrfs for longer decorative names; keep at 143 for Synology/eCryptfs encrypted shares.
- `MEDIA_MAX_DOWNLOAD_ATTEMPTS` (default **5**) — how many times a failing media download is retried before it is skipped (and surfaced as an aggregate count). Requesting a re-download resets the counter.

### Upgrade note
- Migration `016` (adds `media.download_attempts`) runs automatically on first start; it is idempotent and requires no action.

### Credits
- Thanks to [@625801](https://github.com/625801) for the detailed reports and reproductions behind #212 and #213.

## [7.20.0] - 2026-07-06

### Added
- **Chat folders back up their full membership** — Telegram folders can be defined by pinned chats and by category rules (all groups, all channels, all bots, contacts / non-contacts), not just an explicit chat list. The backup previously only recorded a folder's explicitly-included chats, so a folder built from pins or category rules ended up with no members — and after the 7.19.1 empty-folder fix, such folders disappeared from the viewer entirely. The backup now resolves each folder's effective membership against the chats you've archived: pinned + included chats (minus excluded ones) plus the category rules, so those folders show their archived chats again. Saved Messages (pinned or via the contacts rule) resolves correctly too. ([#208](https://github.com/GeiserX/Telegram-Archive/issues/208), [#210](https://github.com/GeiserX/Telegram-Archive/pull/210))

### Notes
- Folder membership matches Telegram's own precedence (explicit pins/includes always win; excludes and category rules follow). The "unread only" / "unmuted only" refinements some folders use depend on live notification state the archive doesn't store, so they aren't reconstructed — such a folder shows all chats of its category rather than being hidden. No configuration or migration is required.

## [7.19.1] - 2026-07-06

### Fixed
- **Empty folder tabs no longer clutter the viewer** — When you back up only a subset of your chats, the viewer's folder bar previously listed *every* Telegram folder, including folders none of whose chats were archived — clicking them showed nothing. Folders now appear only when they contain at least one backed-up chat, so the viewer reflects what was actually crawled rather than your full Telegram account. ([#208](https://github.com/GeiserX/Telegram-Archive/issues/208), [#209](https://github.com/GeiserX/Telegram-Archive/pull/209))

### Credits
- Thanks to [@sube32](https://github.com/sube32) for reporting the empty-folder display and the clear worked example that pinned down the intended behavior.

## [7.19.0] - 2026-07-04

### Added
- **Unseen-message badge** — When new messages arrive while you're scrolled up reading history, the jump-to-latest button now appears with a count badge (screen-reader labelled) instead of messages arriving silently off-screen. The count clears when you return near the bottom, jump manually, or switch views. ([#207](https://github.com/GeiserX/Telegram-Archive/pull/207))
- **Instant realtime rendering** — WebSocket-delivered messages now carry the sender's name and the downloaded media info, so they render fully immediately instead of appearing as a bare text bubble for up to a poll interval.
- **Gallery remembers your place** — Closing the shared-media gallery returns you to the exact scroll position and keyboard focus you had, instead of dropping you at the newest message with focus reset.

### Fixed
- **Realtime display stability** — Message ordering now matches the server contract exactly (`date` then `id`, parsed as UTC), fixing same-second messages rendering out of order and a latent local-timezone sort inconsistency near midnight/DST; realtime and polled rows are deduplicated by a type-safe key; WebSocket rows are filtered to the forum topic being viewed; and auto-scroll on arrival only happens when you're already near the bottom — with the full re-snap cascade for lazily-loading media. ([#207](https://github.com/GeiserX/Telegram-Archive/pull/207))
- **"Load older" pagination can no longer be corrupted** — The history cursor is now advanced only by real history page loads (monotonically older), so realtime arrivals, polling, jump-to-message windows, and media-gallery visits no longer break infinite scroll; the scroll observer is reconnected after the gallery unmounts and after jump-to-message rebuilds the list.
- **Jumping into old history no longer snaps back** — The poll's deletion detection is bounded to the window the server actually returned, fixing the jumped-to view being wiped (and the user yanked to the latest messages) within seconds, plus older loaded rows being falsely removed whenever a burst of new messages arrived.
- **Cross-view race conditions** — Fast chat/topic switching, typing in search, and jump-to-message can no longer splice another view's rows into the current one, wedge pagination for the session, or stall search with a stuck loading gate; all fetch paths now re-validate the active view after every await.
- **Expired sessions surface the login screen** — A 401 during scrolling or background polling now flips the viewer to the login page instead of silently retry-looping forever; repeated pagination failures pause infinite scroll with a console note instead of hammering a failing endpoint.
- **Idle efficiency** — Background polling on a quiet chat no longer re-sorts and re-renders the loaded history every 3 seconds: per-row sort keys are parsed once and cached, and poll merges write in place only when a field actually changed (measured: ~60ms of main-thread work per tick at 5,000 loaded messages down to ~0).

### Credits
- Thanks to [@charys117](https://github.com/charys117) for diagnosing and fixing the realtime display, history-cursor, and gallery-observer bugs in [#207](https://github.com/GeiserX/Telegram-Archive/pull/207) — the third quality contribution in a row, including fork-side image build validation before submission.

## [7.18.1] - 2026-07-02

### Fixed
- Re-release of 7.18.0: its Docker images were never published due to a release-packaging error (a stripped trailing newline failed the lint gate in the image builds). No code changes — 7.18.1 is the first shipped build of the 7.18.0 feature set below.

## [7.18.0] - 2026-07-02

### Added
- **Message edit history** — Edited messages now preserve their previous text versions in a new `message_versions` table (Alembic migration `015`). The viewer shows a clickable `edited(n)` marker that opens a lazy-loaded "Versions" drawer (Escape to close, screen-reader friendly), message lists include a `version_count` field, and a new authenticated endpoint `GET /api/chats/{chat_id}/messages/{message_id}/versions` serves the retained versions (newest first, up to 500). Version capture is idempotent (content-addressed by a change hash), race-safe, and best-effort — a history write can never break the archival of the message itself. Soft-deleted messages keep their history; hard deletes remove it. ([#206](https://github.com/GeiserX/Telegram-Archive/pull/206))
- Chat exports (`GET /api/chats/{chat_id}/export` and the `export` CLI) now include the preserved `message_versions`, streamed entry-by-entry so large edit histories don't inflate memory.

### Changed
- **⚠️ Breaking Change**: the `GET /api/chats/{chat_id}/export` response is now a JSON **object** — `{"chat": {id, type, title, username}, "messages": [...], "message_versions": [...]}` — instead of a top-level array of messages. Consumers that parsed the old array should read the `messages` key. The in-app "Download" button is unaffected.
- Message re-scans (backup, gap-fill, import) only replace archived text when the incoming copy carries an equal-or-newer `edit_date`, so older imports can no longer clobber newer edits; fully unchanged messages are skipped without any database write, keeping full re-backups fast on large archives.
- The Docker Publish workflow also rebuilds the backup image when `scripts/`, `pyproject.toml`, or `uv.lock` change, so entrypoint/migration-stamping changes always ship.

### Fixed
- Deletion/edit sync no longer treats every previously-edited message as changed on every pass (a timezone-aware vs naive comparison bug), removing spurious update round-trips.
- Re-backups no longer reset a message's pinned flag when the source data doesn't include pinning info.
- Real-time edit broadcasts fire only when the archive actually accepted the edit, so the viewer can't briefly display text the archive rejected as stale, and listener statistics distinguish applied edits from no-ops.

### Upgrade / rollback note
- Migration `015` runs automatically on first start. **Rolling back** to a ≤7.17.2 image after that requires stamping the database back first (`alembic downgrade 014` inside the running 7.18.0 container) — an older image does not know revision `015` and will refuse to start.

### Credits
- Thanks to [@charys117](https://github.com/charys117) for designing and contributing message edit-history preservation in [#206](https://github.com/GeiserX/Telegram-Archive/pull/206) — including the migration hygiene, the lazy viewer drawer, and pre-deployment validation on a live instance.

## [7.17.2] - 2026-06-28

### Fixed
- **Media downloads recover from transient "location unavailable" errors** — Telegram sometimes reports a media file's storage location as temporarily unavailable (`LOCATION_NOT_AVAILABLE`, or `LOCATION_INVALID`, on `upload.GetFile`). The backup now re-fetches the message for a fresh file reference/location and retries with exponential backoff; if the file is still unavailable after a few attempts, the item is left for the next scheduled run rather than failing outright. Telethon surfaces these as a generic `BadRequestError` (the code is preserved on the exception), so they previously fell through to a plain retry that could not recover a stale reference. The message-refresh call and per-download timeout were also hardened so they can no longer cancel a Telegram rate-limit (FloodWait) wait. ([#203](https://github.com/GeiserX/Telegram-Archive/pull/203), [#204](https://github.com/GeiserX/Telegram-Archive/issues/204))

### Credits
- Thanks to [@charys117](https://github.com/charys117) for reporting the production `LOCATION_NOT_AVAILABLE` failures — with detailed Telethon error-mapping evidence — and contributing the fix in [#203](https://github.com/GeiserX/Telegram-Archive/pull/203).

## [7.17.1] - 2026-06-25

### Fixed
- **Viewer image startup crash** — `Dockerfile.viewer` now bundles `src/message_utils.py`, which the viewer imports transitively via `src/db/adapter.py`. Without it the standalone viewer container failed to start with `ModuleNotFoundError: No module named 'src.message_utils'` (affected v7.16.0–v7.17.0). ([#201](https://github.com/GeiserX/Telegram-Archive/pull/201))

### Changed
- The viewer image now also rebuilds when `pyproject.toml`/`uv.lock`, `src/__init__.py`, `src/realtime.py`, or `src/message_utils.py` change, and the "Docker Publish Dev" workflow skips its build-and-push job for fork/Dependabot PRs (only same-repo PRs and manual runs publish the dev image).

### Credits
- Thanks to [@charys117](https://github.com/charys117) for the viewer-image hotfix in [#201](https://github.com/GeiserX/Telegram-Archive/pull/201).

## [7.17.0] - 2026-06-25

### Added
- **Forum topics appear immediately** — Topic lists for forum/Topics-mode groups are now fetched up front, at the start of each chat's backup, instead of only at the end of a full run. Large, media-heavy forums no longer show "0 topics" while their media is still downloading; topics show up within seconds and message counts fill in as the backup progresses. ([#200](https://github.com/GeiserX/Telegram-Archive/issues/200))
- **"Backup in progress" indicator** — The Backup Statistics panel shows a live indicator while a backup run is active, so it's clear when the figures are still being collected rather than final.
- **"View all messages" for forums without topics** — When a forum group has no topics recorded yet, the viewer now offers a direct link to browse the chat's messages instead of a dead-end "No topics found".

### Changed
- **Storage statistic reflects actual disk usage** — The "Storage" figure is now measured from on-disk files (`du`-style, counting deduplicated `_shared` blobs once) rather than summing recorded media sizes, so it matches real disk consumption. Sizes are labeled with correct binary units (GiB/MiB/TiB).
- Forum-topic fetching now paginates beyond 100 topics and retries on FloodWait, so large forums are captured fully.

### Fixed
- Forum/Topics-mode groups showing "0 topics" in the web viewer during long, media-heavy backups. ([#200](https://github.com/GeiserX/Telegram-Archive/issues/200))

### Credits
- Thanks to [@1235789gzy1](https://github.com/1235789gzy1) for reporting the forum-topic display and storage-statistic issues in [#200](https://github.com/GeiserX/Telegram-Archive/issues/200).

## [7.16.0] - 2026-06-25

### Added
- **Soft deletion mode** — New `DELETION_MODE` (`hard` | `soft`, default `hard`) controls how Telegram deletions are handled, both for the real-time listener (`LISTEN_DELETIONS=true`) and the batch sync (`SYNC_DELETIONS_EDITS=true`). `hard` keeps the legacy behavior (remove the archived message); `soft` keeps the original message and marks it deleted, showing a `deleted` label in the viewer (and `edited deleted` for an edited-then-deleted message). Soft-deleted messages are retained in the archive — they stay counted in statistics and remain searchable. Adds `messages.is_deleted` / `messages.deleted_at` via Alembic migration `014`; reprocessing, gap-fill, and retries preserve the soft-delete marker, and the deletion write retries on a locked database. ([#199](https://github.com/GeiserX/Telegram-Archive/pull/199))

### Credits
- Thanks to [@charys117](https://github.com/charys117) for contributing soft-deletion mode in [#199](https://github.com/GeiserX/Telegram-Archive/pull/199).

## [7.13.0] - 2026-06-04

### Added
- **Per-file parallel chunked downloads** (opt-in, default OFF) — Large files can now be split into chunks fetched concurrently over several connections to the file's datacenter and reassembled on disk, lifting the ~10 MB/s single-stream throughput cap on fast links. Controlled by `PARALLEL_DOWNLOAD_ENABLED` (default `false`), `PARALLEL_DOWNLOAD_MIN_SIZE_MB` (default `20`), `PARALLEL_DOWNLOAD_CONNECTIONS` (clamped 2–8, default `4`), and `PARALLEL_DOWNLOAD_PART_SIZE_KB` (one of 4/8/16/32/64/128/256/512, default `512`). Photos and files below the size threshold always stay single-stream. Each chunk is written at its exact offset with full coverage verified before finalize; any chunk failure cancels the rest, removes the partial file, and falls back transparently to a single stream. FloodWait flows through the existing retry budget, and peak extra memory is bounded at roughly `CONNECTIONS × PART_SIZE_KB`. Applies to the scheduled backup path only. ([#183](https://github.com/GeiserX/Telegram-Archive/issues/183))

### Credits
- Thanks to [@smbdspk](https://github.com/smbdspk) for proposing per-file parallel downloads in [#183](https://github.com/GeiserX/Telegram-Archive/issues/183).

## [7.12.0] - 2026-06-02

### Added
- **Configurable download timeout** — Media downloads are now wrapped in `asyncio.wait_for` with a `DOWNLOAD_TIMEOUT_SECONDS` budget (default `3600`, `0` disables), so a single stalled download can no longer hang a backup indefinitely.
- **Tunable backoff for transient errors** — `BACKOFF_MIN_SECONDS` and `BACKOFF_MAX_SECONDS` control exponential backoff with jitter for FloodWait and transient network retries, and `FLOOD_WAIT_LOG_THRESHOLD` tunes how chatty FloodWait logging is.

### Fixed
- **Transient network errors no longer abort one-shot API calls** — `call_with_flood_retry` now retries `TimeoutError`/`ConnectionError`/`OSError`/`RPCError` with bounded exponential backoff, while still re-raising terminal errors (FloodWait, FileReferenceExpired, ChannelPrivate, ChatForbidden, UserBanned) immediately.
- **Expired file references are refreshed mid-download** — Downloads that hit `FileReferenceExpiredError` now re-fetch the message and retry instead of failing the media.
- **Concurrent symlink creation is race-safe** — Deduplicated media symlinks tolerate `EEXIST` from concurrent tasks instead of crashing.
- **`upsert_user` and `insert_media` retry on locked DB** — Both now use `@retry_on_locked()` for resilience under concurrent SQLite access.
- **Windows-friendly auth help** — `setup_auth` no longer calls `os.getuid()`/`os.getgid()` unconditionally.

### Credits
- Thanks to [@smbdspk](https://github.com/smbdspk) for the download-resilience work in [#180](https://github.com/GeiserX/Telegram-Archive/pull/180).

## [7.10.10] - 2026-05-24

### Fixed
- **Viewer login page renders again** — Fixed a Vue setup-time crash from the media gallery code that initialized `showMediaGallery` after a watcher referenced it. The crash mounted the app as an empty page before the user/password login form could render.

## [7.10.0] - 2026-05-23

### Added
- **Media Gallery**: Dedicated per-chat media page with grid view for photos/videos, list view for voice messages and files
- **Media API**: New endpoints `GET /api/chats/{id}/media` and `GET /api/chats/{id}/media/counts` for paginated media browsing
- **Thumbnail pre-generation**: Thumbnails are now generated during backup for instant gallery loading
- **Thumbnail concurrency limit**: Semaphore prevents memory exhaustion when loading large grids
- **Database index**: New composite index `idx_media_chat_type(chat_id, type)` for efficient media type filtering

## [7.7.0] - 2026-04-29

### Security

- **Viewer now fails closed when credentials are missing** — If `VIEWER_USERNAME`/`VIEWER_PASSWORD` are not configured, the HTTP API and WebSocket endpoint reject access unless `ALLOW_ANONYMOUS_VIEWER=true` is explicitly set.
- **Restricted media access is enforced consistently** — Media, thumbnails, avatars, and non-chat folders now share centralized chat ACL checks, preventing restricted users from reading `_shared` files or unrelated chat media.
- **No-download users can no longer fetch original or thumbnail bytes** — Accounts and share tokens with `no_download=true` receive metadata only; direct original media and generated thumbnail URLs return 403, while UI avatars remain available.
- **Internal push events require a secret off-loopback** — `/internal/push` requires `INTERNAL_PUSH_SECRET` for non-loopback/private-network callers, reducing spoofing risk between co-located containers.
- **WebSocket upgrades validate origin** — Cross-origin WebSocket connections must be same-origin or explicitly allowed by `CORS_ORIGINS`.
- **Non-interactive auth hash files are owner-only** — Persisted `phone_code_hash` sidecar files are now created with `0600` permissions.

### Fixed

- **Scheduled backups no longer overlap** — The scheduler uses a backup lock so initial and cron-triggered jobs cannot run concurrently.
- **FloodWait handling is explicit and bounded** — One-shot Telegram API calls now retry through shared helpers and abort instead of sleeping when Telegram asks for waits above `MAX_FLOOD_WAIT_SECONDS`.
- **FloodWait env parsing is resilient** — Invalid `MAX_FLOOD_RETRIES` and `MAX_FLOOD_WAIT_SECONDS` values fall back to safe defaults instead of crashing imports.
- **Media downloads finalize atomically** — Temporary `.part` files are moved into place only when an actual file exists, preserving Telethon-selected extensions and avoiding bogus stored paths.
- **Telegram contact, geo, and poll media are metadata-only** — These message types no longer trigger file download attempts.
- **Database URL precedence is consistent** — Entrypoint migrations and realtime notifier/listener mode detection now honor `DATABASE_URL` before `DB_TYPE`, including `postgres://`, `postgresql://`, `postgresql+asyncpg://`, and SQLite URLs.
- **Database migration coverage includes app-state tables** — SQLite-to-PostgreSQL migration now includes viewer accounts, sessions, tokens, folders, forum topics, push subscriptions, and settings.
- **Share token URLs avoid query-string leakage** — Generated links use `#token=` fragments and preserve subpath deployments.

### Changed

- **Deletion listening is safer by default** — `LISTEN_DELETIONS` now defaults to `false` so archives do not mirror Telegram deletions unless explicitly configured.
- **Docker examples pin the 7.7.0 release** — Compose and README snippets now reference `drumsergio/telegram-archive:7.7.0` and `drumsergio/telegram-archive-viewer:7.7.0`.
- **Viewer compose binds to localhost by default** — The example viewer service binds `127.0.0.1:8000:8000` and documents reverse-proxy/auth requirements before public exposure.
- **CI and release checks are stricter** — Docker publish workflows run ruff and pytest before publishing, shellcheck tracks `main`, Docker Hub description sync covers both images, and release checks match the documented local test command.

### Documentation

- **Viewer authentication setup is documented** — README and `.env.example` now show required viewer credentials and the explicit anonymous opt-in.
- **Chat include filters are documented as allow-lists** — Examples now correctly show `CHAT_TYPES=groups,channels` when including one specific channel alongside groups.
- **Operational safety docs were refreshed** — README and `.env.example` now describe deletion mirroring, flood-wait controls, proxy header trust, and internal push secrets.

### Tests

- Added regression coverage for fail-closed viewer auth, no-download media restrictions, thumbnail ACLs, WebSocket subscription filtering, internal push auth, scheduler locking, flood-wait aborts, atomic downloads, `DATABASE_URL` behavior, non-interactive auth hash reuse, and migration model enumeration.

## [7.6.4] - 2026-04-25

### Fixed

- **Improved General topic test suite** — Renamed unprofessional test data, removed redundant `@pytest.mark.asyncio` decorators (project uses `asyncio_mode = "auto"`), converted setup to a proper pytest fixture, and added edge case tests for nonexistent topics, `topic_id=0`, and topic+search filter interaction. Contributed by @tondeaf in #122 (follow-up).

## [7.6.3] - 2026-04-25

### Fixed

- **Edit notifications no longer silently dropped on long messages** — The 500-char truncation guard only protected `data["message"]["text"]` (new_message path), leaving `data["new_text"]` (edit path) unprotected. A 4096-char emoji edit could produce a 16KB payload exceeding PostgreSQL's 8KB NOTIFY limit, causing a silent `pg_notify` error. Both paths are now truncated via a shared `_truncate_notify_data()` helper. (#123 follow-up)
- **Use `pg_notify()` with bound parameters for PostgreSQL NOTIFY** — Replaces f-string SQL interpolation that was vulnerable to asyncpg `$N` placeholder parsing and fragile manual single-quote escaping. Contributed by @tondeaf in #123.
- **Push secret comparison is now timing-safe** — `/internal/push` endpoint used `!=` for bearer token comparison; switched to `secrets.compare_digest()` consistent with the rest of the auth layer.
- **Test assertions use stable `TextClause.text` attribute** — Replaced `str(stmt)` with `stmt.text` for SQLAlchemy SQL assertions, avoiding reliance on undocumented `__str__` behavior.

## [7.6.2] - 2026-04-25

### Fixed

- **FloodWaitError no longer crashes `get_dialogs()` or `get_me()`** — PR #124 set `flood_sleep_threshold=0` globally but only wrapped 2 of ~20 API call sites. The unwrapped `get_dialogs()` and `get_me()` calls could crash the entire backup or prevent startup. Both are now wrapped with bounded flood-wait retry logic.
- **Negative `e.seconds` from Telegram no longer causes zero-delay retry storms** — Sleep duration is now clamped to `max(0, ...)` on both the iterator wrapper and the new one-shot retry helper.
- **Invalid `FLOOD_WAIT_LOG_THRESHOLD` env var no longer crashes mid-backup** — Bare `int()` parsing replaced with defensive `try/except` that falls back to the default of 10 seconds.
- **`iter_messages_with_flood_retry` now rejects `reverse=False`** — The resume tracking (`max(resume_from, msg.id)`) is only correct for ascending iteration. A `ValueError` is now raised if `reverse=True` is not passed, preventing silent data corruption from future misuse.
- **Documented `FLOOD_WAIT_LOG_THRESHOLD`** — Added to `.env.example` alongside the other logging variables.

## [7.6.1] - 2026-04-19

### Fixed

- **Forwarded media from private channels no longer creates broken placeholders** — When a message forwarded from a private channel contains a document with an inaccessible file reference (`media.document=None`), `_get_media_type()` now correctly returns `None` instead of `"document"`. Previously this caused a broken `telegram_file_id` of `"None"`, a failed download attempt, and a misleading "Will download on next backup" placeholder that would never resolve. Applies to both scheduled backup and real-time listener (#125)

## [7.6.0] - 2026-04-18

### Added

- **Topic filtering for forum supergroups** — New `SKIP_TOPIC_IDS` environment variable to exclude specific topics from backup while keeping the rest of the chat. Format: `chat_id:topic_id,...`. Works in both scheduled backup and real-time listener flows (#117)

### Fixed

- **Dangling dedup symlinks no longer cause infinite redownload loops** — When `DEDUPLICATE_MEDIA` is enabled and `VERIFY_MEDIA` runs, dangling symlinks (where the target was renamed by Telethon) are now detected via `os.path.lexists()` instead of `os.path.exists()`, which follows symlinks. The download return value is now captured to use the actual on-disk filename for symlink targets. Stale symlinks are removed before recreation to prevent `Errno 17` (file exists) errors. Applies to both scheduled backup and real-time listener (#115)

## [7.5.0] - 2026-04-13

### Added

- **SOCKS5 proxy support** — Route all Telegram connections through a SOCKS5 proxy, useful in regions where Telegram is blocked or behind corporate firewalls. New env vars: `TELEGRAM_PROXY_TYPE`, `TELEGRAM_PROXY_ADDR`, `TELEGRAM_PROXY_PORT`, `TELEGRAM_PROXY_USERNAME`, `TELEGRAM_PROXY_PASSWORD`, `TELEGRAM_PROXY_RDNS` (#104)
- **Validation hardening** — Port range (1-65535), username/password pairing, boolean RDNS parsing, and case-insensitive proxy type
- **Dependency** — Added `python-socks[asyncio]>=2.7.1` (required by Telethon for SOCKS5 transport)

### Security

- **Proxy endpoint details** — Proxy configuration logged at DEBUG (not INFO) to avoid exposing infrastructure topology

### Contributors

- Thanks to [@samnyan](https://github.com/samnyan) for the proxy feature contribution!

## [7.4.2] - 2026-03-31

### Fixed

- **Listener shutdown KeyError** — `_log_stats()` referenced non-existent keys from `MassOperationProtector.get_stats()`. A clean shutdown would raise `KeyError`. Fixed to use actual keys (`rate_limits_triggered`, `operations_blocked`, `chats_rate_limited`)
- **Pin/unpin realtime** — Full pipeline now works end-to-end: listener emits `PIN` -> notifier delivers -> `handle_realtime_notification()` forwards to WebSocket -> browser reloads pinned messages. Previously the relay in `main.py` was missing
- **pyproject.toml version sync** — Was stuck at `7.2.0` since v7.2.0. Now synced with `__init__.py` at `7.4.2`
- **WebSocket subscribe ACL** — Server now sends `subscribe_denied` (instead of `subscribed`) when a restricted user attempts to subscribe to a chat outside their allowed list

## [7.4.1] - 2026-03-31

### Security

- **Avatar ACL bypass** — Restricted users can no longer access avatars outside their allowed chats. `serve_media()` and `serve_thumbnail()` now extract `chat_id` from avatar filenames and enforce per-chat scoping
- **Push endpoint spoofing** — `/internal/push` now supports an optional `INTERNAL_PUSH_SECRET` env var as a bearer token. Prevents co-tenant containers from spoofing live events
- **Reaction recovery data loss** — `insert_reactions()` now retries ALL reactions after a sequence reset, not just the row that triggered the duplicate-key error
- **Push unsubscribe ownership** — `POST /api/push/unsubscribe` is now scoped to the requesting user's `username`, preventing cross-user endpoint removal

### Added

- **`INTERNAL_PUSH_SECRET` env var** — Optional shared secret for `/internal/push` endpoint in multi-tenant Docker environments

## [7.4.0] - 2026-03-31

### Security

- **XSS fix** — `linkifyText()` now percent-encodes raw `"` and `'` in URLs before inserting into `href` attributes

### Fixed

- **Stats filter** — Fixed JSON string-key vs `int` type mismatch that caused per-chat filtering to silently fail. Also removes `media_files`/`total_size_mb` for restricted users
- **Deletion path** — Unknown-chat deletions now resolve the chat ID from DB first, apply rate limiting, skip ambiguous message IDs, and send viewer notifications
- **Folders** — Restricted users no longer see empty folder names/emoticons for folders with 0 accessible chats
- **Push endpoint** — `/internal/push` accepts loopback + RFC1918/Docker private IPs to support split-container SQLite mode

### Changed

- **`delete_message_by_id_any_chat()` replaced** — Replaced by `resolve_message_chat_id()` in the database adapter. The old method deleted from ALL chats with a matching message ID; the new approach resolves to a single chat first and skips ambiguous cases

## [7.3.2] - 2026-03-26

### Fixed

- **Album caption display** — Captions now display correctly for album posts with grouped messages in the viewer

### Contributors

- Thanks to [@vadimvolk](https://github.com/vadimvolk) for the contribution!

## [7.3.1] - 2026-03-25

### Fixed

- **Skip `get_dialogs()` in whitelist mode** — Prevents backup from hanging when `CHAT_IDS` whitelist is configured, by skipping the full dialog enumeration that is unnecessary in whitelist mode (#96)

## [7.3.0] - 2026-03-15

### Added

- **Gap-fill recovery** — Detects gaps in message ID sequences using SQL `LAG()` window function and recovers skipped messages from Telegram API automatically. Available as CLI subcommand (`fill-gaps --chat-id --threshold`) and scheduler option (`FILL_GAPS=true`). Respects all backup config rules
- **Token URL auto-login** — Shareable links with `?token=XXX` parameter for direct viewer access. Token is stripped from URL after login via `history.replaceState`
- **@username display** — Usernames now shown in chat list and message headers
- **Shareable link generation UI** — New controls in admin panel for generating share links

## [7.2.1] - 2026-03-13

### Fixed

- **Login with unreachable database** — Login endpoint now falls through to master env var credentials instead of returning a generic "Unexpected error". Viewer-only users see a clear "Database temporarily unavailable" message (HTTP 503)
- **All data endpoints** — Connection errors now return HTTP 503 "Database temporarily unavailable" instead of generic HTTP 500
- **Audit log resilience** — Audit log writes in the login flow are wrapped in try/except so they never crash the response

### Added

- **Health endpoint** — `GET /api/health` returns `{"status": "ok", "database": "connected"}` (200) or `{"status": "degraded", "database": "unreachable"}` (503). Useful for Docker healthchecks and monitoring
- **Global exception handler** — Catches unhandled DB connection errors across all endpoints and returns 503

## [7.2.0] - 2026-03-10

### Added

- **Share tokens** — Admins can create link-shareable tokens scoped to specific chats. Recipients authenticate via token without needing an account. Tokens support expiry dates, revocation, and use tracking
- **Download restrictions** — `no_download` flag on both viewer accounts and share tokens. Restricted users can still view media inline but cannot explicitly download files or export chat history. Download buttons hidden in the UI for restricted users
- **On-demand thumbnails** — WebP thumbnail generation at whitelisted sizes (200px, 400px) with disk caching under `{media_root}/.thumbs/`. Includes Pillow decompression bomb protection and path traversal guards
- **App settings** — Key-value `app_settings` table for cross-container configuration, with admin CRUD endpoints
- **Audit log improvements** — Action-based filtering in admin panel (prefix match for suffixed events like `viewer_updated:username`), token auth events tracked (`token_auth_success`, `token_auth_failed`, `token_created`, etc.)
- **Admin chat picker metadata** — Chat picker now returns `username`, `first_name`, `last_name` for better display
- **Token management UI** — New "Share Tokens" tab in admin panel with create, revoke, and delete controls. Plaintext token shown once at creation with copy button
- **Token login UI** — Login page has a "Share Token" tab for token-based authentication

### Security

- **Token revocation enforced on active sessions** — Revoking, deleting, or changing scope/permissions of a share token immediately invalidates all sessions created from that token. Sessions track `source_token_id` for precise invalidation
- **Session persistence includes restrictions** — `no_download` and `source_token_id` are now persisted in `viewer_sessions` table, surviving container restarts. Previously `no_download` was lost after restart, silently granting download access
- **Export endpoint respects no_download** — The `GET /api/chats/{chat_id}/export` endpoint now returns 403 for restricted users

### Fixed

- **Create viewer passes all flags** — `is_active` and `no_download` from the admin form are now correctly passed through to `create_viewer_account()`. Previously both flags were silently ignored on creation
- **Token expiry timezone handling** — Frontend now converts local datetime to UTC ISO before sending to the backend, fixing early/late expiry for non-UTC admins
- **Audit filter matches suffixed actions** — Filter now uses prefix matching so "viewer_updated" catches "viewer_updated:username"
- **Migration stamping checks all artifacts** — Entrypoint now checks `viewer_tokens`, `app_settings`, AND `viewer_accounts.no_download` before stamping migration 010 as complete

### Changed

- **Migration 010** — Consolidated idempotent migration creates `viewer_tokens`, `app_settings` tables and adds `no_download` column to `viewer_accounts`. Also adds `no_download` and `source_token_id` columns to `viewer_sessions`
- **Entrypoint stamping** — Updated both PostgreSQL and SQLite stamping blocks to detect all migration 010 artifacts
- **Dockerfile.viewer** — Added Pillow system dependencies (libjpeg, libwebp) for thumbnail generation
- **Version declarations** — `pyproject.toml` and `src/__init__.py` both set to 7.2.0
- **SECURITY.md** — Added 7.x.x as a supported version
- **pyproject.toml** — Added `viewer` optional dependency group for Pillow

## [7.1.7] - 2026-03-08

### Fixed

- **Missing `beautifulsoup4` in Docker image** — `beautifulsoup4` was declared in `pyproject.toml` but missing from `requirements.txt` (used by Docker builds), causing `No module named 'bs4'` when running HTML imports

## [7.1.6] - 2026-03-08

### Fixed

- **Idempotent migrations 007-009** — When `create_all()` runs before Alembic (fresh SQLite databases), tables and columns may already exist. Migrations now inspect the schema before altering, preventing "duplicate column name: username" crashes on upgrade. Fixes #81

## [7.1.5] - 2026-03-08

### Fixed

- **Duplicate messages in real-time viewer** — Race condition in 3-second polling (`checkForNewMessages`) allowed concurrent async calls to both add the same message. Added concurrency guard and deduplication
- **Missing `chat_id` in WebSocket broadcast** — The `new_message` payload was missing `chat_id`, making client-side real-time message insertion a silent no-op. Messages only appeared via polling
- **WebSocket new message handler deduplication** — Added `messages.some()` check to prevent duplicates when both WebSocket and polling deliver the same message

## [7.1.4] - 2026-03-05

### Security

- **Media path injection hardening** — Early rejection of `..` traversal and absolute paths before filesystem operations. Uses `resolve(strict=True)` to prevent TOCTOU race conditions with symlinks. Existing `is_relative_to` check retained as defense-in-depth (CodeQL alerts #12, #13, #14)

## [7.1.3] - 2026-03-05

### Fixed

- **Alembic stamping detects all migrations** — The entrypoint's pre-Alembic database stamping logic now detects migrations 008 (`push_subscriptions.username` column) and 009 (`viewer_sessions` table). Previously it only checked up to 007, causing `CREATE TABLE` failures when `Base.metadata.create_all()` had already created newer tables (e.g. SQLite containers crash-looping on `viewer_accounts already exists`)

## [7.1.2] - 2026-03-05

### Fixed

- **Two-tier session protection** — Replaces the single-backup approach from v7.1.1 with a robust two-tier system:
  - **Golden backup** (`.session.authenticated`) — only written after a successful login, guarantees a known-good recovery point that crash-loops can never corrupt
  - **Pre-connect snapshot** (`.session.bak`) — taken before every connect attempt as a secondary fallback
  - On auth failure, restores from golden backup first, then snapshot. Prevents Telethon's silent DH key renegotiation from permanently destroying authenticated sessions during crash-loops.
  - Uses raw `sqlite3` to verify `auth_key` presence before deciding whether to back up or restore, avoiding false positives from empty/corrupted session files
  - Flushes WAL checkpoint before creating golden backup to ensure file completeness

## [7.1.1] - 2026-03-05

### Added

- **Non-interactive auth script** — `scripts/auth_noninteractive.py` for authenticating Telegram sessions without a TTY (useful for SSH automation, CI pipelines)

### Fixed

- **Session file protection** — Telethon session files are now backed up before each connect attempt. If the container crash-loops (e.g. due to database permission errors), the authenticated session is preserved and restored instead of being overwritten with an empty one
- **Duplicate session_path assignment** in config.py removed

## [7.1.0] - 2026-03-05

### Added

- **Persistent sessions** — Viewer sessions now survive container restarts. Sessions are backed by a `viewer_sessions` database table with an in-memory write-through cache for zero-latency lookups. On startup, active sessions are restored from the database so users stay logged in across restarts, Docker updates, and server reboots. Closes [#84](https://github.com/GeiserX/Telegram-Archive/issues/84).
  - **Alembic migration 009** — Creates `viewer_sessions` table (auto-applied on container startup for both SQLite and PostgreSQL)
  - Graceful degradation: if the database is unavailable, sessions fall back to in-memory only (same behavior as v7.0.x)

### Security

- **Corrupted chat permissions denial** — Sessions with corrupted `allowed_chat_ids` JSON now deny access instead of silently granting access to all chats

## [7.0.3] - 2026-02-27

### Added

- **Viewer-only mode** — When a reverse proxy sets `X-Viewer-Only: true`, master/admin login and all admin API endpoints are blocked. Allows sharing the same backend instance across domains with different access levels.

### Fixed

- **Chat names in admin panel** — Private chats now show `first_name last_name` instead of numeric IDs in the chat picker and viewer list
- **Viewer list shows chat names** — The viewer account list now displays assigned chat titles instead of just a count

## [7.0.2] - 2026-02-27

### Security

- **Per-user push notifications** — Push subscriptions now store the subscriber's `username` and `allowed_chat_ids`. Notifications are only sent to users who have access to the chat where the message was posted. Prevents restricted viewers from receiving push notifications for chats outside their whitelist.
- **Alembic migration 008** — Adds `username` and `allowed_chat_ids` columns to `push_subscriptions` table

### Fixed

- **Stale template cache** — Index HTML now served with `Cache-Control: no-cache, must-revalidate` to prevent browsers from serving outdated templates after upgrades

## [7.0.1] - 2026-02-27

### Fixed

- **Stale template cache** — Added `Cache-Control: no-cache, must-revalidate` header to index.html to prevent browsers from serving stale templates after version upgrades

## [7.0.0] - 2026-02-27

### Added

- **Multi-user viewer access control** — Viewer accounts with per-user chat whitelists. Master (env var) account manages viewer accounts via admin UI. Each viewer sees only their assigned chats across all endpoints and WebSocket. Backward compatible: existing single-user setups work unchanged.
  - `POST /api/admin/viewers` — Create viewer account with username, password, allowed chat IDs
  - `PUT /api/admin/viewers/{id}` — Update viewer account (invalidates sessions)
  - `DELETE /api/admin/viewers/{id}` — Delete viewer account
  - `GET /api/admin/audit` — Paginated audit log
- **Admin settings panel** — Gear icon in sidebar (master only) opens account management UI with viewer CRUD, multi-select chat picker, and activity log
- **Session-based authentication** — Random session tokens replace deterministic PBKDF2 token. Enables real logout, session invalidation, and per-user session limits (max 10)
- **Login rate limiting** — 15 attempts per IP per 5 minutes to prevent brute-force attacks
- **Audit logging** — All login attempts (success/failure), admin actions, and logouts are recorded with IP address and user agent
- **Logout endpoint** — `POST /api/logout` invalidates session and clears cookie (works for both master and viewer)
- **Alembic migration 007** — Creates `viewer_accounts` and `viewer_audit_log` tables

### Security

- **Authenticated media serving** — `/media/*` now requires authentication and validates per-user chat permissions. Previously served via unauthenticated `StaticFiles` mount
- **Path traversal protection** — Media endpoint validates resolved paths stay within the media directory
- **XSS fix** — `linkifyText()` now escapes HTML entities before linkifying URLs, preventing script injection via message text
- **Constant-time token comparison** — All credential comparisons use `secrets.compare_digest`
- **LIKE wildcard escaping** — Search queries no longer treat `%` and `_` as SQL wildcards
- **Generic error messages** — 500 responses no longer leak internal exception details
- **WebSocket per-user enforcement** — Broadcasts now enforce per-connection `allowed_chat_ids`, preventing restricted viewers from receiving messages from unauthorized chats
- **Push notification chat access** — `/api/push/subscribe` validates `chat_id` against user permissions before allowing subscription
- **Media chat-level authorization** — `/media/*` endpoint checks that the requested file belongs to a chat the user has access to
- **Trusted proxy rate limiting** — `X-Forwarded-For` is only trusted from private/Docker IPs, preventing header spoofing to bypass rate limits
- **Stats refresh restricted** — `/api/stats/refresh` now requires master role (was accessible to all authenticated users)
- **Internal push hardened** — `/internal/push` no longer accepts requests when `client_host` is `None`
- **Master username collision** — Creating a viewer account with the same username as the master is rejected

### Changed

- **Auth check endpoint** — `/api/auth/check` now returns `role` ("master"/"viewer") and `username` fields
- **Per-user chat filtering** — All API endpoints and WebSocket subscriptions respect viewer-level `allowed_chat_ids`
- **WebSocket auth** — Validates session cookie during upgrade handshake and enforces per-user chat access

### Contributors

- Thanks to [@PhenixStar](https://github.com/PhenixStar) for the initial concept and discussion in [PR #80](https://github.com/GeiserX/Telegram-Archive/pull/80)

## [6.5.0] - 2026-02-27

### Added

- **Import Telegram Desktop chat exports** — New `telegram-archive import` CLI command reads Telegram Desktop exports (`result.json` + media folders) and inserts them into the database. Imported chats appear in the web viewer like any other backed-up chat. Supports both single-chat and full-account exports. Closes [#81](https://github.com/GeiserX/Telegram-Archive/issues/81).
  - `--path` — Path to export folder containing `result.json`
  - `--chat-id` — Override chat ID (marked format)
  - `--dry-run` — Validate without writing to DB or copying media
  - `--skip-media` — Import only messages/metadata
  - `--merge` — Allow importing into a chat that already has messages
- Handles text messages, photos, videos, documents, voice messages, stickers, and service messages (pins, group actions, etc.)
- Forwards, replies, and edited messages are preserved with full metadata
- Media files are copied into the standard media directory structure

## [6.4.0] - 2026-02-27

### Added

- **`bots` chat type** — New `bots` option for `CHAT_TYPES` to back up bot conversations. Previously, bot chats were silently skipped because they didn't match any chat type (`private`, `groups`, `channels`). Add `bots` to your `CHAT_TYPES` to include them. Bots share `PRIVATE_INCLUDE/EXCLUDE_CHAT_IDS` lists for per-type filtering. Backward compatible — existing configs without `bots` are unaffected.

## [6.3.2] - 2026-02-17

### Fixed

- **Empty chat blank screen** — Chats with no backed-up messages now show a "No messages backed up for this chat yet" empty state instead of a blank screen. Fixes [#78](https://github.com/GeiserX/Telegram-Archive/issues/78).

## [6.3.1] - 2026-02-16

### Fixed

- **Backup resume after crash/restart** — `sync_status` is now updated after every `CHECKPOINT_INTERVAL` batch inserts (default: 1) instead of only at the end of each chat. On crash or power outage, backup resumes from the last committed batch rather than re-fetching all messages for the current chat. Fixes [#76](https://github.com/GeiserX/Telegram-Archive/issues/76).
- **Reduced memory usage on large chats** — Removed in-memory accumulation of all messages per chat; only the current batch is held in memory.

### Added

- **`CHECKPOINT_INTERVAL` environment variable** — Controls how often backup progress is saved (every N batch inserts). Default: `1` (safest). Higher values reduce database writes but increase re-work on crash.

### Refactored

- **Batch commit logic extracted** — Duplicated batch insert code consolidated into `_commit_batch()` helper method.

## [6.3.0] - 2026-02-16

### Added

- **Skip media downloads for specific chats** — New `SKIP_MEDIA_CHAT_IDS` environment variable to skip media downloads for selected chats while still backing up message text. Useful for high-volume media chats where you only need text content. Messages, reactions, and all other data are still fully backed up.
- **Automatic media cleanup for skipped chats** — When `SKIP_MEDIA_DELETE_EXISTING` is `true` (default), existing media files and database records are deleted for chats in the skip list, reclaiming disk space. Set to `false` to keep previously downloaded media while skipping future downloads.
- **Per-chat media control in real-time listener** — The listener now respects `SKIP_MEDIA_CHAT_IDS`, skipping media downloads for new incoming messages in skipped chats.

### Fixed

- **Freed-bytes reporting for deduplicated media** — Media cleanup now correctly reports freed bytes: symlink removals (from deduplicated media) no longer inflate the freed storage count. Only actual file deletions count toward reclaimed space.
- **Empty media directories cleaned up** — After media cleanup, empty per-chat media directories are automatically removed.

### Changed

- **Media cleanup runs once per session** — The cleanup check for skipped chats now uses a session-level cache, avoiding redundant database queries on subsequent backup cycles.

### Contributors

- [@Farzadd](https://github.com/Farzadd) — Initial implementation of `SKIP_MEDIA_CHAT_IDS` ([#74](https://github.com/GeiserX/Telegram-Archive/pull/74))

## [6.2.16] - 2026-02-15

### Fixed

- **Messages intermittently fail to load when clicking chats** — Race condition in `selectChat`: if a previous message load was still in-flight (from another chat, scroll pagination, or auto-refresh), the `loading` gate caused `loadMessages()` to silently return without fetching. Added a version counter to invalidate stale requests and reset the loading gate on chat switch. Also fixes stale auto-refresh results from a previous chat bleeding into the current view.

## [6.2.15] - 2026-02-15

### Fixed

- **Chat search broken (silent 422 error)** — The search bar sent `limit=1000` but the API enforced `le=500`, causing FastAPI to reject every search request with a 422 validation error. The frontend silently swallowed the error, making search appear to return no results. Raised the API limit to 1000 to match the frontend.
- **Chat search ignored in DISPLAY_CHAT_IDS mode** — When `DISPLAY_CHAT_IDS` was configured, the search query was never passed to the database, so typing in the search bar had no effect on the displayed chats.

## [6.2.14] - 2026-02-13

### Fixed

- **PostgreSQL migrations silently rolled back** — The advisory lock used to serialize concurrent migrations was acquired before Alembic's `context.configure()`, triggering SQLAlchemy's autobegin. Alembic detected this as an external transaction and skipped its own transaction management, so DDL changes (new columns, tables) were never committed. Switched to `pg_advisory_xact_lock()` inside the transaction block so Alembic properly commits. Fixes [#70](https://github.com/GeiserX/Telegram-Archive/issues/70).

## [6.2.13] - 2026-02-11

### Fixed

- **Push notifications requiring re-enable** — Push subscriptions can expire (browser push service decides when), causing notifications to silently stop working. The viewer now auto-resubscribes on page load when the browser permission is still granted but the subscription was lost. A `localStorage` flag remembers the user's opt-in preference across subscription losses.
- **Push subscription renewal while tab closed** — Added `pushsubscriptionchange` handler in the service worker so the browser can auto-renew the push subscription even when no tab is open, keeping notifications working indefinitely.

### Changed

- **Refactored push subscription sync** — Extracted `syncSubscriptionToServer()` helper to share logic between initial subscribe, auto-resubscribe, and subscription renewal flows.

## [6.2.12] - 2026-02-09

### Fixed

- **Forum topics always showing same messages** — The auto-refresh (every 3s) was fetching messages without the `topic_id` filter, immediately replacing topic-specific messages with all chat messages. Now properly passes `topic_id` during refresh.
- **"Deleted Account" shown as group name in forum chats** — Clicking a topic passed a minimal object (only `id` and `is_forum`) to the message view, causing `getChatName()` to fall through to "Deleted Account". Now stores and passes the full chat object with title/name fields.

## [6.2.11] - 2026-02-08

### Fixed

- **Backup summary showing zero stats** — The backup completion summary (`Total chats: 0`, `Total messages: 0`, etc.) now calculates statistics directly instead of reading cached values from the viewer. This also pre-populates the stats cache for the viewer on first startup.

### Security

- **Redacted database URL in logs** — The `_safe_url()` method now reconstructs the logged URL entirely from non-sensitive environment variables, ensuring no credential leakage even when `DATABASE_URL` contains a password (CodeQL `py/clear-text-logging-sensitive-data`).

## [6.2.10] - 2026-02-07

### Changed

- **`SECURE_COOKIES` auto-detection** — Default changed from `true` to auto-detect. The viewer now inspects the `X-Forwarded-Proto` header and request scheme to set the `Secure` cookie flag automatically. Behind HTTPS reverse proxies it is `Secure`; over plain HTTP it is not. Explicit `true`/`false` override still works. This fixes silent login failures for users accessing the viewer over HTTP without setting the env var.

### Fixed

- **Archived chats visible in restricted viewers** — The `/api/archived/count` endpoint now respects `DISPLAY_CHAT_IDS`, so the "Archived Chats" row only appears if there are actually archived chats visible to the viewer instance.
- **Doubled archived chats on first click** — Fixed an infinite scroll race condition where navigating to the archived view could trigger a concurrent append fetch (stale `hasMoreChats` from the previous view), duplicating all chat entries on first visit.

## [6.2.9] - 2026-02-07

### Fixed

- **Viewer blank blue page** — Vue.js 3 in-browser template compiler requires `'unsafe-eval'` in the CSP `script-src` directive (it uses `new Function()` internally). Without it, Vue loads but silently fails to compile templates, leaving a blank page. Added `'unsafe-eval'` to fix rendering. Bug present since v6.2.3.

## [6.2.8] - 2026-02-07

### Fixed

- **Viewer CSS/JS broken since v6.2.3** — Content-Security-Policy header blocked all CDN resources (Tailwind CSS, Vue.js, Google Fonts, FontAwesome, Flatpickr), causing the viewer to render without styling or interactivity. Added required CDN domains to `script-src`, `style-src`, and `font-src` directives.

## [6.2.7] - 2026-02-07

### Changed

- **Python 3.14 base image** — Bumped Docker base from `python:3.11-slim` to `python:3.14-slim` in both `Dockerfile` and `Dockerfile.viewer`. All dependencies have pre-built cp314 wheels.
- **Python 3.14 type annotations** — Removed string quotes from forward references (PEP 649 deferred evaluation), replaced `Optional[X]` with `X | None`, simplified `AsyncGenerator` type args (PEP 585).
- **PEP 758 except formatting** — Unparenthesized except clauses now used where applicable.
- **CI updated to Python 3.14** — Tests and lint workflows now run on Python 3.14.
- **Dependabot dev image builds skipped** — `docker-publish-dev` workflow no longer fails on Dependabot PRs (they lack Docker Hub secrets).

## [6.2.6] - 2026-02-07

### Fixed

- **SQLite viewer crash** — Viewer container failed to start when using SQLite because `PRAGMA journal_mode=WAL` requires write access to create `.db-wal` and `.db-shm` sidecar files. WAL and `create_all` are now wrapped in try/except so the viewer degrades gracefully to default journal mode instead of crashing. (#61)
- **Read-only volume mount** — Removed `:ro` from the viewer volume in `docker-compose.yml` since SQLite WAL needs write access. Added comment explaining when `:ro` is safe (PostgreSQL only).

## [6.2.5] - 2026-02-07

### Fixed

- **CodeQL security alerts resolved** — Replaced weak SHA256 auth token with PBKDF2-SHA256 (600k iterations), fixed stack trace exposure in `/internal/push`, and eliminated clear-text password logging by constructing log-safe strings from non-sensitive env vars.
- **CORS credentials with wildcard origins** — Disabled `allow_credentials` when `CORS_ORIGINS=*` (browser security requirement).
- **Auth cookie `Secure` flag** — Cookie now sets `Secure=true` by default, configurable via `SECURE_COOKIES` env var.
- **`/internal/push` access control** — Endpoint restricted to private IPs only (loopback + RFC 1918).
- **Dependabot config** — Removed invalid duplicate Docker ecosystem entry.

### Changed

- **Roadmap updated** — Reflects current v6.x implementation, reordered milestones, added new feature ideas.

## [6.2.4] - 2026-02-07

### Changed

- **Unified environment variables reference** — Consolidated 8+ scattered subsections into one comprehensive table with Scope column (B=backup, V=viewer, B/V=both) and bold category separators.
- **Documented missing env vars** — Added `CORS_ORIGINS`, `SECURE_COOKIES`, and `MASS_OPERATION_BUFFER_DELAY` to the reference table.
- **`ENABLE_LISTENER` master switch** — Prominently documented that `ENABLE_LISTENER=false` disables all `LISTEN_*` and `MASS_OPERATION_*` variables.
- **docker-compose.yml** — Added all missing env vars to both backup and viewer services (listener sub-settings, mass operation, CORS, secure cookies, notifications).
- **.env.example** — Complete rewrite with all variables organized into clear sections.

## [6.2.3] - 2026-02-07

### Added

- **Dependabot configuration** — Automated dependency updates for pip (weekly), GitHub Actions (monthly), and Docker base images (weekly). Groups minor/patch updates, ignores major bumps.
- **Ruff linter and formatter** — Configured in `pyproject.toml` with CI workflow. Replaces flake8/black/isort with a single fast tool. Entire codebase auto-formatted.
- **Pre-commit hooks** — `.pre-commit-config.yaml` with Ruff + standard hooks (check-yaml, trailing-whitespace, etc.).
- **CodeQL security scanning** — Weekly SAST analysis plus on every PR.
- **SECURITY.md** — Responsible disclosure policy with supported versions and scope.
- **CONTRIBUTING.md** — Developer setup guide, branch naming, commit conventions, and testing instructions.
- **PR template** — Checklists for type of change, database changes, data consistency, testing, and security.
- **CODEOWNERS** — Routes all PR reviews to @GeiserX.
- **`.editorconfig`** — Consistent formatting across editors (UTF-8, LF, Python 4-space, YAML 2-space).
- **Content-Security-Policy headers** — CSP, X-Frame-Options, X-Content-Type-Options, and Referrer-Policy on all responses.
- **`CORS_ORIGINS` environment variable** — Configure allowed CORS origins (default: `*` without credentials).
- **`SECURE_COOKIES` environment variable** — Control `secure` flag on auth cookie (default: `true`; set `false` for local HTTP development).

### Fixed

- **CORS misconfiguration** — Removed `allow_credentials=True` when using wildcard origins (browser security requirement). Restricted allowed methods to GET/POST.
- **`/internal/push` access control** — Endpoint now enforces private IP allowlist (loopback + RFC 1918 ranges) instead of silently allowing all requests.
- **Auth cookie missing `secure` flag** — Cookie now sets `secure=True` by default, preventing transmission over plain HTTP.

### Changed

- **Docker Compose security hardening** — Both services now use `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and `tmpfs: [/tmp]`. Viewer volume mounted read-only.
- **GitHub Actions bumped** — `docker/build-push-action` v5→v6, `codecov/codecov-action` v4→v5.
- **Removed `.cursor/rules/project.mdc`** — Redundant with `CLAUDE.md` which is the single source of truth for AI assistant configuration.

## [6.2.2] - 2026-02-07

### Fixed

- **Migration 006 stamping for `create_all()` databases** — SQLite databases created by `create_all()` already include all v6.2.0 schema (forum_topics, is_forum, etc.) but had no `alembic_version`. The stamping logic only detected up to 005, so on restart it tried to re-run migration 006 and failed with `duplicate column name: is_forum`. Now detects the `forum_topics` table as a marker for migration 006

## [6.2.1] - 2026-02-07

### Fixed

- **SQLite migration error on upgrade** — Existing SQLite databases created before Alembic was introduced had no `alembic_version` table. On upgrade to v6.2.0, the entrypoint ran all migrations from scratch, causing `table chats already exists` error. Now detects pre-Alembic SQLite databases and stamps the correct migration version before upgrading (#61)
- **PostgreSQL stamping improvement** — Added migration 005 detection to the PostgreSQL stamping logic (previously only detected up to 004)

## [6.2.0] - 2026-02-06

### Added

- **Forum topics** — Detect forum-enabled channels and extract topic threading (`reply_to_top_id`). Fetch topic metadata via `GetForumTopicsRequest` with fallback inference. Resolve custom emoji document IDs to real unicode emojis. Viewer shows topic list with emoji icons, color indicators, and per-topic message drill-down
- **Chat folders** — Sync user-created Telegram folders via `GetDialogFiltersRequest`. Folder tab bar in viewer sidebar with dynamic filtering
- **Archived chats** — Fetch archived dialogs via `get_dialogs(folder=1)` with clean separation from regular dialogs. Apply same INCLUDE/EXCLUDE/CHAT_TYPES filters. Archived section in viewer with count badge
- **Viewer navigation** — Navigation stack for smart back-button across all views, Telegram-like back navigation preserving main panel content
- **API additions** — `GET /api/folders`, `GET /api/chats/{id}/topics`, `GET /api/archived/count`, plus `archived`, `folder_id`, `topic_id` query params on existing endpoints

### Fixed

- **iOS/mobile scroll** — Fix scroll not working until a programmatic scroll activated it

### Changed

- **Database stability** — PostgreSQL advisory lock to prevent migration deadlocks with concurrent containers. Skip `create_all()` for PostgreSQL (Alembic manages schema exclusively)
- **Migration 006** — Adds `is_forum`, `is_archived` columns to `chats`; `reply_to_top_id` column to `messages`; new tables: `forum_topics`, `chat_folders`, `chat_folder_members`

## [6.1.1] - 2026-02-06

### Fixed

- **Critical: `schedule` command would silently do nothing** - The `run_schedule` function in the CLI called the async `scheduler.main()` without `asyncio.run()`, causing the scheduler to never actually start. This affected all Docker deployments using `python -m src schedule`.

### Changed

- **Removed `:latest` tag from CLI help text** - Docker examples in `--help` output now use `<version>` placeholder instead of `:latest`, following the project convention of always using specific version tags.

## [6.1.0] - 2026-02-06

### Community Contributions

This release includes a major contribution from **[@yarikoptic](https://github.com/yarikoptic)** (Yaroslav Halchenko) - thank you for this substantial improvement to the project!

### Added

- **Unified CLI interface** (`python -m src <command>`) - All operations now route through a single entry point with intuitive subcommands: `auth`, `backup`, `schedule`, `export`, `stats`, `list-chats`. Includes comprehensive `--help` with workflow guidance. (contributed by @yarikoptic, PR #57)

- **Python packaging with `pyproject.toml`** - Proper PEP 621 package definition with centralized dependencies. Install locally with `pip install -e .` to get the `telegram-archive` command. (contributed by @yarikoptic, PR #57)

- **`--data-dir` option for local development** - Override the default `/data` directory to avoid permission issues when developing outside Docker:
  ```bash
  telegram-archive --data-dir ./data list-chats
  python -m src --data-dir ./data backup
  ```

- **`telegram-archive` executable script** - Direct execution without installation (`./telegram-archive --help`). (contributed by @yarikoptic, PR #57)

- **Smart database migrations in entrypoint** - Migrations now skip for `auth` command (no DB needed yet) and check database existence before running SQLite migrations. (contributed by @yarikoptic, PR #57)

### Changed

- **Dockerfile default CMD now shows help** - Running the container without an explicit command displays help instead of silently starting the scheduler. The `docker-compose.yml` explicitly runs `schedule`. This is a behavioral change for users running `docker run` without a command - add `python -m src schedule` to your command.

- **Unified command syntax** - Old module-based commands (`python -m src.telegram_backup`, `python -m src.export_backup stats`) are replaced by `python -m src backup`, `python -m src stats`, etc.

## [6.0.3] - 2026-02-02

### Community Contributions

This release includes contributions from **[@yarikoptic](https://github.com/yarikoptic)** - welcome to the project! 🎉

### Improved

- **Better error messages for permission issues** (#54, #55) - Authentication setup now provides clear troubleshooting guidance when encountering permission errors (common with Podman or Docker UID mismatches):
  ```
  PERMISSION ERROR - Unable to write to session directory

  For Podman users:
    Add --userns=keep-id to your run command

  For Docker users:
    mkdir -p data && sudo chown -R 1000:1000 data
  ```

### Changed

- **Standardized on `docker compose` (v2) syntax** - All documentation and scripts now use the modern `docker compose` command instead of the deprecated `docker-compose` (v1). Docker Compose v2 has been built into Docker CLI since mid-2021, and v1 was deprecated in July 2023. (contributed by @yarikoptic)

- **`init_auth.sh` is now executable by default** - No need to manually run `chmod +x init_auth.sh` before using the script. (contributed by @yarikoptic)

### Added

- **Shellcheck CI workflow** - Added GitHub Actions workflow to lint shell scripts on push/PR, improving code quality for bash scripts. (contributed by @yarikoptic)

## [6.0.2] - 2026-02-02

### Fixed
- **Reduced Telethon disconnect warnings** (#50) - Added graceful disconnect handling to reduce "Task was destroyed but it is pending" asyncio warnings during shutdown or reconnection. These warnings are caused by a [known Telethon issue](https://github.com/LonamiWebs/Telethon/issues/782) and don't affect functionality.

### Technical
- Added small delay after `client.disconnect()` to allow internal task cleanup
- Wrapped disconnect in try/except to handle cleanup errors gracefully

## [6.0.1] - 2026-01-30

### Fixed
- **Graceful handling of inaccessible chats** (fixes #49) - When you lose access to a channel/group (kicked, banned, left, or it went private), the backup now logs a clean warning instead of a full error traceback:
  ```
  WARNING - → Skipped (no access): ChannelPrivateError
  ```
  Previously this would show a confusing multi-line error that looked like a bug.

### Technical
- Added specific error handling for `ChannelPrivateError`, `ChatForbiddenError`, and `UserBannedInChannelError`
- These Telegram API responses are now treated as expected conditions, not application errors

## [6.0.0] - 2026-01-28

### ⚠️ Breaking Changes

This is a major release with breaking schema changes. **Backup your database before upgrading.**

#### Normalized Media Storage

Media metadata is now stored exclusively in the `media` table instead of being duplicated in the `messages` table.

**Removed columns from `messages` table:**
- `media_type`
- `media_id`
- `media_path`

**API response format changed:**

Before (v5.x):
```json
{
  "id": 123,
  "media_type": "photo",
  "media_path": "/data/backups/media/123/file.jpg",
  "media_file_name": "photo.jpg",
  "media_mime_type": "image/jpeg"
}
```

After (v6.0.0):
```json
{
  "id": 123,
  "media": {
    "type": "photo",
    "file_path": "/data/backups/media/123/file.jpg",
    "file_name": "photo.jpg",
    "file_size": 12345,
    "mime_type": "image/jpeg",
    "width": 1920,
    "height": 1080
  }
}
```

#### Service Messages and Polls

- Service messages: Now detected by `raw_data.service_type === 'service'` instead of `media_type === 'service'`
- Polls: Now detected by presence of `raw_data.poll` instead of `media_type === 'poll'`

### Added

#### Simple Whitelist Mode with `CHAT_IDS` (fixes #48)

New `CHAT_IDS` environment variable provides a simple way to backup only specific chats:

```bash
# Backup ONLY these 2 channels - nothing else
CHAT_IDS=-1001234567890,-1009876543210
```

**Two filtering modes:**

| Mode | When | How it works |
|------|------|--------------|
| **Whitelist** | `CHAT_IDS` is set | Backup ONLY the listed chats. All other settings ignored. |
| **Type-based** | `CHAT_IDS` not set | Use `CHAT_TYPES` + `INCLUDE`/`EXCLUDE` filters (existing behavior). |

This solves the common confusion where users expected `CHANNELS_INCLUDE_CHAT_IDS` to act as a whitelist, but it was actually additive.

#### Removed `LISTEN_ALBUMS` Setting (fixes #46)

The `LISTEN_ALBUMS` setting was redundant and has been removed. Albums are now automatically handled via `grouped_id` in the NewMessage handler. The viewer groups messages by `grouped_id` to display albums correctly.

#### Foreign Key Constraints
- `media(message_id, chat_id)` → `messages(id, chat_id)` (ON DELETE CASCADE)
- `reactions.user_id` → `users.id` (nullable, ON DELETE SET NULL)

**Note:** `messages.sender_id` does NOT have a FK constraint because sender_id can contain channel/group IDs that aren't in the users table. The relationship is maintained at ORM level only.

#### New Indexes
- `idx_messages_reply_to` - Fast reply message lookups
- `idx_media_downloaded` - Find undownloaded media by chat
- `idx_media_type` - Filter media by type
- `idx_reactions_user` - User reaction queries
- `idx_chats_username` - Chat username lookups
- `idx_users_username` - User username lookups

### Changed

- **Media file_path column type**: Changed from `String(500)` to `Text` to support longer paths
- **Media relationship**: Messages now have a `media_items` relationship for direct access

### Migration Guide

The Alembic migration handles data migration automatically:

1. **Backup your database** before upgrading
2. The migration will:
   - Copy any missing media data from `messages` to `media` table
   - Create a backup table `_messages_media_backup` for rollback
   - Drop the `media_type`, `media_id`, `media_path` columns
   - Add foreign key constraints
   - Create new indexes

**Run the migration:**
```bash
# If using Docker
docker exec telegram-backup alembic upgrade head

# If running locally
alembic upgrade head
```

**Rollback if needed:**
```bash
alembic downgrade 004
```

### Technical Notes

- SQLite: Uses table recreation for schema changes (SQLite doesn't support DROP COLUMN in older versions)
- PostgreSQL: Uses direct ALTER TABLE operations
- Migration is reversible - downgrade restores columns from backup table

## [5.4.9] - 2026-01-28

### Added

- **Notification deep links** — Clicking a push notification now opens the viewer directly at the relevant chat

## [5.4.8] - 2026-01-27

### Fixed

- **Migration retry logic** — Added retry logic for PostgreSQL connection during migrations, handling transient connection failures on startup

## [5.4.7] - 2026-01-26

### Fixed

- **Push notifications respect `DISPLAY_CHAT_IDS`** — Push notifications now filter by the viewer's `DISPLAY_CHAT_IDS` configuration, preventing notifications for chats not shown in the viewer

## [5.4.6] - 2026-01-26

### Fixed

- **Auto-stamp pre-Alembic databases** — Existing databases created before Alembic was introduced are now automatically detected and stamped with the correct migration version on startup

## [5.4.5] - 2026-01-26

### Fixed

- **PWA icon backgrounds** — Added dark background to PWA icons for better visibility on light home screens

## [5.4.4] - 2026-01-26

### Added

- **PWA manifest and dark logo** — Proper PWA manifest with dark logo for installable web app experience

## [5.4.3] - 2026-01-26

### Fixed

- **VAPID push headers** — Use `py_vapid sign()` for VAPID headers, fixing push notification delivery failures

## [5.4.2] - 2026-01-26

### Fixed

- **Service worker scope** — Serve service worker from root with correct scope, fixing push notification registration failures

## [5.4.1] - 2026-01-25

### Fixed
- **Scroll-to-bottom button not appearing** - Fixed detection logic for `flex-col-reverse` containers where `scrollTop` is negative when scrolled up

## [5.4.0] - 2026-01-25

### Added

#### Multiple Pinned Messages Support
- **Pinned message banner** - Shows currently pinned message at the top of the chat, matching Telegram's UI
- **Pin navigation** - Click the message content to scroll to that pinned message and cycle through others
- **Pin count indicator** - Shows "(1 of N)" when multiple messages are pinned
- **Pinned Messages view** - Click the list icon to view all pinned messages in a dedicated view
- **Real-time pin sync** - Listener now catches pin/unpin events when `ENABLE_LISTENER=true`
- **Automatic pin sync** - Pinned messages are synced on every backup (no manual migration needed)
- **API endpoint** - `GET /api/chats/{chat_id}/pinned` returns all pinned messages

#### Database
- **`is_pinned` column** - New column on messages table to track pinned status
- **Alembic migration** - Migration `004` adds the column and index automatically

### Fixed
- **Auto-load older messages** - Replaced manual "Load older messages" button with automatic Intersection Observer loading
- **Telegram-style loading spinner** - Shows spinning indicator while fetching older messages
- **Alembic migrations auto-run** - Docker image now includes Alembic and runs migrations automatically on startup for PostgreSQL

### Upgrade Notes

**Database Migration Required:**

The migration runs automatically on startup. If you're using PostgreSQL, ensure the backup container has write access.

After upgrading, pinned messages will be populated on the next backup run. If you want to populate them immediately without waiting for the next backup:

```bash
# Trigger a manual backup to sync pinned messages
docker exec telegram-backup python -m src backup
```

If using the real-time listener (`ENABLE_LISTENER=true`), pin/unpin events will be captured automatically going forward.

## [5.3.7] - 2026-01-22

### Fixed
- **Avatar filename mismatch** (#35, #41) - Avatars are now saved as `{chat_id}_{photo_id}.jpg` to match what the viewer expects. Previously saved as `{chat_id}.jpg` which caused avatars to not display.

### Added
- **`scripts/cleanup_legacy_avatars.py`** - Utility script to remove old `{chat_id}.jpg` avatar files after they've been replaced by the new format. Run with `--dry-run` to preview changes.

### Changed
- **Shared avatar utility** - Avatar path generation moved to `src/avatar_utils.py` for consistency between backup and listener
- **Skip redundant downloads** - Avatars are only downloaded when the file doesn't exist or is empty

### Upgrade Notes
Legacy avatar files (`{chat_id}.jpg`) are still supported via fallback. To clean up old files after new-format avatars are downloaded:
```bash
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py --dry-run  # Preview
docker exec telegram-backup python scripts/cleanup_legacy_avatars.py            # Apply
```

## [5.3.6] - 2026-01-21

### Fixed

- **Avatar download type check** — Avatar download now uses photo type check instead of `photo_id`, fixing cases where avatars failed to download

## [5.3.5] - 2026-01-21

### Fixed

- **Avatar download on `photo_changed` event** — Avatars are now downloaded when a `photo_changed` chat action event is detected by the listener

## [5.3.4] - 2026-01-21

### Fixed

- **Push notification session factory** — Corrected session factory access in push notifications, fixing notification delivery failures

## [5.3.3] - 2026-01-20

### Fixed
- **Listener media deduplication** - Real-time listener now uses the same deduplication logic as scheduled backups, creating symlinks to `_shared` directory instead of downloading duplicates

## [5.3.2] - 2026-01-20

### Added
- **Forwarded message info** - Shows the original sender's name for forwarded messages (resolved from Telegram when possible)
- **Channel post author** - Shows the post author (signature) for channel messages when enabled in the channel

### Fixed
- **Avatar refresh not working** (#35) - Simplified avatar logic to always update on each backup. Removed `AVATAR_REFRESH_HOURS` config (was unreliable)

### Removed
- `AVATAR_REFRESH_HOURS` environment variable - Avatars now update on every backup run automatically

## [5.3.1] - 2026-01-20

### Fixed
- **Album duplicates showing** - Fixed `grouped_id` comparison (string vs integer) causing albums to show duplicate placeholder messages. Added `getGroupedId()` helper that converts to string for consistent comparison.

### Added
- **Service messages** - Chat actions (photo changed, title changed, user joined/left) now display as centered service messages in the viewer, like the real Telegram client
- **`scripts/normalize_grouped_ids.py`** - Migration script to normalize old `grouped_id` values to strings. Run with `--dry-run` to preview changes.

### Upgrade Notes
If you have existing albums showing as duplicates, run the migration script:
```bash
docker exec telegram-backup python scripts/normalize_grouped_ids.py --dry-run  # Preview
docker exec telegram-backup python scripts/normalize_grouped_ids.py            # Apply
```

## [5.3.0] - 2026-01-19

### Fixed

#### Bug Fixes
- **Long message notification error** (#36) - Truncate notification payload to avoid PostgreSQL NOTIFY 8KB limit
- **Non-Latin export encoding** (#34) - JSON export now uses UTF-8 encoding with RFC 5987 filename encoding
- **ChatAction photo_removed error** (#28) - Fixed `AttributeError: 'Event' object has no attribute 'photo_removed'`
- **Album grouping flaky** (#29) - Albums now save correct media_type (photo/video) instead of generic 'album'
- **Album media not downloading** (#31) - Album handler now downloads media when `LISTEN_NEW_MESSAGES_MEDIA=true`
- **Sender name position** - Fixed sender names appearing at bottom instead of top with flex-col-reverse layout

### Changed
- Improved documentation for chat filtering options (`GLOBAL_INCLUDE_CHAT_IDS` vs type-specific) (#33)

## [5.2.0] - 2026-01-18

### Fixed

#### Critical Bug Fixes
- **`get_statistics` missing** - Fixed `AttributeError: 'DatabaseAdapter' object has no attribute 'get_statistics'` at end of backup (#23)
- **FK violation on new chats** - Listener now creates chat record before inserting messages, fixing foreign key violations when adding new `PRIORITY_CHAT_IDS` (#25)
- **VIEWER_TIMEZONE not applied** - Times were showing in UTC instead of configured timezone; now properly converts from UTC to viewer timezone (#24)
- **LOG_LEVEL=WARN not working** - Added alias mapping from `WARN` to `WARNING` for Python compatibility (#26)
- **Date separators position** - Fixed date separators appearing at wrong position with flex-col-reverse layout

#### Mobile UI Improvements (iOS/Android)
- **Avatar distortion** - Chat avatars were rendering as ellipsoids on mobile; now perfectly round with `aspect-square` and `shrink-0`
- **Chat name overflow** - Long channel names caused massive header bars; now truncated with `max-width` on mobile
- **Search bar too wide** - Reduced from fixed 256px to responsive `w-28 sm:w-48 md:w-64`
- **Export button hidden** - Was pushed off-screen on small devices; now always visible with compact sizing
- **White status bar strips** - Added `theme-color` meta tag and safe area insets for proper iOS status bar theming

### Added

#### Integrated Media Lightbox
- **Image lightbox** - Click images to view fullscreen instead of opening new tab
- **Video lightbox** - Videos now open in integrated player with autoplay
- **Media navigation** - Navigate between all media (photos, videos, GIFs) with arrow keys or buttons
- **Keyboard shortcuts** - `←`/`→` to navigate, `Esc` to close
- **Play button overlay** - Video thumbnails show play button for clear affordance
- **Download button** - Download media directly from lightbox

#### Performance & UX
- **flex-col-reverse scroll** - Messages container uses CSS-based instant scroll-to-bottom (no JS hacks, better mobile performance)
- iOS Safe Area support (`env(safe-area-inset-*)`) for notch/Dynamic Island devices
- `apple-mobile-web-app-capable` meta tag for PWA-like experience
- Responsive header padding (`px-2 py-2` on mobile, `px-4 py-3` on desktop)

## [5.1.0] - 2026-01-18

### Fixed

#### iOS Safari / In-App Browser Compatibility
- **Critical**: Fixed JavaScript crash when `Notification` API is undefined (iOS Safari, in-app browsers)
  - The Vue app would crash before auth check could run, showing "Authentication is disabled"
  - Now uses `typeof Notification !== 'undefined'` check instead of optional chaining
- **Fixed**: Auth check returning `null` instead of `false` when cookie is missing
  - Python's `None and X` returns `None`, not `False` - now wrapped in `bool()`
- Added `authCheckFailed` state with helpful message for in-app browser users

#### Notification Improvements
- Added "Notifications blocked" banner when push is subscribed but browser has denied permission
- Users can unsubscribe from push directly from the banner

### Added
- **`AUTH_SESSION_DAYS`** - Configure authentication session duration (default: 30 days)
- Auth test page at `/static/test-auth.html` for debugging (temporary)

### Documentation
- Added missing env vars: `AUTH_SESSION_DAYS`, `BATCH_SIZE`, `DATABASE_TIMEOUT`, `SESSION_NAME`
- Updated mass operation protection docs to reflect actual behavior (rate limiting, not zero-footprint)

## [5.0.0] - 2026-01-18

### ⚠️ Major Release - Real-time Sync & Media Path Changes

This release introduces **real-time message sync**, **zero-footprint mass operation protection**, and **consistent media path naming**. Migration scripts are provided for existing installations.

### Added

#### Real-time Listener Mode
- **`ENABLE_LISTENER`** - Background listener for instant sync (no waiting for scheduled backup)
- **`LISTEN_EDITS`** - Apply text edits to backed up messages in real-time
- **`LISTEN_DELETIONS`** - Mirror deletions from Telegram (with protection, see below)
- **`LISTEN_NEW_MESSAGES`** - Save new messages immediately (default: true)
- **`LISTEN_NEW_MESSAGES_MEDIA`** - Download media in real-time (default: false)
- **`LISTEN_CHAT_ACTIONS`** - Track chat photo/title changes, member joins/leaves
- **`LISTEN_ALBUMS`** - Detect and group album uploads together

#### Mass Operation Rate Limiting
- **Sliding-window rate limiter** protects against mass edit/deletion attacks
- **`MASS_OPERATION_THRESHOLD`** - Max operations per chat before blocking (default: 10)
- **`MASS_OPERATION_WINDOW_SECONDS`** - Time window for counting operations (default: 30)
- First N operations are applied, then chat is blocked for remainder of window
- To prevent ANY deletions from affecting your backup, set `LISTEN_DELETIONS=false`

#### Priority Chats
- **`PRIORITY_CHAT_IDS`** - Process these chats FIRST in all backup/sync operations
- Useful for ensuring important chats are always backed up before others

#### Viewer Enhancements
- **WebSocket real-time updates** - New messages appear instantly without refresh
- **Infinite scroll** - Cursor/keyset pagination for large chats
- **Album grid display** - Photo/video albums shown as grids like Telegram
- **Compact stats dropdown** - Stats moved to dropdown next to header
- **Per-chat stats** - Message count, media count, total size per chat
- **"Real-time sync" indicator** - Shows when listener is active
- **`SHOW_STATS`** - Hide stats dropdown for restricted viewers (default: true)

#### Web Push Notifications
- **`PUSH_NOTIFICATIONS`** - Notification mode: `off`, `basic`, `full` (default: basic)
  - `off` - No notifications at all
  - `basic` - In-browser notifications (tab must be open)
  - `full` - **Persistent Web Push** (works even when browser is closed!)
- **Auto-generated VAPID keys** - Stored in database, persist across restarts
- **Subscription management** - Subscriptions survive container restarts and updates
- **Automatic cleanup** - Expired subscriptions removed automatically
- **Optional custom VAPID keys** via `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_CONTACT`

#### Migration Scripts
- **`scripts/migrate_media_paths.py`** - ⚠️ **REQUIRED** - Normalizes media folder names to use marked IDs
- **`scripts/update_media_sizes.py`** - ⚠️ **REQUIRED** - Populates file_size for accurate stats
- **`scripts/detect_albums.py`** - ⚠️ **HIGHLY RECOMMENDED** - Detect albums in existing backups for album grid display
- **`scripts/deduplicate_media.py`** - ⚠️ **HIGHLY RECOMMENDED** - Global deduplication using symlinks (saves disk space)
- **`scripts/restore_chat.py`** - Repost archived messages to Telegram

### Changed
- **Shared Telethon client** - Backup and listener share connection (avoids session DB locks)
- **WAL mode for session DB** - Better concurrency for Telethon session
- **Media folder naming** - Groups/channels now use marked IDs (e.g., `-35258041/` not `35258041/`)
- **Bulk SQL operations** - Migration scripts use single queries per batch (10-100x faster)

### Fixed
- Media 404s due to inconsistent folder naming (positive vs negative IDs)
- Audio files served with wrong Content-Type (now audio/ogg, audio/mp3, etc.)
- Stats calculation error with Decimal types (JSON serialization)
- Session DB locking when running backup and listener simultaneously

### ⚠️ Migration Required

**If upgrading from v4.x with existing data:**

1. **Run migration scripts** (inside Docker container):
   ```bash
   # 1. Normalize media paths (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.migrate_media_paths

   # 2. Update file sizes for accurate stats (REQUIRED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.update_media_sizes

   # 3. Detect albums for grid display (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.detect_albums

   # 4. Deduplicate media files (HIGHLY RECOMMENDED)
   docker run --rm -e DB_TYPE=postgresql ... python -m scripts.deduplicate_media
   ```

2. **Update docker-compose.yml** with new env variables (see README)

See [Upgrading to v5.0.0](#upgrading-to-v500-from-v4x) below for detailed instructions.

### Related Issues
- Fixes #12 - Timezone-aware datetime sorting
- Fixes #20 - Real-time sync for edits/deletions
- Fixes #21 - Mass operation protection
- Fixes #22 - Media path consistency

## [4.1.5] - 2026-01-15

### Improved
- **Quick Start guide** - Expanded with step-by-step instructions for beginners
- **Database configuration** - Added prominent warning about viewer needing same DB path
- **Troubleshooting table** - Common permission and setup issues
- **docker-compose.yml** - Clearer comments about matching DB settings

### Added
- `scripts/release.sh` - Validates changelog entry before allowing tag creation

## [4.1.4] - 2026-01-15

### Changed
- Moved all upgrade notices from README to `docs/CHANGELOG.md`
- README now references CHANGELOG for upgrade instructions

### Improved
- Release workflow now extracts changelog notes for GitHub releases
- Added release guidelines to CLAUDE.md
- Documented chat ID format requirements

## [4.1.3] - 2026-01-15

### Added
- Prominent startup banner showing SYNC_DELETIONS_EDITS status
- Makes it clear why backup re-checks all messages from the start

## [4.1.2] - 2026-01-15

### Fixed
- **PostgreSQL reactions sequence out of sync** - Auto-detect and recover from sequence drift
- Prevents `UniqueViolationError` on reactions table after database restores

### Added
- `scripts/fix_reactions_sequence.sql` - Manual fix script for affected users
- Troubleshooting section in README for this issue

## [4.1.1] - 2026-01-15

### Added
- **Auto-correct DISPLAY_CHAT_IDS** - Viewer automatically corrects positive IDs to marked format (-100...)
- Helps users who forget the -100 prefix for channels/supergroups

## [4.1.0] - 2026-01-14

### Added
- **Real-time listener** for message edits and deletions (`ENABLE_LISTENER=true`)
- Catches changes between scheduled backups
- `SYNC_DELETIONS_EDITS` option for batch sync of all messages

### Fixed
- Timezone handling for `edit_date` field (PostgreSQL compatibility)
- Tests updated for pytest compatibility

## [4.0.7] - 2026-01-14

### Fixed
- Strip timezone from `edit_date` before database insert/update
- Prevents `asyncpg.DataError` with PostgreSQL TIMESTAMP columns

## [4.0.6] - 2026-01-14

### Fixed
- **CRITICAL: Chat ID format mismatch** - Use marked IDs consistently
- Chats now stored with proper format (-100... for channels/supergroups)

### ⚠️ Breaking Change
**Database migration required if upgrading from v4.0.5!**

See [Upgrading to v4.0.6](#upgrading-to-v406-from-v405) below.

## [4.0.5] - 2026-01-13

### Added
- CI workflow for dev builds on PRs
- Tests for timezone and ID format handling

### Known Issues
- Chat ID format bug (fixed in v4.0.6)

## [4.0.4] - 2026-01-12

### Fixed
- `CHAT_TYPES=` (empty string) now works for whitelist-only mode
- Previously caused ValueError due to incorrect env parsing

## [4.0.3] - 2026-01-11

### Fixed
- Environment variable parsing for empty CHAT_TYPES

## [4.0.2] - 2026-01-05

### Changed

- **Viewer title** — Renamed viewer browser title to "Telegram Archive"
- **PostgreSQL version** — Updated docker-compose example to PostgreSQL 18

## [4.0.1] - 2026-01-05

### Fixed

- **Timezone stripping for PostgreSQL** — Strip timezone from datetimes for PostgreSQL compatibility
- **Async merge fix** — Fixed async database merge operations

### Added

- **Migration script** — Added migration script for v3.x to v4.0 database upgrade
- **Upgrade guide** — Added v3.x to v4.0 upgrade documentation and updated docker-compose.yml with new image names

## [4.0.0] - 2026-01-10

### ⚠️ Breaking Change
**Docker image names changed!**

| Old (v3.x) | New (v4.0+) |
|------------|-------------|
| `drumsergio/telegram-backup-automation` | `drumsergio/telegram-archive` |
| Same image with command override | `drumsergio/telegram-archive-viewer` |

See [Upgrading from v3.x to v4.0](#upgrading-from-v3x-to-v40) below.

### Changed
- Split into two Docker images (backup + viewer)
- Viewer image is smaller (~150MB vs ~300MB)

## [3.0.5] - 2025-12-31

### Fixed

- **Empty `CHAT_TYPES` for whitelist-only mode** — Allow empty `CHAT_TYPES` for users who only want to back up explicitly listed chats

### Added

- **GitHub issue templates** — Bug report, feature request, and question templates
- **FUNDING.yml** — GitHub Sponsors configuration
- **Roadmap** — Added roadmap section with planned features including multi-tenancy, OAuth, and magic links

## [3.0.4] - 2025-12-19

### Changed

- **Documentation update** — Updated README and `.env.example` with v2 backward compatibility information

## [3.0.3] - 2025-12-19

### Fixed

- **v2 backward compatibility** — Added backward compatibility for v2 `DATABASE_PATH` and `DATABASE_DIR` environment variables, so upgrades from v2 work without changing configuration

## [3.0.2] - 2025-12-19

### Fixed

- **`create_all` idempotency** — Use `checkfirst=True` in `create_all()` to skip existing tables, preventing errors when restarting with an existing database

## [3.0.1] - 2025-12-19

### Fixed

- **Reaction model foreign key** — Added `ForeignKeyConstraint` to Reaction model for composite key, fixing database integrity issues with reaction storage

## [3.0.0] - 2025-12-19

### Added
- PostgreSQL support
- Async database operations with SQLAlchemy
- Alembic migrations

### Changed
- Database layer rewritten for async

## [2.x] - 2025-XX-XX

### Features
- SQLite database
- Web viewer
- Media download support

---

# Upgrading

## Upgrading to v5.0.0 (from v4.x)

> ⚠️ **Migration Scripts Recommended**

v5.0.0 changes media folder naming to use marked IDs consistently. While the backup will work without migration, **running the migration scripts is highly recommended** for:
- Correct media display in viewer (no 404s)
- Accurate file size statistics
- Album grid display for existing photos/videos

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker compose stop telegram-backup
   ```

2. **Pull the new image:**
   ```bash
   docker compose pull
   ```

3. **Run migration scripts** (one at a time, wait for each to finish):

   ```bash
   # Replace with your actual values
   NETWORK=telegram-backup_default
   DB_HOST=your-postgres-container
   DB_PASS=your-password
   BACKUP_PATH=/path/to/backups

   # 1. Media path migration (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.migrate_media_paths

   # 2. Update file sizes (HIGHLY RECOMMENDED)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.update_media_sizes

   # 3. Detect albums (optional but recommended)
   docker run --rm \
     -e DB_TYPE=postgresql \
     -e POSTGRES_HOST=$DB_HOST \
     -e POSTGRES_PASSWORD=$DB_PASS \
     -e POSTGRES_USER=telegram \
     -e POSTGRES_DB=telegram_backup \
     -e BACKUP_PATH=/data/backups \
     --network $NETWORK \
     -v $BACKUP_PATH:/data/backups \
     drumsergio/telegram-archive:latest \
     python -m scripts.detect_albums
   ```

4. **Update docker-compose.yml** with new env variables:
   ```yaml
   environment:
     # ... existing vars ...
     # Real-time listener (recommended)
     ENABLE_LISTENER: true
     LISTEN_EDITS: true
     LISTEN_DELETIONS: true  # ⚠️ Will delete from backup!
     LISTEN_NEW_MESSAGES: true
     # Mass operation protection
     MASS_OPERATION_THRESHOLD: 10
     MASS_OPERATION_WINDOW_SECONDS: 30
     # Optional: Priority chats (processed first)
     # PRIORITY_CHAT_IDS: -1002240913478,-1001234567890
   ```

5. **Start the new version:**
   ```bash
   docker compose up -d
   ```

**If starting fresh:** No migration needed, just use the new image.

---

## Upgrading to v4.0.6 (from v4.0.5)

> 🚨 **Database Migration Required**

v4.0.5 had a bug where chats were stored with positive IDs while messages used negative (marked) IDs, causing foreign key violations.

### Migration Steps

1. **Stop your backup container:**
   ```bash
   docker compose stop telegram-backup
   ```

2. **Run the migration script:**

   **PostgreSQL:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids.sql
   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < migrate_to_marked_ids.sql
   ```

   **SQLite:**
   ```bash
   curl -O https://raw.githubusercontent.com/GeiserX/Telegram-Archive/master/scripts/migrate_to_marked_ids_sqlite.sql
   sqlite3 /path/to/telegram_backup.db < migrate_to_marked_ids_sqlite.sql
   ```

3. **Pull and restart:**
   ```bash
   docker compose pull
   docker compose up -d
   ```

**If upgrading from v4.0.4 or earlier:** No migration needed.
**If starting fresh:** No migration needed.

---

## Upgrading from v3.x to v4.0

> ⚠️ **Docker image names changed**

### Update your docker-compose.yml:

```yaml
# Before (v3.x)
telegram-backup:
  image: drumsergio/telegram-backup-automation:latest

telegram-viewer:
  image: drumsergio/telegram-backup-automation:latest
  command: uvicorn src.web.main:app --host 0.0.0.0 --port 8000

# After (v4.0+)
telegram-backup:
  image: drumsergio/telegram-archive:latest

telegram-viewer:
  image: drumsergio/telegram-archive-viewer:latest
  # No command needed
```

Then:
```bash
docker compose pull
docker compose up -d
```

**Your data is safe** - no database migration needed.

---

## Upgrading from v2.x to v3.0

Transparent upgrade - just pull and restart:
```bash
docker compose pull
docker compose up -d
```

Your existing SQLite data works automatically. v3 detects v2 environment variables for backward compatibility.

**Optional:** Migrate to PostgreSQL - see README for instructions.

---

## Chat ID Format (Important!)

Since v4.0.6, all chat IDs use Telegram's "marked" format:

| Entity Type | Format | Example |
|-------------|--------|---------|
| Users | Positive | `123456789` |
| Basic groups | Negative | `-123456789` |
| Supergroups/Channels | -100 prefix | `-1001234567890` |

**Finding Chat IDs:** Forward a message to @userinfobot on Telegram.

When configuring `GLOBAL_EXCLUDE_CHAT_IDS`, `DISPLAY_CHAT_IDS`, etc., use the marked format.
