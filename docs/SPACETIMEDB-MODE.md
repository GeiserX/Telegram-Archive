# SpacetimeDB mode

Status: parked. Nothing here is implemented, and we will not start until SpacetimeDB ships tables that live on disk. The vendor has announced disk and object storage tables for a release after v2.10.1; today every table row is resident in RAM, and an archive does not need in-memory speed, it needs to hold years of messages on an SSD. When a release with disk tables exists for the self-hosted server, re-read the "Reopen when" section, re-check every claim below against that release, and then run the experiment plan.

SQLite stays the default and PostgreSQL stays supported. This is a design for a third, opt-in deployment mode that replaces the SQL database with a [SpacetimeDB](https://spacetimedb.com) module written in Rust.

The goal is real reactivity: the browser subscribes to the archive and receives every insert, edit, deletion and reaction as it commits, with no polling and no relay socket in between. The archiver stays Python and keeps its code paths; only its database adapter changes. We checked everything here against SpacetimeDB v2.10.1, source and docs at tag [`v2.10.1`](https://github.com/clockworklabs/SpacetimeDB/tree/v2.10.1). Where the docs and the code disagreed, the code won.

## What SpacetimeDB is, for this purpose

- **The module is the backend.** Tables, write functions (reducers), read functions (views, procedures) and access rules are Rust code compiled to WebAssembly and published into the database. There is no query planner to persuade. The module walks indexes.
- **All table data lives in RAM.** Disk holds an append-only [commit log](https://spacetimedb.com/docs/reference/internals/commitlog) and periodic snapshots. Nothing prunes the commit log. The practical size limit is host memory.
- **Reducers are transactions.** One call, one atomic transaction, no return value, no I/O. A reducer that returns `Err` rolls back.
- **[Subscriptions](https://spacetimedb.com/docs/clients/subscriptions) replace fetching.** A client subscribes to a query and holds a live replica of the matching rows. Each committed transaction delivers at most one update. The [subscription language](https://spacetimedb.com/docs/reference/sql) is a single table with a `WHERE` on indexed columns and at most one indexed join. No `ORDER BY`, `LIMIT`, `GROUP BY`, `LIKE` or arithmetic.
- **[Views](https://spacetimedb.com/docs/functions/views) are subscribable read functions.** A view takes no arguments beyond its context, reads only through indexes, returns rows, and the host re-evaluates it when its read set changes. A per-caller view sees `ctx.sender()`, so "what am I looking at" is read from a table the caller wrote to.
- **[Procedures](https://spacetimedb.com/docs/functions/procedures) return values to one caller.** They may make outbound HTTP calls and must open their own transaction to touch tables. In Rust they sit behind the `unstable` feature flag.
- **Schema changes are additive.** Publish applies new tables, indexes, reducers and trailing columns with defaults [on its own](https://spacetimedb.com/docs/databases/automatic-migrations). It refuses to remove or retype a column or drop a non-empty table. The documented pattern for anything else is a [new table with lazy row copy](https://spacetimedb.com/docs/databases/incremental-migrations).

Two consequences drive everything below. Aggregates are rows that reducers maintain, never something computed at read time. Ordering comes from index order, so every sort key is an integer column with an index.

## Shape of the mode

```
Telegram ──► archiver (Python, unchanged code paths)
                │  STDBAdapter: same DatabaseAdapter surface
                │  writes = reducer calls over HTTP with the archiver token
                ▼
          SpacetimeDB standalone ◄──── module.wasm (Rust, built in CI, published at start)
                ▲            ▲
                │            │ WebSocket subscriptions, views, procedures
                │            │ (short-lived token minted by the viewer)
   viewer (FastAPI):         browser (Vue page + vendored TypeScript SDK bundle)
   login, token minting,
   media bytes from disk,
   export, push notifications
```

Three things stay in Python because SpacetimeDB does not do them: talking to Telegram, serving media bytes from disk, and the viewer's own login. Everything that is "read rows, order them, push changes" moves to the module and the browser.

The archiver's realtime notifier becomes a no-op in this mode. The commit is the event.

## Rules we follow

These are the [SpacetimeDB principles](https://spacetimedb.com/docs/intro/zen) applied to an archive.

1. **Everything is a table.** No aggregate cache in Python, no derived state outside the database. Per-chat counts, last-message pointers, per-day counts and global totals are rows updated inside the same transaction as the write that changes them.
2. **State the client would pass as an argument goes in a table.** Views cannot take arguments, so the open chat is a `viewer_focus` row written by the browser through a reducer and read by a per-caller view.
3. **Never poll.** The browser holds subscriptions; history pages come from procedures; nothing refetches on a timer.
4. **Tables are [private](https://spacetimedb.com/docs/tables/access-permissions). Reads go through views.** Row-level security exists but is experimental and the vendor steers away from it. Entitlement is a `viewer_grant` table checked in views and procedures.
5. **Tables are append-only in shape.** New needs get new tables or trailing defaulted columns. Nullable columns that need an index use a sentinel value instead of `Option`.
6. **Media bytes stay on disk.** The module holds media metadata and the file path. Inline blobs would live in RAM.
7. **Composite keys become a surrogate key plus a multi-column index.** SpacetimeDB has single-column primary keys only. The inserting reducer enforces uniqueness of `(account_id, chat_id, message_id)`.

## Module schema

All keys are `u64` auto-increment surrogates. Timestamps used for filtering or sorting are `i64` microseconds since the epoch, suffixed `_us`; audit timestamps that are never filtered use the native `Timestamp`. Because index scans have no descending direction, tables that page newest-first also carry `date_us_desc = i64::MAX - date_us`.

| Table | Key columns and notable columns | Indexes |
|---|---|---|
| `account` | `key`, `telegram_user_id` (0 = unset), `label` | `telegram_user_id` |
| `chat` | `key`, `account_id`, `chat_id`, `ref` (unique), `kind` enum, names, `is_forum`, `is_archived`, `last_synced_message_id` | `(account_id, chat_id)`, `(account_id, username)` |
| `message` | `key`, `chat_key`, `account_id`, `chat_id`, `msg_id`, `sender_id`, `sender_name`, `date_us`, `date_us_desc`, `edit_date_us`, `text`, `reply_to_msg_id`, `reply_to_top_id`, `reply_to_text`, `forward_from_id`, `is_outgoing`, `is_pinned`, `is_deleted`, `deleted_at_us` | `(chat_key, msg_id)`, `(chat_key, date_us_desc)`, `(account_id, msg_id)`, `(chat_key, reply_to_top_id, date_us_desc)`, `(chat_key, reply_to_msg_id)` |
| `message_raw` | `msg_key`, `raw_data` (JSON string) | `msg_key` |
| `message_version` | `key`, `msg_key`, `text`, `date_us`, `captured_us`, `captured_us_desc` | `(msg_key, date_us)`, `captured_us_desc` |
| `tg_user` | `user_id` (Telegram id, global), names, `is_bot` | primary key only |
| `media` | `key`, `msg_key`, `chat_key`, `msg_id`, `kind` enum, `file_path`, `file_name`, `mime_type`, `file_size`, dimensions, `content_hash`, `downloaded`, `download_attempts`, `date_us`, `sender_name` | `msg_key`, `(chat_key, downloaded, msg_id)`, `(chat_key, kind)`, `(account_id, content_hash)`, `(account_id, downloaded, download_attempts)` |
| `reaction` | `key`, `msg_key`, `chat_key`, `emoji`, `count`, `removed_at_us` | `(msg_key, emoji)`, `chat_key` |
| `sync_status`, `forum_topic`, `chat_folder`, `chat_folder_member`, `metadata`, `app_setting` | as today, keyed by surrogate or string key | by `(account_id, chat_id)`, `(chat_key, topic_id)`, folder and chat keys |

Rows the reducers maintain so nothing aggregates at read time:

| Table | Content | Replaces |
|---|---|---|
| `chat_summary` | `chat_key`, `account_id`, `message_count`, `deleted_count`, `media_count`, `media_bytes`, `pinned_count`, `first_us`, `last_us`, `last_us_desc`, `last_msg_key`, `displayed_copy` | the correlated `MAX(date)` in the chat list, per-chat stats, folding |
| `chat_media_count` | `chat_key`, `kind`, `count` | the media type chips |
| `topic_summary` | `chat_key`, `topic_id`, `count`, `last_us` | the topics `GROUP BY` |
| `day_count` | `chat_key`, `day`, `count` | the calendar probes |
| `archive_stats` | one row of global totals | the daily statistics job |
| `pinned_message` | `msg_key`, `chat_key` | the pinned filter |
| `change_event` | `seq`, `kind`, `chat_key`, `msg_key`, `captured_us_desc`, `old_text`, `new_text` | the changes feed |
| `viewer_focus` | `identity` (primary key), `chat_key`, `topic_id`, `floor_msg_id` | the "which chat is open" argument |
| `viewer_grant` | `identity`, `account_id`, `chat_id` (0 = whole account) | the viewer's chat scope |
| `writer` | the archiver identity allowed to call write reducers | |
| `schema_version` | one row: module version, last applied data migration | Alembic's version table |

`message_raw` is a separate table so the hot `message` rows stay small. It carries the Telethon JSON the archiver needs for migration markers and album grouping.

### Search

There is no text index of any kind. The module-native shape is an inverted index written by the ingest reducer with the same word-prefix semantics the app uses today: every query word is a required prefix.

```rust
#[spacetimedb::table(accessor = message_token,
    index(accessor = by_token, btree(columns = [token, date_us_desc])))]
pub struct MessageToken { token: String, chat_key: u64, date_us_desc: i64, msg_key: u64 }
```

A `search_messages(q, limit, offset)` procedure range-scans each word as a string prefix, intersects the posting sets, filters by the caller's grants and returns hydrated rows. The reducer that inserts or edits a message writes and deletes its token rows in the same transaction.

The cost is memory. Tokens and their index entries take roughly 0.5 to 2 KB of resident RAM per message, more than the message itself. So search is a module feature flag, off in the first experiment. The alternative that keeps SpacetimeDB memory flat is a Python sidecar that feeds an SQLite FTS5 table from a `message` subscription. It works, but it puts derived state outside the database, which is the one thing this mode is trying not to do.

## Reducers

One reducer per archiver write path. Every reducer checks `ctx.sender()` against the `writer` table and returns `Err` otherwise. A batch is one transaction.

| Archiver call today | Reducer | Notes |
|---|---|---|
| `ensure_account` | `ensure_account(telegram_user_id, label)` | find by index, insert or relabel |
| `upsert_chat` | `upsert_chat(account_id, ChatIn)` | `Option` fields mean "not provided"; mints `ref` on insert; creates the `chat_summary` row |
| `upsert_user` | `upsert_user(UserIn)` | |
| `insert_message`, `insert_messages_batch`, `insert_media`, `reconcile_reactions` | `ingest_messages(account_id, chat_id, Vec<MessageIn>)` | the listener sends a one-element batch; per element: probe `(chat_key, msg_id)`, insert or apply the existing upsert policy (text gated by edit evidence, sender name immutable once set, raw data never blanked), write a version row on text change, update summaries, tokens, media and reactions |
| `update_message_text` | `apply_edit(account_id, chat_id, msg_id, text, edit_date_us, entities)` | version row on change; `change_event` row |
| `mark_message_deleted`, `delete_message` | `tombstone_message(...)`, `purge_message(...)` | tombstone keeps the first `deleted_at`; purge cascades by `msg_key`; both write a `change_event` row only when the row existed |
| `update_message_pinned`, `sync_pinned_messages` | `set_pinned(...)`, `sync_pinned(account_id, chat_id, Vec<i64>)` | |
| `update_sync_status`, `reset_chat_sync_cursor` | `advance_sync(...)`, `reset_sync_cursor(chat_id)` | high-water clamp as today |
| media path, attempts, retype, reconcile | `media_set_path`, `media_attempt_failed`, `media_mark_redownload`, `media_retype`, `media_reconcile_kind` | media rows are addressed by `media.key` |
| `delete_media_for_chat`, `delete_media_records`, `delete_voice_note_audio_twins`, `delete_chat_and_related_data` | `purge_media_for_chat`, `purge_media(keys)`, `dedupe_voice_twins`, `purge_chat` | file deletion stays in Python after the reducer commits |
| topics, folders, metadata, settings | same names | |
| `calculate_and_store_statistics` | none | `archive_stats` and `chat_summary` are always current; a daily scheduled reducer recounts and logs drift as a self-check |
| viewer-side writes (`open_chat`, grants) | `open_chat(chat_ref, topic_id)`, `set_grants(subject, Vec<Grant>)` | `open_chat` verifies the grant and computes `floor_msg_id`, the id of the 50th newest message |

Deletion snapshots for the outgoing webhook come back as return values today. Here they become `change_event` rows the viewer subscribes to.

## Reads

Every viewer read maps to one of four mechanisms: a subscription the browser holds, a view, a procedure that walks an index range, or a summary row.

| Viewer read | Mechanism |
|---|---|
| chat list, ordered by last message, with name filter and folder or archived filters | subscription to `my_chats` (a per-caller view over `chat_summary` joined to `viewer_grant`); ordering, name filter and paging happen in the browser cache. The list is at most a few thousand rows. |
| chat by ref | keyed lookup on the unique `ref` index |
| the open chat: newest 50 messages, then live | reducer `open_chat` writes `viewer_focus`; per-caller view `my_open_chat` returns `message` rows with `msg_id >= floor_msg_id` for that chat; the browser subscribes once and receives inserts, edits, tombstones and pins through it. Subscribe to the new focus before dropping the old one when switching chats. |
| older pages, jump to message, load newer | procedure `messages_before(chat_key, before_id, limit)` and an `after_id` variant, walking `(chat_key, msg_id)`; results are appended locally and are not part of the live replica |
| media, reactions, versions, users for the open window | subscriptions filtered by `chat_key`, plus a two-table join for `tg_user` rows referenced by the window |
| pinned, topics, folders | subscriptions to `pinned_message`, `topic_summary` and `forum_topic`, folder tables |
| per-chat stats, media type counts, global stats | `chat_summary`, `chat_media_count`, `archive_stats` rows |
| dates and by-date jump | procedure doing index seeks on `(chat_key, date_us_desc)` and `day_count` |
| changes feed | subscription or procedure over `change_event` |
| media gallery | procedure `page_media(chat_key, kinds, cursor, limit)` on `(chat_key, downloaded, msg_id)` |
| search | procedure `search_messages` (feature flag) |
| export | procedure `export_page(chat_key, from_us, to_us, after_id, limit)` looped by Python |
| media bytes | Python, from disk, after a keyed `media` lookup |

What has no equivalent and stays out of this mode: substring search with `ILIKE`, the database size endpoint, and any read that today relies on `OFFSET` over an unbounded sort.

## Python side

A new `STDBAdapter` implements the same `DatabaseAdapter` method names behind the existing [`init_database`](../src/db/__init__.py) entry point, selected by a `spacetimedb://host/db-name` database URL and an archiver token from the environment. The listener, the backup pass and the viewer keep their call sites.

- **Writes** are [`POST /v1/database/<db>/call/<reducer>`](https://spacetimedb.com/docs/http/database) with a JSON array of arguments and the archiver's bearer token. A reducer failure comes back as HTTP 530 with the error string and is raised as an exception.
- **Batch size.** The call endpoint has a request body cap of about 2 MiB. Message batches carrying raw Telethon JSON must stay well under that, which the default batch of 100 does.
- **Reads the archiver still needs** (sync cursors, last message id, media by hash, migration markers) are `POST /sql` with `WHERE` on indexed columns, or small procedures.
- **Latency.** Each HTTP call runs the module's `client_connected` and `client_disconnected` hooks. That is acceptable for the listener's one-message-at-a-time path and the backup pass. If it ever matters, the follow-up is a small WebSocket client speaking the `v1.json` protocol; note that the JSON protocol cannot receive event tables, so the module must not depend on them.
- **The viewer process** keeps login, sessions, the token route below, media bytes, export and push notifications. Its `/ws/updates` socket, the internal push endpoint and the realtime listener are unused in this mode.

There is no Python SDK. The adapter is a thin HTTP client plus SATS-JSON decoding against the schema fetched once from `/schema`. Integers above 2^53 must be decoded from the raw text, never through a float.

## Browser side

The Vue page gets a second data layer, active only in this mode.

- **Bundle.** Generate TypeScript bindings with `spacetime generate`, bundle them with the SDK's browser build into one IIFE file, and vendor it next to the Vue bundle under [`src/web/static/vendor/`](../src/web/static/vendor/) with the app version in the name. The SDK ships ESM only, so this bundling step is new. Every module publish that changes tables ships a matching bundle in the same release.
- **Connection.** `GET /api/stdb/token` on the viewer returns a one-hour token for the logged-in principal. The page connects with it and re-fetches before expiry. Nothing is written to local storage.
- **Subscriptions.** `my_chats`, then per open chat `my_open_chat` plus the filtered `media`, `reaction`, `message_version` and `tg_user` queries. Row callbacks push into the existing `ref()`s; the page's rendering code does not change.
- **What goes away in this mode:** the relay socket client and its reconnect, the 3-second poll and its reconciliation, the `after_id` newer-messages fetch, and the server-side connection manager and internal push route.

## Auth

The viewer already is the identity authority, so it becomes the token issuer.

- The operator generates one ES256 key pair for the SpacetimeDB server. SpacetimeDB accepts any token signed by its own private key regardless of issuer, and derives the identity from `iss` plus `sub`.
- The key is mounted read-only into the viewer and into the publish job. The viewer mints `{"iss":"telegram-archive","sub":"<viewer username>","exp":<now+1h>}`. The archiver and the publish job use long-lived tokens with `sub` values `archiver` and `publisher`.
- The module's `client_connected` reducer returns `Err` unless the token's issuer is `telegram-archive`. That also rejects tokens the server issues to anonymous callers.
- `viewer_grant` mirrors the viewer's chat scope. The viewer calls `set_grants` when a session's entitlement is created or changed and refuses to mint a token until the reducer acknowledged.
- The standalone server should run with `TEMP_SPACETIMEAUTH_ISSUER_REQUIRED_TO_PUBLISH=telegram-archive`, so publish is not open to anyone who can reach the port.

A compromised viewer container can mint any identity. It already holds every session today; in this mode it also holds the database key, which is the price of not running a separate identity provider.

## Migrations without Alembic

The Rust structs are the schema. `spacetime publish` diffs the new module against the running one and applies the change or refuses.

**Publish applies on its own:** new tables, new indexes, added or changed reducers, views and procedures, appended columns that carry a `#[default]`, a new unique constraint (the server scans existing rows and refuses on duplicates), and dropping empty tables.

**Publish refuses:** removing or retyping a column, reordering columns, dropping a table that has rows, changing a primary key. Appending a column rewrites the table and disconnects every client, so the publish job must pass `--yes=break-clients` and the browser must reconnect.

**The pattern for a refused change** is the vendor's [incremental migration](https://spacetimedb.com/docs/databases/incremental-migrations): add `message_v2` with the new shape, make every reducer read v2 first and fall back to v1 with a lazy copy, dual-write during the transition, and drop v1 in a later release once it is empty. There is no downgrade. Rolling back is publishing a newer module whose tables are a superset of the current ones.

**How past Alembic revisions would look here:**

| Past revision | Category | In this mode |
|---|---|---|
| add `is_pinned` with default | trailing defaulted column | `#[default(false)] is_pinned: bool` appended; publish with break-clients |
| add nullable `sender_name` | nullable column | `#[default(None)] sender_name: Option<String>`; every added column needs a default |
| add or drop an index | index | change the table attribute; plain publish |
| drop media columns from messages | drop column | refused; ship the new table or keep the columns unused |
| widen types, add NOT NULL | type change | refused; design columns non-optional and `i64` from day one |
| re-key every table by account | re-key | not needed; keys are surrogates and `account_id` is an index column |
| rewrite file paths, delete phantom rows | data fix | an idempotent `migrate_<n>` reducer called once by the publish job, guarded by `schema_version` |

**Deployment.** The module crate lives at `spacetimedb/` in this repository with its version pinned to the app version and checked by the release script. CI builds `module.wasm` once (it is architecture independent) and copies it into the archiver image. A one-shot `stdb-publish` service in [`docker-compose.yml`](../docker-compose.yml) replaces `alembic upgrade head`: it publishes the module with the publisher token, then calls `migrate` with the stored and compiled versions. The archiver and viewer depend on it completing. A refused publish exits non-zero and the app containers do not start; the fix is always a new module version, never a hand edit of the database.

## Importing an existing archive

A one-off importer reads the SQLite or PostgreSQL archive with the existing adapter and calls `ingest_messages` and the other reducers in batches, in dependency order: accounts, users, chats, messages, media, reactions, versions, topics, folders. Over HTTP, batches stay under the body cap; a Rust importer using the official client SDK over WebSocket can send much larger batches.

Three facts shape the import:

- **The commit log stores each reducer's arguments and every row it wrote**, so an import writes each row roughly twice. Budget about two to three times the row bytes in disk, permanently.
- **Snapshots happen every million transactions or on every commit log segment rotation.** A batched import is only thousands of transactions, so set `commitlog.max-segment-size` in [`config.toml`](https://spacetimedb.com/docs/cli-reference/standalone-config) low (for example 64 MiB) for the import and the first weeks. Otherwise every restart replays the whole import from the log.
- **Verification** is `SELECT COUNT(*)` per table against the source, then per-chat counts and last ids through a `verify_chat` procedure. There is no `GROUP BY`.

## Operations

```yaml
services:
  stdb:
    profiles: [stdb]
    image: clockworklabs/spacetime:v2.10.1
    command: ["start", "--listen-addr=0.0.0.0:3000",
              "--jwt-priv-key-path=/etc/spacetimedb/id_ecdsa",
              "--jwt-pub-key-path=/etc/spacetimedb/id_ecdsa.pub"]
    environment:
      TEMP_SPACETIMEAUTH_ISSUER_REQUIRED_TO_PUBLISH: telegram-archive
    expose: ["3000"]          # never `ports:`; the browser reaches it through the viewer's proxy
    volumes:
      - ./data/stdb:/home/spacetime/.local/share/spacetime/data
      - ./data/stdb-keys:/etc/spacetimedb:ro
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:3000/health"]
      interval: 15s
      timeout: 5s
      start_period: 60s
    mem_limit: <sized to the archive>
```

- **Image.** [`clockworklabs/spacetime:v2.10.1`](https://hub.docker.com/r/clockworklabs/spacetime) exists for amd64 and arm64. The module is WebAssembly and runs on both.
- **Network.** The browser needs the subscribe endpoint and the token exchange endpoint only, which is also what the vendor's [self-hosting guide](https://spacetimedb.com/docs/how-to/deploy/self-hosting) exposes. The viewer proxies those two paths and nothing else. Port 3000 stays inside the compose network.
- **Backup.** There is no backup, dump or restore command. A backup is a copy of the data directory (`control-db`, `program-bytes`, `replicas/<id>/clog`, `replicas/<id>/snapshots`) taken while the server is stopped. Restore is stop, replace the directory, start. The commit log grows forever, and the only logical export is replaying rows through a client.
- **Memory.** Everything resident. Estimate before measuring: about 1 GB per million messages for rows plus the indexes above without raw Telethon JSON and without search; raw JSON adds its own size; the token index adds 0.5 to 2 GB per million. Set `mem_limit` to about twice the expected resident set and measure on the first import.
- **Client idle timeout** defaults to 30 seconds without a ping reply; a background tab that stops answering pings is dropped and must reconnect.

## Limits and risks

- **RAM is the budget.** The archive's message rows, every index, the token index and every summary row are resident. A large archive on a small box does not fit. Disk-backed tables are announced by the vendor but not in this release.
- **Procedures, module HTTP handlers and row-level security are unstable or beta.** History paging, search and export depend on procedures.
- **No descending index scans.** The `_desc` mirror columns cost 8 bytes per row per sort key and must be kept consistent by every reducer that touches the row.
- **Composite uniqueness is code, not a constraint.** A reducer bug can create a duplicate `(account_id, chat_id, msg_id)` that SQL would have rejected.
- **Every column add disconnects all clients.** Fine for a family deployment; it must be expected.
- **The JSON WebSocket protocol is the oldest of four** and carries a breaking-change note in the source. Pin the server version and treat a bump as a release task.
- **No backup tooling, no log pruning.** Operators copy directories.
- **Licensing.** SpacetimeDB is [Business Source License 1.1](https://github.com/clockworklabs/SpacetimeDB/blob/v2.10.1/LICENSE.txt) with a grant of one production instance per licensee, changing to AGPL-3.0 with a linking exception in September 2031. The Rust bindings crates the module links are Apache-2.0, so the module itself is a clean GPL-3.0 crate. The TypeScript SDK bundle vendored into the page is BSL code inside a GPL-3.0 tree; BSL permits redistribution, and the repository must carry its license text next to the bundle.
- **Restricted viewers** need `viewer_grant`, per-caller views and grant checks in every procedure. The first experiment runs with a single full-access viewer.

## Reopen when

All of these are true for one self-hosted release:

- Tables can be declared as disk-backed, the working set is bounded by a configurable cache rather than by the table size, and this is available in the standalone server, not only on the vendor's cloud.
- The memory section below is rewritten with the new residency rule, and the sizing estimate is replaced by a measurement from step 1 of the plan.
- Procedures are no longer behind the `unstable` feature, or we accept the flag knowingly.
- The v2.10.1 facts this doc rests on are re-verified: single-column primary keys, argument-less views, no descending index scans, the automatic migration rules, the `/call` body cap, the commit log layout.

## Plan for the first experiment

Each step ends with a measurement. Any step can stop the experiment, and that is fine.

1. **Module and import on a scratch box.** Write the schema and `ingest_messages`, publish to a standalone container, import a synthetic archive of one million messages over WebSocket, read resident memory and restart time. Go if resident memory per million messages is within the estimate above.
2. **Archiver writes.** Implement `STDBAdapter` for the write methods and the handful of archiver reads, run the listener and one backup pass against the scratch box. Go if a full backup cycle completes with matching counts.
3. **Reactive open chat.** Add the token route, the vendored bundle, `my_chats` and `my_open_chat`. Go if a message captured by the listener appears in the browser with no poll and survives a viewer restart.
4. **History and galleries.** Procedures for older pages, by-date, media, changes, export.
5. **Search** behind the flag, measured separately.

Until step 3 passes, none of this ships in a release.

## Open questions, answerable on the first publish

- Whether a `Timestamp` column can carry an index in this release. The docs say no; the bindings do not forbid it. The design uses `i64` microseconds either way.
- Whether a String prefix range on a multi-column index behaves as expected from module code.
- Whether the browser cache stays smooth with a window of several thousand rows plus media and reactions.
- Measured bytes per message row, per token row and per index entry on real data.
