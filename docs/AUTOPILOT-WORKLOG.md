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

### 2026-07-20 — kickoff: issue #224 (resweep FloodWait pacing)
- Directive appended to GOAL.md verbatim. Loop: /research! -> /implement! -> /review-pr! -> merge -> release (v7.24.1 patch) -> deploy+test on geiserback -> answer/close/thank.
- #224 (Igor, v7.24.0): getMessagesReactions burst limit ~4-5 req refilling ~60s, cross-chat; per-chat cap can't express it; MAX_PER_CHAT=2000 burned all 5 retries on dialog 1 (385s). Fix menu: inter-batch delay (default small), global per-sweep budget, skip-remainder-after-first-flood. Also fix retry log label ("<TelegramClient object>").
- NOTE: GOAL.md/AUTOPILOT-WORKLOG.md are untracked local docs — NEVER git add (bit us on PR #220).

### 2026-07-20 — #224 researched + implemented (PR #225 open)
- /research! 4-agent panel: flood buckets per-(account,method) (Telethon+gotd both key caches by constructor id — cross-chat accumulation CONFIRMED); official docs say >=15s for this method family; Telethon's own sibling by-ids method paces 10s/batch; retrying into a live flood window compounds penalties. CRITIC caught 3 design flaws BEFORE build: skip-after-flood was unimplementable (flood swallowed by blanket except -> falls to 2nd-bucket fallback with 5 more retries), per-chunk delay never fires for the 1-chunk-per-chat trip pattern (must be GLOBAL spacing), positional fairness cursor meaningless (recency re-sort) -> chat-keyed cycle cursor; global budget knob cut as redundant.
- Implemented: REACTION_RESWEEP_BATCH_DELAY_SECONDS (default 2, global spacing across chats via per-run monotonic ts); raw request UNWRAPPED with FloodWaitError->defer-rest-of-run (no fallback on flood); fallback paced + max_retries=1 + flood->defer; chat-keyed cycle cursor in metadata (persist on flooded run, clear on clean; self-healing if every run floods); backup_all load/finalize hooks; README/.env/compose docs. The "<TelegramClient object>" log wart gone (call site no longer wrapped).
- Evidence: 2262 passed (8 new pacing/deferral/cursor tests), ruff clean. PR #225 open; 5-reviewer panel + adversarial verify running; CI polling.
- OPS: geiserback HOST REBOOTED 2026-07-20 20:20 local (not us). sergio get_dialogs flood = 4 episodes since yesterday, ~9.7h each to exhaust 120 retries, wait flat ~26s, NO self-heal signal, reboot did not clear; listener unaffected (314 msgs/12h captured). annais healthy (18/18 clean cycles, separate account) -> live-test target for the pacing fix.

### 2026-07-20 — #224 reviewed + merged as v7.25.0
- 5-reviewer panel + adversarial verify: FIXED per-chat resume progress (chats larger than the flood bucket now guaranteed forward progress — was a coverage regression), symmetric fallback flood-deferral (no sleep-into-window), stamped cursor (days+48h freshness; disable/re-enable safe), finalize moved after dialog loops, gate order, key-pinned cursor test. DECLINED w/ reasoning: cross-process cursor races (best-effort over idempotent reconcile), crash-mid-loop cursor staleness (self-heals).
- Semver corrected by panel: new env var + changed defaults = MINOR -> v7.25.0 (not 7.24.1).
- CodeRabbit: initial review clean; round-2 commit rate-limited -> waited stated 1-min cooldown, triggered full review ONCE -> completed, zero findings.
- PR #225 squash-merged (main 71951a8), tag v7.25.0 pushed; 2267 tests green; release+images polling.
- NEXT: gitops v7.25.0 (4 refs) + REACTION_RESWEEP_DAYS=3 on annais (live stress test, 1992 dialogs) -> verify deploy -> observe paced resweep live -> answer/close #224 + thank Igor -> cancel.

### 2026-07-20 — #224 SHIPPED v7.25.0, live-validated, closed
- Deployed v7.25.0 (gitops 2090f90): 4 containers up, sessions authorized, viewers 200. Resweep now on BOTH instances (DAYS=3).
- LIVE VALIDATION (annais, ~2000 dialogs, first sweep): 27 resweep requests / 376 ids at an EXACT 2s cadence across consecutive chats; first FloodWait (7s) at 22:14:05 -> ONE warning + immediate deferral; zero "retrying <TelegramClient" signatures; cursor resumes deferred chats next hourly run. Bucket varies by account (ours ~27 spaced requests vs Igor's ~4-5 raw).
- #224 CLOSED (auto-closed by merge; thank-you comment 5026849806 posted).
- OPS STANDING ITEM for Sergio: sergio-instance get_dialogs flood — episode 5 started with the deploy restart (retry 1/120, wait ~25s flat). 5 episodes since 2026-07-19; ~9.7h/episode to exhaust; no self-heal signal; host also rebooted 2026-07-20 20:20 (unrelated). Listener unaffected throughout. Consider giving the account a quiet window (e.g. temporarily disable startup-backup/schedule on sergio) if it persists another day.

### 2026-07-21 — #224 follow-up (Igor's overnight data) -> PR #226
- Igor confirmed the deferral works exactly as designed BUT over-corrects at his scale: bucket ~4 requests (vs our 27) -> one run covered 2 chats, deferred 498; v7.24 covered all ~14 eligible in one run by sleeping ~120s. His middle ground: record flood expiry, resume within the same run (his run ~390s, wait 53s). Also found MAX_PER_CHAT=100 fully solves coverage on his account (12 chats, no floods, 264.69s vs 264s baseline); caveats noted (bucket degraded through his evening of restarts, trip condition not a simple request count).
- Acked on PR #225, REOPENED #224, implemented his proposal faithfully: _resweep_flood_until deadline (server seconds + 2s margin), pause (no sleep/retry/fallback), cooldown-skips go to cycle cursor, in-run resume when monotonic passes deadline, hard-defer after RESWEEP_MAX_FLOODS_PER_RUN=3 (degrading buckets), finalize keyed on exact _resweep_deferred_any. README sizing tip (lower cap = broader per-run reach). NO new env vars. v7.25.1 (patch: behavior fix, no knobs).
- 2269 tests green (new: in-run resume w/ controlled clock, hard-defer cap, cooldown-skip counting), ruff clean. PR #226 open; 3-reviewer panel + CI polling.

### 2026-07-21 — #224 follow-up SHIPPED v7.25.1 + live-proven; Pillow security PR #227 in flight
- PR #226 merged (main 36ce070) after: 3-reviewer panel (0 serious; 5 minors fixed incl. stale narratives + 2 new tests: PII log guard, double-flood offset advance), CodeRabbit clean twice (one 48-min rate-limit cooldown honored, single re-trigger; final env-table wording catch fixed), CI green. v7.25.1 released + deployed (gitops d49467d).
- LIVE PROOF on annais: post-deploy sweep completed clean (cursor {}), next hourly run inherited the hot bucket -> FloodWait(21s) at 13:00:46 -> "pausing" warning -> "resuming within this run" at 13:01:09 (21s+2s margin). Zero hard-defers, zero retry-burn. Yesterday the same event deferred everything. #224 final reply posted (comment 5033235039) + CLOSED.
- Dependabot (Sergio's link): 13 open alerts, ALL Pillow, all fixed by 12.3.0 (8 high: heap OOB writes, bomb-check bypasses — Pillow processes untrusted downloaded media). PR #227 open: floors ->=12.3.0, uv.lock regenerated (12.2.0->12.3.0), v7.25.2. CI green; CodeRabbit rate-limited -> persistent monitor armed (65-min anchor + early-recovery detection, single re-trigger).
- NEXT: on CodeRabbit pass -> merge #227 -> tag v7.25.2 -> images -> ONE deploy (also carries Pillow fix to prod) -> verify -> done. sergio get_dialogs flood still standing (listener fine).

### 2026-07-21 — Pillow security SHIPPED v7.25.2, deployed, all Dependabot alerts closed
- PR #227 merged on Sergio's explicit "just merge" (all real CI green; CodeRabbit rate-limit status outstanding — monitor stopped cleanly). Tag v7.25.2, release + images by CI.
- Deployed (gitops 247bf8c): 4 containers on v7.25.2, Pillow 12.3.0 CONFIRMED inside the running container, Géiser+Annais authorized, viewers 200.
- Dependabot open alerts: 13 -> 0 (all auto-closed by the uv.lock update on main).
- Day's tally: v7.25.1 (#224 in-run resume, live-proven, issue closed w/ thanks) + v7.25.2 (Pillow 12.3.0). Standing: sergio get_dialogs flood (listener unaffected).

### 2026-07-21 ~15:47 — sergio backup STOPPED for a quiet window (get_dialogs flood)
- Per Sergio: docker stop telegram-backup-sergio (Exited; restart policy unless-stopped keeps it down across daemon/host restarts). Viewer (8123) + postgres stay UP. Trade-off accepted: sergio's real-time listener is paused during the window; the first successful sweep catches up.
- Rationale: 4+ flood episodes since 2026-07-19, ~26s waits flat across 9.7h retry ladders, no self-heal signal, host reboot didn't clear it — the retries themselves likely kept the quota pinned. True zero-request window is the clean experiment.
- RESTART: one-shot Claude Code cron a0c6c5c8 fires 2026-07-22 04:23 local (~12.5h quiet) in the SAME session — steps: start container, verify get_dialogs outcome, report; if still flooded stop again + reschedule +12h. Cron is SESSION-ONLY: if that session closed, restart manually:
    ssh root@geiserback.mango-alpha.ts.net "docker start telegram-backup-sergio"
  then check: docker logs -f telegram-backup-sergio | grep -E "FloodWait|Backing up"

### 2026-07-23 — kickoff: issues #228 (migration silent-stop) + #229 (sender avatars)
- 8-researcher panel (1 critic failed structured-output; architect synthesis covered its ground). VERDICT: GO-MINIMAL both.
- #228 root cause CONFIRMED from vendored Telethon 1.43.2: events.ChatAction.build() has NO branch for MessageActionChatMigrateTo/ChannelMigrateFrom (chataction.py:68-107), NewMessage skips MessageService -> migration invisible to both live handlers; ONLY the sweep sees it. PREREQ: _process_message drops the pointer id today (MessageActionChatMigrateTo has .channel_id not .title). Detection = entity.migrated_to (primary, dialogs.py ignore_migrated=False keeps dead group visible) + stored-marker fallback. marked id = -(10**12+channel_id).
- #229: member avatars only 47.9% coincidental coverage (112/234 on annais; those are 1:1 contacts) — NO group-member download path exists. ACL (main.py:858-869) keys avatar_chat_id=split("_")[0] which for users/ IS the user_id, checked against user_chat_ids (chat ids) -> BLOCKS every member avatar (the #1 slice-2 blocker). Name palette hsl(h,70%,65%) FAILS white-text contrast on ~60-70% of hues -> use darker gradient hsl(h,65%,40%)->26% (contrast-proven all 360 hues).
- PLAN: #228 -> PR (enrich raw_data + always-WARNING + opt-in FOLLOW_CHAT_MIGRATIONS default OFF + viewer banner). #229 -> slice1 initials + slice2a serve-existing-files (ACL fix + sender_avatar_url + render), DEFER slice2b proactive member-avatar download (flood-sensitive; sergio is in a flood quiet-window). Two features -> minor releases.
- OPS: sergio STOPPED (flood quiet-window, cron restart ~04:23). annais = live-test target but CANNOT reproduce #228 (all-groups mode, zero migration rows) -> #228 validated by unit tests + synthetic fixtures. annais partially validates #229 render.
- Deferred (safe defaults taken): FOLLOW_CHAT_MIGRATIONS=off; sender-info popup deferred; slice2b download deferred.

### 2026-07-23 — #228 + #229 implemented, PR #230 open
- Two parallel executors (non-overlapping files), both green together: exec-228-backend (migration handling) + exec-229-viewer (avatars + banner). Shared adapter.py carried both methods cleanly (get_migration_markers:466, sender_has_message_in_chats:930 — no clobber). exec-229 tightened avatar contrast on request to 27%→18% (both stops ≥4.5:1 all 360 hues, ceiling-guard test).
- 3 commits on ai/issues-228-229 (2514022 #228 backend, 377221b #229 viewer, +bump 7.26.0). My verification: ruff clean, full suite 2320 passed (52 new tests). PR #230 open.
- 7-reviewer panel + adversarial verify + CI polling. Key scrutiny targets: ACL security boundary (member_ok fail-closed, membership query injection-safe), FOLLOW-off must not widen scope (allow-list contract), 4 sweep scope-injection sites consistency, sender_avatar_url per-message glob perf.

### 2026-07-23 — PR #230 review panel (7 reviewers + adversarial verify)
- NO critical/high survived. 2 "confirmed serious" → downgraded LOW by verifier (test-coverage gaps on correct code: sweep scope-injection + listener union not exercised end-to-end). Full-scan perf worry REFUTED as high (once/6h, dwarfed by sweep; real fix=index=schema change → declined).
- Dispatched fix round (both idle executors resumed): 
  * exec-228: FIX1 FOLLOW dead in whitelist mode (_should_process_chat MODE1 ignores followed set — store self._followed_live, allow in whitelist branch); FIX2 _reconcile suppress warning when new id type-in-scope (should_backup_chat) + whitelist fetch honor excludes; FIX3 integration test (followed id lands in dialogs across 3 sweep sites) + REAL listener union test (current one uses MagicMock config so branch degrades — passes even if union deleted).
  * exec-229: FIX1 loading=lazy on sender-avatar img; FIX2 getSenderInitials mirror getSenderName username/post_author fallback; FIX3 cache membership ACL probe + Cache-Control on avatar FileResponse; FIX4 direct sender_has_message_in_chats test + ACL allow-path endpoint test + /api/messages sender_avatar_url wiring test.
- CI green on PR #230 (all checks; CodeRabbit in progress). Igor uses include-list (type) mode so FIX1 whitelist gap didn't affect the reporter, but fixed for correctness.

### 2026-07-23 — PR #230 fix round committed (3195384)
- Both executors' fix rounds + CodeRabbit fixes landed. My verify: ruff clean, full suite 2344 passed (62 new tests total).
- CodeRabbit: fixed the 2 Majors (fail-closed membership probe on DB error; should_skip_topic mock guard) + the Minor (docstring); DECLINED the "Critical" except-clause (valid PEP 758 on py314 — CI/Dockerfile/ruff all target 3.14; CodeRabbit's own ast.parse passed) with evidence on the PR.
- Consolidated review reply posted (issue comment 5060390369). CI polling on final head; watch for CodeRabbit re-review + possible rate-limit on the new commit.
- NEXT: CI+CodeRabbit green -> merge -> tag v7.26.0 -> images -> gitops deploy -> verify annais (sergio OFF-LIMITS) -> answer+close #228 + #229 inviting Igor.

### 2026-07-23 — CodeQL py/path-injection FP hardened (Sergio: "harden the code")
- PR #230 CodeQL flagged HIGH py/path-injection at main.py:1042 (the FileResponse line exec-229 touched adding Cache-Control). VERIFIED FALSE POSITIVE: serve_media rejects ../absolute, resolve(strict), is_relative_to(media_root) containment — sound; CodeQL just doesn't model is_relative_to. Same rule already has 5 accepted open alerts on main. NOT a required check (PR MERGEABLE/UNSTABLE).
- Surfaced the resolution choice to Sergio (outward-facing public-repo security decision) via AskUserQuestion → chose HARDEN.
- Replaced is_relative_to with equivalent os.path.commonpath(realpath) containment (CodeQL-recognized barrier), guarding both direct + legacy-fallback resolution. No behavior change. 469 traversal/media/avatar tests pass; full suite 2344. Commit 67d5ac9 pushed. Polling CodeQL re-run.
- REMEMBER: dismissing security alerts / resolving public security-tab items = surface to Sergio, don't do unilaterally.

### 2026-07-23 — PR #230 MERGED as v7.26.0
- CodeQL py/path-injection FP: 3 hardening attempts (commonpath, startswith-on-value, post-construction header) didn't clear it — CodeQL re-flags serve_media's sink because the PR modified that function (avatar ACL), even with the FileResponse call byte-identical to main. main carries 5 accepted alerts of this rule; serve_media containment is sound (2344 tests incl traversal pass).
- Went back to Sergio (2nd AskUserQuestion) → chose "dismiss as FP then merge". Dismissed alert #41 (false positive, containment justification on the security tab). CodeQL check flipped to PASS. All 9 checks green, CLEAN.
- Final code state = clean is_relative_to containment + Cache-Control set post-construction (cleaner than the churned attempts). CodeRabbit rate-limited on the churn commits but status pass; substantive code already reviewed at 3195384.
- MERGED squash 37b5c77, main 7.26.0, tag v7.26.0 pushed. Release+images polling.
- NEXT: gitops v7.26.0 -> deploy -> verify annais + running set (sergio STILL stopped in flood quiet-window — cron restart ~04:23) -> answer+close #228 + #229 inviting Igor.

### 2026-07-23 — #228 + #229 SHIPPED v7.26.0, deployed, both issues CLOSED
- v7.26.0 deployed to geiserback (direct docker compose — Gitea git-http was dropping connections under watchtower load 6.79; deployed compose persisted at v7.26.0 on disk). All 4 containers v7.26.0, both sessions authorized, viewers 200, annais listener capturing (151 events/3min). #228 + #229 code confirmed live in the running containers.
- #228 answered+CLOSED (auto-closed on PR merge; comment 5063097763). #229 answered+CLOSED (comment 5063098041). Both thank Igor + invite to test.
- ALL 10 PRD stories pass. sergio-loop goal met: research→implement→review→release→deploy→verify→answer→close.
- OPEN ITEMS FOR SERGIO (surfaced): 
  1. sergio get_dialogs FLOOD PERSISTS after ~2 DAYS of quiet (container was Exited 2 days — the earlier quiet-window cron was session-scoped and never fired). On restart it floods again immediately (wait=26s, retry ladder). This is NOT a decaying FloodWait — it's a persistent account-level get_dialogs restriction. Listener capture is UNAFFECTED (real-time works). Real fix = WHITELIST mode (CHAT_IDS) which skips get_dialogs entirely, or accept listener-only for sergio. NEEDS SERGIO DECISION.
  2. gitops repo (giteaer/geiserback) still at v7.25.2 — reconcile to v7.26.0 when Gitea git-http recovers (watchtower load). Deployed state is already v7.26.0; only the source-of-truth repo lags.

### 2026-07-25 — NEW LOOP: issues #232 + #234 (reporter @Sasha50701)
- Vetted both for AI slop: NOT slop. Author = 6y-old account; self-closed #233 after own A/B disproved its premise (anti-slop signal). All code citations verified against dc6504d:
  * #232: config.py:98+:759 hardcode flood_sleep_threshold=0; telethon users.py:118-125 floors seconds to 1 then raises when > threshold → mid-media FloodWait aborts download_media; telegram_backup.py:2391 os.remove(tmp_path) → every retry restarts from byte 0. Large files (> one burst window) can NEVER complete. REAL BUG.
  * #234: whitelist loop (telegram_backup.py ~:698-711) bare get_entity(cid) except→warn→skip, no fallback; regression from 0316c41 (#96 fix for #95) which removed the dialog sweep that used to populate the session entity cache; bare numeric USER ids need cached access_hash (channels have hash-0 probe, users don't). DM whitelist entries on fresh sessions silently never archive. REAL BUG.
- Plan: one PR fixing both; minor bump v7.27.0; merge (pre-authorized) → tag → images → deploy geiserback (+ reconcile stale gitops v7.25.2 while at it) → verify → answer+close both thanking @Sasha50701.

### 2026-07-25 — design check (3-skeptic adversarial panel) → SOUND_WITH_CHANGES ×3
- concurrency-232: CONFIRMED core mechanism (threshold is a clamped property min(v,86400); _call reads CLIENT threshold live on every route incl. parallel borrowed senders; wait_for does NOT convert FloodWaitError; CancelledError-safe finally). REQUIRED: isinstance-int guard on threshold (MagicMock <= 0 raises TypeError on py314 — would break ~25 tests); re-read depth in finally (captured-value decrement corrupts overlap + poisons counter to -1); document telethon request_retries=5 ValueError exhaustion (new failure mode) + #124 dilution window. BONUS: FloodPremiumWaitError is a SIBLING of FloodWaitError — widen call_with_flood_retry + parallel_download re-raise (premium floods = the classic large-download flood).
- semantics-234: CONFIRMED the central bet (iter_dialogs → session process_entities insert-or-replace on the SAME sqlite connection → same-run bare get_entity succeeds; mb in-memory cache consulted first; durable via 60s session.save loop). folder unspecified = includes archived dialogs (folder=0 would MISS archived DMs — do not reuse _get_dialogs). limit=1000 = ≤10 chunked GetDialogsRequests, hard-bounded. REQUIRED: broad except (not just FloodWait) — sweep must be best-effort; suppress re-sweep for known-dead ids (fresh TelegramBackup per run → DB metadata, not instance state).
- tests-scope: mapped exact blast radius (~26 existing tests would break without isinstance guards; :704 canary must stay unmodified; CM must live INSIDE _fetch_media_bytes for the mock-replacement tests). Prescribed 18-test set (adopted). Added: time-bound the sweep with wait_for(300) — count limit alone cannot prevent a wedged-connection hang (#95's actual failure mode).
- Design amendments folded in: dialog.entity grabbed directly during sweep (saves a get_entity per recovered chat); only exact marked-id matches early-stop (discarding entity.id too risks cross-type raw-id collisions); metadata key whitelist_unresolved_ids persists post-retry still-failed set (self-clears on resolution; listener traffic is the natural resurrection path).
- exec-232-234 (opus) dispatched with the full amended spec in worktree ai/issues-232-234. No git ops for executors (standing rule).

### 2026-07-25 — PR #235 opened; review panel round applied
- exec-232-234 delivered both fixes; MY verify: 2368 passed, ruff clean. Committed ace4bf0, PR #235 opened (fixes #232 + #234, v7.27.0).
- 7-lens review panel + adversarial verify: 3 CONFIRMED HIGHs = one root defect (known-failed suppression permanently poisonable: aborted sweep persisted unproven ids; raised limit never re-armed; stale entries lingered). 0 serious findings refuted.
- Fix round 71702df: completion-aware persistence (only a COMPLETED scan proves absence; aborts retain prior-proven ids only), proof-bound stored with ids ({"limit":N,"ids":[]}, legacy list invalidates), stale-clear on all-resolved runs, followed ids never persisted, ValueError narrowed by message match, log undercount fixed. 11 new tests → suite 2379 passed, ruff+format clean.
- Consciously accepted (documented, not fixed): listener downloads have no per-op timeout (pre-existing; absorption trades abort-forever for bounded-longer transfers); #124 dilution during CM windows (client-global attr is the only lever — telethon __call__ drops the kwarg); telethon request_retries ValueError exhaustion path (now visible via narrowed warning).
- NEXT: CI green on 71702df + CodeRabbit round → merge (pre-authorized) → tag v7.27.0 → gitops deploy (healthy again — reconciles v7.25.2 lag) → verify → answer+close #232/#234 thanking @Sasha50701.

### 2026-07-25 — PR #235 MERGED, v7.27.0 released, gitops deploy fired
- CodeRabbit round: 2 actionable (the recurring PEP-758 "Critical" false alarm — declined with ast.parse+import+CI-green proof on py3.14.6, same as PR #230; a Minor about _get_base_env credential vars — declined: Config reads ONLY TELEGRAM_-prefixed names, config.py:117-119/:761) + 1 nitpick (bare except ValueError — already narrowed in 71702df before its review posted). All CI green on 71702df (ruff, test 2379, CodeQL — no serve_media edits so no FP this round, build, GitGuardian, codecov).
- MERGED squash f2c8572; tag v7.27.0; GitHub Release + Docker Publish + Docker Publish Viewer all success.
- Gitops: giteaer/geiserback bumped v7.25.2→v7.27.0 in one commit (reconciles the stale-repo lag from the 7.26.0 direct deploy — that pending item is now CLOSED). Webhook fired; waiting on 4 containers.

### 2026-07-25 — #232 + #234 SHIPPED v7.27.0, deployed via gitops, both issues CLOSED
- Webhook gitops deploy (Gitea healthy again): giteaer/geiserback v7.25.2→v7.27.0 — the stale-repo pending item from the 7.26.0 round is RESOLVED. All 4 prod containers Up on v7.27.0.
- Live verify: both sessions authorized (Annais 11589825, Géiser 835003); absorb_media_floods present in running telegram_backup.py(7)/listener.py(3); both config knobs present; backup+viewer report 7.27.0; viewers 200/200; zero non-flood errors; annais listener ENABLED + 18 events/10min. sergio get_dialogs flood persists exactly as before (account-level, pre-existing, decision still with Sergio; listener capture unaffected).
- #232 answered (comment 5078650116) + CLOSED; #234 answered (comment 5078650181) + CLOSED — both thank @Sasha50701 and invite testing (auto-closed by merge keywords; comments added post-deploy).
- ALL 8 PRD stories pass. sergio-loop goal met: vetted (not AI slop) → designed (3-skeptic panel) → implemented → 7-lens reviewed + adversarially verified (3 HIGHs found+fixed pre-merge) → CodeRabbit addressed → merged f2c8572 → v7.27.0 released → gitops deployed → live-verified → answered+closed.
- Worktree ai/issues-232-234 removed after merge (repo CLAUDE.md standing instruction). NOTE for Sergio: 6 OLDER stale worktrees remain (.worktrees/issue-224, issue-224-resume, issues-221-222, issues-228-229, parallel-download, pillow-security, review-fixes) — from earlier merged rounds, not removing without explicit OK.

### 2026-07-25 — post-loop ops (Sergio-approved): sergio→whitelist mode + worktree cleanup
- Evidence first: sergio get_dialogs re-floods on EVERY retry (30 consecutive over 2h, honoring wait=25s each time) → account-flagged method, 120-ladder can never pass → flip justified.
- Whitelist derivation: DB has 2490 chats (4470 message-having ids) vs ~429 live dialogs → full-DB list wrong. Chose 365d-active set: 673 ids (excludes subtracted, priority chat included). WHITELIST_RESOLVE_DIALOG_LIMIT=0 on sergio (rescue scan uses the same flagged method; all ids session-cached).
- Gitops commits: CHAT_IDS+knob added to sergio service (with trade-off comment); then dropped sergio MAX_FLOOD_RETRIES=120 (legacy flood-riding knob) after first resolution pass exposed a DEAD BASIC GROUP id → ChatIdInvalidError classified TRANSIENT → 120×~300s ≈ 10h stall per dead id. At default 5 retries a dead id costs ~1min.
- Verified during flip: "Whitelist mode: fetching 673 chat(s) directly", get_dialogs calls = 0, listener capturing ("New message saved") in whitelist mode.
- TRADE-OFF (told to Sergio): whitelist = total capture scope incl. listener; new/dormant chats need manual CHAT_IDS addition. Regen recipe in memory.
- FOLLOW-UP CANDIDATE: call_with_flood_retry should classify ChatIdInvalidError/PeerIdInvalidError as terminal (permanent ids never become valid); would make whitelist mode robust to dead ids at defaults. File as issue.
- Worktrees: all 7 removed (+ their local branches; remotes were already auto-deleted). exec-232-234 teammate had resurrected issues-232-234 → stopped the teammate, removed again, .worktrees/ gone. ~12 older worktree-less local branches left untouched (out of approved scope).

### 2026-07-28 — kickoff: assess issues #242–#244
- Directive appended verbatim to `docs/GOAL.md`. Solo loop engaged: `/research! → /implement! → /review-pr! → (back to research)`.
- Open issues to assess for validity, value, and official-client parity: #242 calendar-jump forward pagination, #243 unreadable month selector on dark Windows Chromium, #244 message-day indicators in the date calendar.
- No implementation decision yet; next step is parallel code, history, UX, official-client, performance, and test research.

### 2026-07-28 — research verdict: all three are real; narrow the proposals
- Parallel 8-lens panel completed (code flow, UX, architecture, official clients, slop critique, query performance, history, tests). Tally: **0 closed / 3 open**.
- **#242 GO, reframed as completing deferred work rather than a regression.** PR #217 explicitly deferred continuous forward pagination in detached history windows. Android directly re-enables forward history after date jumps; current viewer has only the older-history sentinel. Build a separate newer cursor/loader using existing `after_id`, preserve topic and stale-chat guards, and auto-return to live mode at the tail.
- **#243 GO, CSS-only.** Flatpickr's native month `<select>` inherits pale text while Windows Chromium can render the popup white. Use scoped `color-scheme: dark` plus explicit select/option colors. A custom picker is disproportionate.
- **#244 GO with scope cuts.** Add binary dots for message-bearing days; keep empty days selectable (official clients do). No heatmap, count UI, previews, schema migration, server cache, or custom calendar. Use one portable statement of 28–31 index-backed day-existence probes with UTC half-open ranges; pass active topic and fix the existing topic/date lookup mismatch.
- Official-client nuance: iOS/Android/Desktop mainly decorate media days, not every text-message day, but all provide themed calendar navigation and anchored history that remains pageable. Binary message-day dots are a useful archive-specific extension, not exact parity.
- Acceptance plan: regression-first tests; three production files for #244 (`adapter.py`, `main.py`, `index.html`), frontend-only #242/#243, then full review/deslop/release loop.
