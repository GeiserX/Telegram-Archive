-- merge_sqlite.sql — import a second single-account SQLite archive into this one.
--
-- Companion to docs/MERGING-ARCHIVES.md. Read that page first: it explains the
-- preconditions (both installs upgraded to the same 8.0.x release and started
-- once, both containers stopped, both databases backed up) and every step that
-- comes before and after this script (media files, the session file, the env).
--
-- How to run it — interactively, on a COPY-VERIFIED-BACKED-UP target:
--
--     sqlite3 /path/to/target/data/backups/telegram_backup.db
--     sqlite> .bail on
--     sqlite> ATTACH DATABASE '/path/to/source/telegram_backup.db' AS source;
--     sqlite> .read scripts/merge/merge_sqlite.sql
--     ... preflight, import, per-table counts, verdict ...
--     sqlite> COMMIT;      -- ONLY if the verdict line says OK
--     sqlite> ROLLBACK;    -- otherwise, or whenever in doubt
--
-- THIS SCRIPT DOES NOT COMMIT. Everything below runs inside one transaction
-- that is left open on purpose, so the last word is yours: read the summary and
-- the verdict, then type COMMIT; yourself — or ROLLBACK; and the target is
-- byte-for-byte what it was. Quitting sqlite3 without committing also rolls
-- everything back. There is no state in between.
--
-- Failure model: every precondition below is checked by inserting into a guard
-- table wired to a trigger that RAISEs with a message naming the failed check.
-- With .bail on (line 1 below sets it), the first failed check stops the script
-- immediately — nothing after it runs, and the open transaction means nothing
-- is kept. If sqlite3 drops back to its prompt after an error, type ROLLBACK;
-- if it exits, the rollback already happened.
--
-- Privacy: this script prints table names and row counts only — never chat
-- ids, titles, phone numbers or message content, same as the rest of the
-- project. The one path-shaped output is the media root comparison, and it is
-- reported as yes/no, never as the paths themselves.

.bail on
.headers on
.mode box

.print ''
.print '=== Telegram-Archive merge (SQLite): preflight ==='

-- ---------------------------------------------------------------------------
-- Guard plumbing. _guard is a scratch table; each named trigger below fires on
-- a failed check and aborts with a message that says exactly what failed and
-- what to do about it. Temporary objects, private to this session. The DROPs
-- make a second run in the same sqlite3 session start clean (dropping _guard
-- drops its triggers with it).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS temp._guard;
DROP TABLE IF EXISTS temp._fk_baseline;
DROP TABLE IF EXISTS temp._merge_counts;
DROP TABLE IF EXISTS temp._new_account;

CREATE TEMP TABLE _guard (check_name TEXT NOT NULL, ok INTEGER NOT NULL);

CREATE TEMP TRIGGER guard_source_attached BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_attached' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: no database is attached as "source". Run ATTACH DATABASE ''/path/to/source/telegram_backup.db'' AS source; before .read, as the guide shows. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_source_is_not_target BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_is_not_target' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the attached "source" is the same file as the target. Attach the OTHER install''s database. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_target_revision BEFORE INSERT ON _guard
WHEN NEW.check_name = 'target_revision' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the TARGET database is not at schema revision 023, the revision this script was written for. Upgrade the target install to the release this script shipped with, start it once, stop it, and re-run. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_source_revision BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_revision' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the SOURCE database is not at schema revision 023, the revision this script was written for. Upgrade the source install to the same release as the target, start it once, stop it, and re-run. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_target_claimed BEFORE INSERT ON _guard
WHEN NEW.check_name = 'target_claimed' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the TARGET has an account row with no Telegram user id, which means an account has never completed a start under 8.0. Start the target backup container once (it claims its row on login), stop it, and re-run. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_source_single_account BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_single_account' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the SOURCE archive holds more than one account (or none). This script merges a single-account archive; a multi-account source is out of scope — see docs/MERGING-ARCHIVES.md. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_source_claimed BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_claimed' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the SOURCE account row has no Telegram user id, so the merged rows could never be matched to a login. Start the source backup container once under 8.0 (it claims its row on login), stop it, and re-run. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_source_rows_one_account BEFORE INSERT ON _guard
WHEN NEW.check_name = 'source_rows_one_account' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the SOURCE archive has data rows under an account_id that is not its single account row — it is not the clean single-account archive this script expects. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_identities_distinct BEFORE INSERT ON _guard
WHEN NEW.check_name = 'identities_distinct' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: the source account''s Telegram user id already exists in the target. These are two archives of the SAME Telegram account; merging them is message-level deduplication, a different problem this script refuses to guess at. Nothing has been changed.'); END;

