# Autopilot Worklog

Append-only. Newest at the bottom. Every "done" carries evidence (tests/CI/commit).

Loop: `/research! → /implement! → /review-pr! → (back to research)` · solo · repo: Telegram-Archive

---

### 2026-07-06 — kickoff
- Goal captured in `docs/GOAL.md`: back up a folder's FULL membership (pinned_peers + flag-based inclusion), not just `include_peers`, so folders hidden by the #208 fix show their archived chats.
- Baseline: main at v7.19.1 (`a878fe5`); PR #209 (#208 empty-folder fix) merged + released this session.
- Current `_backup_folders` (src/telegram_backup.py:2468) resolves only `include_peers` → `sync_folder_members`. Ignores `pinned_peers`, `exclude_peers`, and flags (contacts/non_contacts/groups/broadcasts/bots + exclude_muted/exclude_read/exclude_archived).
- Next: research Telethon `DialogFilter`/`DialogFilterChatlist` schema + our Chat/User metadata (what we can evaluate flags against), then implement a pure membership-resolution helper.

### 2026-07-06 — research (primary sources)
- **Telethon `DialogFilter`** (vendored `telethon/tl/types`): `id,title,pinned_peers,include_peers,exclude_peers, contacts,non_contacts,groups,broadcasts,bots, exclude_muted,exclude_read,exclude_archived, emoticon,color`. **`DialogFilterChatlist`** = shareable, include-only: `pinned_peers,include_peers` (NO flags, NO exclude_peers). **`DialogFilterDefault`** = the "All" filter (skipped).
- **Our stored `chat.type` taxonomy** (`_extract_chat_data`, telegram_backup.py:2213): only **`private` / `group` / `channel`** — megagroups saved as `group`, bots saved as `private` (no `bot`/`supergroup` type). `chats` has `is_archived`; no mute/unread/contact columns. `users` has `is_bot`.
- **Evaluability**: groups→type group, broadcasts→type channel, exclude_archived→is_archived (all direct). bots→private + users.is_bot (join). contacts/non_contacts→private split by one `GetContacts(hash=0)` call. exclude_muted/exclude_read→not archived → best-effort NOT applied (documented; errs toward showing the folder).
- **Design**: pure resolver `resolve_folder_member_ids(filter, chats, contact_ids)` (unit-testable) + `_backup_folders` gathers archived chats (id,type,is_bot,is_archived) once + contact ids once, resolves pinned∪include∪flag-matches − exclude_peers per folder. No schema migration.
- Next: verify Telegram membership precedence (do explicit include/pinned override exclude_* state flags? does exclude_peers override include?) then implement.

### 2026-07-06 — implement + verify
- **Semantics confirmed** against canonical TDLib (`need_dialog`) + Telegram Desktop (`ChatFilter::contains`) source: precedence is explicit `(pinned∪include)` → `exclude_peers` → category gate → `exclude_*` state flags; explicit peers dominate; bot matches `bots` only; `groups` = basic+super; chatlist = pure allowlist. exclude_muted/read bypassed by unread mentions (moot — we don't apply them).
- **New `src/folder_utils.py`** — pure, dependency-free resolver `resolve_folder_member_ids(rules, chats, contact_ids)` + `FolderChat`/`FolderRules` dataclasses. Explicit peers pass through (existence-filtered downstream by `sync_folder_members`); flag matches drawn from archived chats; exclude_archived applied; exclude_muted/read documented not-applied.
- **`adapter.get_chats_for_folder_resolution()`** — archived chats + is_bot via LEFT JOIN users (no schema change).
- **`_backup_folders`** rewritten: fetch archived-chat snapshot once + contacts lazily (only if a folder uses contacts/non_contacts), resolve pinned/include/exclude + flags per folder, always `sync_folder_members` (empties stale folders). Factored `_resolve_peer_ids`, `_folder_rules_from_filter`, `_get_contact_ids`.
- **Tests**: 19 pure resolver tests (tests/test_folder_utils.py), adapter test for the new method, rewritten TestBackupFolders + new pinned/exclude/contacts/no-contact-fetch cases.
- **Evidence**: full suite **2015 passed / 0 failed** (Mac via machost); `ruff check` + `ruff format --check` clean; py_compile clean (3.14). No migration.
- Next: commit → PR → /review-pr! → merge → release.

### 2026-07-06 — review + fixes (PR #210)
- Ran 3 adversarial reviewers + CodeRabbit. No blockers; core precedence/taxonomy/peer/contact all CLEAR, new member set a strict superset of the old path.
- **Fixed (real bug)**: `sync_folder_members` chunks its existence `IN()` check (dedup + 500-batch) — flag folders can now resolve to >32k members, which would have exceeded SQLite/PG bind-param caps and silently failed the sync.
- **Fixed (self)**: resolve own id once (`_get_own_id`), map `InputPeerSelf` (pinned Saved Messages) → own id, and count self as a contact — closes the two reviewers' self/Saved-Messages findings.
- **Fixed (perf)**: chat snapshot now fetched lazily on the first real folder (accounts with only the "All" filter pay nothing); snapshot + contacts fetched once per run (CodeRabbit).
- **Tests**: new `tests/test_folder_resolution_integration.py` (real in-memory SQLite) executes the users outer join + proves the chunking across 3 chunks with dedup/existence filtering + empty-clear; new helper tests cover real `_get_contact_ids`/`_get_own_id` bodies, `InputPeerSelf`→own-id, once-per-run fetch, and chatlist getattr defaults.
- **Evidence**: full suite **2025 passed / 0 failed**; ruff clean; py_compile clean (3.14).
- Next: push fix commit → re-verify CI/CodeRabbit → merge → release.

### 2026-07-06 — merged + released
- Adversarial verify of the fix commit: **all 5 items CONFIRMED-CLEAN**, no regressions (bonus: dedup also prevents a composite-PK IntegrityError on duplicate peers). CodeRabbit's one remaining nitpick (mock-spec consistency) applied.
- **PR #210 merged** (squash `1f1ba3e`). **Released v7.20.0** (minor — new capability): commit `a56440a`, tag `v7.20.0`, changelog entry crediting #208/#210. Docker images building.
- **GOAL HELD**: folders defined by pins/flags now resolve full membership against archived chats and show in the viewer. Loop complete for this goal.

### 2026-07-14 — #212 studied + implemented (PR #215, NOT released)
- /research! 6-agent panel: truncation is right, but static-via-pathconf is WRONG (pathconf reports 255 over CIFS→Synology; confirmed eCryptfs Launchpad #885744). User chose Option D (deterministic conservative byte budget + hash fallback) + retry-cap in same PR.
- Implemented: build_media_filename (byte-aware, ext-preserving, codepoint-safe, deterministic; message_utils.py) wired into both _get_media_filename sites; MEDIA_MAX_FILENAME_BYTES=143 default. Retry cap: media.download_attempts (migration 016 + entrypoint stamping PG+SQLite), MEDIA_MAX_DOWNLOAD_ATTEMPTS=5, get_pending excludes capped rows, retry loop increments on failure.
- 3-agent review: no blockers. Landed fixes: mark_media_for_redownload resets attempts (recovery path); capped-count WARNING (no silent loss). Reverted a fallback-reserve tweak after analysis showed it breaks the hash+ext tier for no real gain (branch only reachable at sub-reserve misconfig where temp-safety is impossible anyway).
- Evidence: full suite 2052 passed; ruff/shellcheck clean; CI green. PR #215 open, NOT merged, NOT released (batching #213/#214/#215 into one release on Sergio's go).