CREATE TEMP TRIGGER guard_no_ref_collision BEFORE INSERT ON _guard
WHEN NEW.check_name = 'no_ref_collision' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE ABORTED: a chat ref in the source already exists in the target. Refs are 128-bit random values, so this does not happen to honestly generated archives — one of these databases has hand-copied rows. Nothing has been changed.'); END;

-- Post-import triggers: same mechanism, applied to the verification checks.
CREATE TEMP TRIGGER guard_counts_match BEFORE INSERT ON _guard
WHEN NEW.check_name = 'counts_match' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE FAILED VERIFICATION: at least one table''s imported row count does not match the source (see the counts table above). Type ROLLBACK; — nothing is committed.'); END;

CREATE TEMP TRIGGER guard_no_new_fk_violations BEFORE INSERT ON _guard
WHEN NEW.check_name = 'no_new_fk_violations' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE FAILED VERIFICATION: the import introduced foreign key violations that exist in neither original archive. Type ROLLBACK; — nothing is committed.'); END;

CREATE TEMP TRIGGER guard_refs_unique_after BEFORE INSERT ON _guard
WHEN NEW.check_name = 'refs_unique_after' AND NEW.ok = 0
BEGIN SELECT RAISE(ABORT, 'MERGE FAILED VERIFICATION: chat refs are not globally unique after the import. Type ROLLBACK; — nothing is committed.'); END;

-- ---------------------------------------------------------------------------
-- Attachment checks (no transaction needed; they read catalog state only).
-- ---------------------------------------------------------------------------
INSERT INTO _guard
SELECT 'source_attached',
       EXISTS (SELECT 1 FROM pragma_database_list WHERE name = 'source');

INSERT INTO _guard
SELECT 'source_is_not_target',
       (SELECT file FROM pragma_database_list WHERE name = 'main')
       IS NOT (SELECT file FROM pragma_database_list WHERE name = 'source');

-- ---------------------------------------------------------------------------
-- Open the one transaction everything runs in. IMMEDIATE takes the write lock
-- now: if another process is writing the target, this fails right here with
-- "database is locked" instead of half way through. (A reader — a still-running
-- viewer — does not show up here in WAL mode, which is why the guide's first
-- step is stopping both installs'' containers.)
-- ---------------------------------------------------------------------------
BEGIN IMMEDIATE;

-- ---------------------------------------------------------------------------
-- Preflight guards. Every one of these aborts the script with a specific
-- message (see the triggers above) rather than importing into a wrong state.
-- ---------------------------------------------------------------------------

-- Both databases must be at the schema revision this script was written for.
-- Equal is not enough: equal-but-older would mean the multi-account keys this
-- import depends on do not exist yet.
INSERT INTO _guard
SELECT 'target_revision',
       (SELECT count(*) FROM main.alembic_version WHERE version_num = '023') = 1
       AND (SELECT count(*) FROM main.alembic_version) = 1;

INSERT INTO _guard
SELECT 'source_revision',
       (SELECT count(*) FROM source.alembic_version WHERE version_num = '023') = 1
       AND (SELECT count(*) FROM source.alembic_version) = 1;

-- The target must have started at least once under 8.0: every account row
-- claimed (telegram_user_id filled in on first login). An unclaimed row would
-- later be claimed by env index 1 and could swallow the imported history.
INSERT INTO _guard
SELECT 'target_claimed',
       (SELECT count(*) FROM main.accounts) >= 1
       AND (SELECT count(*) FROM main.accounts WHERE telegram_user_id IS NULL) = 0;

-- The source must be exactly one account, and that account must have logged in
-- at least once under 8.0 — its telegram_user_id is what the target's first
-- start will match on to ADOPT the imported rows (src/db/adapter.py,
-- ensure_account: a row already carrying the user id wins outright).
INSERT INTO _guard
SELECT 'source_single_account', (SELECT count(*) FROM source.accounts) = 1;

INSERT INTO _guard
SELECT 'source_claimed',
       (SELECT telegram_user_id FROM source.accounts) IS NOT NULL;

-- Every data row in the source must belong to that single account.
INSERT INTO _guard
SELECT 'source_rows_one_account',
       (
         (SELECT count(*) FROM source.chats               WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.messages            WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.message_versions    WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.media               WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.reactions           WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.sync_status         WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.forum_topics        WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.chat_folders        WHERE account_id <> (SELECT id FROM source.accounts))
       + (SELECT count(*) FROM source.chat_folder_members WHERE account_id <> (SELECT id FROM source.accounts))
       ) = 0;

-- Two archives of the same Telegram account are not a merge — refuse.
INSERT INTO _guard
SELECT 'identities_distinct',
       NOT EXISTS (
         SELECT 1 FROM main.accounts t
         JOIN source.accounts s ON t.telegram_user_id = s.telegram_user_id
       );

-- Imported chats KEEP their refs (they are 128-bit random, and the source
-- install's share links die with that install either way), so a collision must
-- be impossible rather than improbable: checked here, and re-checked after.
INSERT INTO _guard
SELECT 'no_ref_collision',
       NOT EXISTS (SELECT 1 FROM source.chats s JOIN main.chats t ON t.ref = s.ref);

.print 'preflight OK — importing'

-- ---------------------------------------------------------------------------
-- Baselines for the foreign-key verdict. Archives can legally carry old orphan
-- rows (migration 022 carries them over too), so the bar is "the import adds
-- no NEW violations": afterwards, main must show at most the violations it
-- already had plus the ones the source already had.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _fk_baseline AS
SELECT
  (SELECT count(*) FROM pragma_foreign_key_check) AS target_before,
  ( (SELECT count(*) FROM pragma_foreign_key_check('messages',            'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('message_versions',    'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('media',               'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('reactions',           'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('sync_status',         'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('forum_topics',        'source'))
  + (SELECT count(*) FROM pragma_foreign_key_check('chat_folder_members', 'source'))
  ) AS source_before;

-- ---------------------------------------------------------------------------
-- Expected row counts, captured BEFORE anything is written so the final
-- comparison is against a snapshot the import cannot have influenced.
-- For users (a global table — a Telegram user is the same person in both
-- archives) the expectation is "every source sender exists in the target
-- afterwards"; rows the target already had are kept as they are.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _merge_counts (table_name TEXT NOT NULL, expected INTEGER NOT NULL, actual INTEGER);

INSERT INTO _merge_counts (table_name, expected)
SELECT 'users (new senders)', count(*) FROM source.users s
WHERE NOT EXISTS (SELECT 1 FROM main.users t WHERE t.id = s.id);

INSERT INTO _merge_counts (table_name, expected) SELECT 'chats',               count(*) FROM source.chats;
INSERT INTO _merge_counts (table_name, expected) SELECT 'messages',            count(*) FROM source.messages;
INSERT INTO _merge_counts (table_name, expected) SELECT 'message_versions',    count(*) FROM source.message_versions;
INSERT INTO _merge_counts (table_name, expected) SELECT 'media',               count(*) FROM source.media;
INSERT INTO _merge_counts (table_name, expected) SELECT 'reactions',           count(*) FROM source.reactions;
INSERT INTO _merge_counts (table_name, expected) SELECT 'sync_status',         count(*) FROM source.sync_status;
INSERT INTO _merge_counts (table_name, expected) SELECT 'forum_topics',        count(*) FROM source.forum_topics;
INSERT INTO _merge_counts (table_name, expected) SELECT 'chat_folders',        count(*) FROM source.chat_folders;
INSERT INTO _merge_counts (table_name, expected) SELECT 'chat_folder_members', count(*) FROM source.chat_folder_members;

-- ---------------------------------------------------------------------------
-- The imported account row. id is deliberately omitted: accounts.id is an
-- INTEGER PRIMARY KEY, so SQLite assigns the next free id — the id the source
-- used (1) already belongs to the target's own first account. The label rides
-- along as-is; it is cosmetic, and the first start rewrites it from the env's
-- TG_ACCOUNT_<N>_LABEL anyway. _new_account captures the assigned id; every
-- INSERT below stamps it over the source's account_id.
-- ---------------------------------------------------------------------------
INSERT INTO main.accounts (label, telegram_user_id)
SELECT label, telegram_user_id FROM source.accounts;

CREATE TEMP TABLE _new_account AS SELECT last_insert_rowid() AS id;

-- ---------------------------------------------------------------------------
-- The import. Parents before children (users/chats/folders before messages
-- before media/reactions/versions), every column named explicitly — a migrated
-- schema and a freshly created one can order columns differently, and this
-- script must work identically on both.
--
-- users: target rows always win. The users table is global by design (see
-- src/db/models.py) with last-writer-wins display names; a merge keeps the
-- target's version of anyone both archives know.
-- ---------------------------------------------------------------------------
INSERT INTO main.users (id, username, first_name, last_name, phone, is_bot, created_at, updated_at)
SELECT s.id, s.username, s.first_name, s.last_name, s.phone, s.is_bot, s.created_at, s.updated_at
FROM source.users s
WHERE NOT EXISTS (SELECT 1 FROM main.users t WHERE t.id = s.id);

INSERT INTO main.chats (account_id, id, ref, type, title, username, first_name, last_name, phone,
                        description, participants_count, is_forum, is_archived,
                        last_synced_message_id, created_at, updated_at)
SELECT (SELECT id FROM _new_account), s.id, s.ref, s.type, s.title, s.username, s.first_name,
       s.last_name, s.phone, s.description, s.participants_count, s.is_forum, s.is_archived,
       s.last_synced_message_id, s.created_at, s.updated_at
FROM source.chats s;

INSERT INTO main.chat_folders (account_id, id, title, emoticon, sort_order, created_at, updated_at)
SELECT (SELECT id FROM _new_account), s.id, s.title, s.emoticon, s.sort_order, s.created_at, s.updated_at
FROM source.chat_folders s;

INSERT INTO main.messages (account_id, id, chat_id, sender_id, sender_name, date, text,
                           reply_to_msg_id, reply_to_top_id, reply_to_text, forward_from_id,
                           edit_date, raw_data, created_at, is_outgoing, is_pinned,
                           is_deleted, deleted_at)
SELECT (SELECT id FROM _new_account), s.id, s.chat_id, s.sender_id, s.sender_name, s.date, s.text,
       s.reply_to_msg_id, s.reply_to_top_id, s.reply_to_text, s.forward_from_id,
       s.edit_date, s.raw_data, s.created_at, s.is_outgoing, s.is_pinned,
       s.is_deleted, s.deleted_at
FROM source.messages s;

INSERT INTO main.forum_topics (account_id, id, chat_id, title, icon_color, icon_emoji_id,
                               icon_emoji, is_closed, is_pinned, is_hidden, date,
                               created_at, updated_at)
SELECT (SELECT id FROM _new_account), s.id, s.chat_id, s.title, s.icon_color, s.icon_emoji_id,
       s.icon_emoji, s.is_closed, s.is_pinned, s.is_hidden, s.date, s.created_at, s.updated_at
FROM source.forum_topics s;

-- sync_status is what makes the first merged sweep incremental: the imported
-- last_message_id cursors mean account 2 resumes where the source install
-- stopped, instead of re-capturing every chat from message 1.
INSERT INTO main.sync_status (account_id, chat_id, last_message_id, last_sync_date, message_count)
SELECT (SELECT id FROM _new_account), s.chat_id, s.last_message_id, s.last_sync_date, s.message_count
FROM source.sync_status s;

INSERT INTO main.chat_folder_members (account_id, folder_id, chat_id)
SELECT (SELECT id FROM _new_account), s.folder_id, s.chat_id
FROM source.chat_folder_members s;

INSERT INTO main.media (account_id, id, message_id, chat_id, type, file_path, file_name,
                        file_size, mime_type, width, height, duration, content_hash,
                        downloaded, download_attempts, download_date, created_at)
SELECT (SELECT id FROM _new_account), s.id, s.message_id, s.chat_id, s.type, s.file_path,
       s.file_name, s.file_size, s.mime_type, s.width, s.height, s.duration, s.content_hash,
       s.downloaded, s.download_attempts, s.download_date, s.created_at
FROM source.media s;

-- message_versions and reactions have their own autoincrement ids in each
-- archive; the source's values would collide with the target's, so the id is
-- omitted and SQLite assigns fresh ones. Nothing references these ids.
INSERT INTO main.message_versions (account_id, message_id, chat_id, text, date, change_hash, captured_at)
SELECT (SELECT id FROM _new_account), s.message_id, s.chat_id, s.text, s.date, s.change_hash, s.captured_at
FROM source.message_versions s;

INSERT INTO main.reactions (account_id, message_id, chat_id, emoji, user_id, "count", created_at, removed_at)
SELECT (SELECT id FROM _new_account), s.message_id, s.chat_id, s.emoji, s.user_id, s."count", s.created_at, s.removed_at
FROM source.reactions s;

.print 'import done — verifying'

-- ---------------------------------------------------------------------------
-- Verification. Actual counts are RE-COUNTED from the target — never assumed
-- from the statements above — and compared to the pre-import snapshot.
-- ---------------------------------------------------------------------------
UPDATE _merge_counts SET actual =
  expected - (SELECT count(*) FROM source.users s
              WHERE NOT EXISTS (SELECT 1 FROM main.users t WHERE t.id = s.id))
WHERE table_name = 'users (new senders)';

UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.chats               WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'chats';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.messages            WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'messages';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.message_versions    WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'message_versions';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.media               WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'media';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.reactions           WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'reactions';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.sync_status         WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'sync_status';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.forum_topics        WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'forum_topics';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.chat_folders        WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'chat_folders';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM main.chat_folder_members WHERE account_id = (SELECT id FROM _new_account)) WHERE table_name = 'chat_folder_members';

.print ''
.print '=== Imported rows, per table (counts only) ==='
SELECT table_name, expected, actual,
       CASE WHEN expected IS actual THEN 'ok' ELSE 'MISMATCH' END AS status
FROM _merge_counts;

-- These three inserts ARE the verification: each aborts loudly via its trigger
-- if its check fails, and their survival is what the verdict below reports.
INSERT INTO _guard
SELECT 'counts_match',
       NOT EXISTS (SELECT 1 FROM _merge_counts WHERE expected IS NOT actual);

INSERT INTO _guard
SELECT 'no_new_fk_violations',
       (SELECT count(*) FROM pragma_foreign_key_check)
       <= (SELECT target_before + source_before FROM _fk_baseline);

INSERT INTO _guard
SELECT 'refs_unique_after',
       (SELECT count(*) FROM main.chats) = (SELECT count(DISTINCT ref) FROM main.chats);

-- ---------------------------------------------------------------------------
-- Summary and verdict. Counts and yes/no indicators only.
-- The media-roots line compares the directory prefix up to and including
-- '/media/' of one stored media path from each archive — if they differ, the
-- imported media rows point at paths the target install does not serve, and
-- the media section of docs/MERGING-ARCHIVES.md tells you how to remap them.
-- ---------------------------------------------------------------------------
.print ''
.print '=== Summary ==='
SELECT
  (SELECT id FROM _new_account)                                          AS imported_account_row,
  (SELECT count(*) FROM main.accounts)                                   AS accounts_total,
  (SELECT target_before FROM _fk_baseline)                               AS fk_violations_target_before,
  (SELECT source_before FROM _fk_baseline)                               AS fk_violations_source_before,
  (SELECT count(*) FROM pragma_foreign_key_check)                        AS fk_violations_after,
  CASE
    WHEN (SELECT count(*) FROM main.media   WHERE file_path IS NOT NULL AND instr(file_path, '/media/') > 0) = 0
      OR (SELECT count(*) FROM source.media WHERE file_path IS NOT NULL AND instr(file_path, '/media/') > 0) = 0
    THEN 'n/a'
    WHEN (SELECT substr(file_path, 1, instr(file_path, '/media/') + 6) FROM main.media
          WHERE file_path IS NOT NULL AND instr(file_path, '/media/') > 0 LIMIT 1)
       = (SELECT substr(file_path, 1, instr(file_path, '/media/') + 6) FROM source.media
          WHERE file_path IS NOT NULL AND instr(file_path, '/media/') > 0 LIMIT 1)
    THEN 'yes'
    ELSE 'NO - see the media section of docs/MERGING-ARCHIVES.md'
  END                                                                    AS media_roots_match;

.print ''
.print '=== VERDICT ==='
SELECT CASE
  WHEN (SELECT count(*) FROM _guard WHERE ok = 0) = 0
   AND (SELECT count(*) FROM _guard) = 13
  THEN 'OK — every check passed. Type COMMIT; now to keep the merge.'
  ELSE 'FAILED — do NOT commit. Type ROLLBACK; now.'
END AS verdict;

.print ''
.print 'This script never commits. COMMIT; keeps the merge, ROLLBACK; (or just'
.print 'quitting sqlite3) leaves the target exactly as it was.'
