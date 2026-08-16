-- merge_postgres.sql — import a second single-account PostgreSQL archive into
-- this one.
--
-- Companion to docs/MERGING-ARCHIVES.md. Read that page first: it explains the
-- preconditions (both installs upgraded to the same 8.0.x release and started
-- once, both containers stopped, both databases backed up) and every step that
-- comes before and after this script (media files, the session file, the env).
--
-- How to run it — against a BACKED-UP target database:
--
--     psql "host=127.0.0.1 port=5432 dbname=telegram_backup user=telegram" \
--       -v SRC_HOST=127.0.0.1 -v SRC_PORT=5432 -v SRC_DB=ta_merge_source \
--       -v SRC_USER=telegram -v SRC_PASSWORD='...' \
--       -f scripts/merge/merge_postgres.sql
--
-- SRC_* names the SOURCE database — either the other install's live (but
-- stopped-container) PostgreSQL server reachable over the network, or, usually
-- simpler, a dump of it restored as an extra database on the target's own
-- PostgreSQL server (the guide shows both). The source is only ever read.
--
-- Failure model: the whole script is ONE transaction. Every precondition and
-- every verification is a DO block that RAISEs EXCEPTION on failure; psql
-- stops there (ON_ERROR_STOP is set below) and the transaction rolls back, so
-- a failed run leaves the target byte-for-byte untouched. Only a run in which
-- every check passed reaches the COMMIT on the last line. (Even without
-- ON_ERROR_STOP a failed transaction cannot commit — PostgreSQL turns the
-- final COMMIT of an aborted transaction into a rollback.)
--
-- Requirements: run as a superuser of the target cluster (the stock
-- docker-compose POSTGRES_USER is one) — CREATE EXTENSION postgres_fdw and
-- reading other sessions in pg_stat_activity need it.
--
-- Privacy: output is table names, row counts and yes/no indicators only —
-- never chat ids, titles, phone numbers or message content, same as the rest
-- of the project.

\set ON_ERROR_STOP on

-- Refuse to start without the five source parameters, before anything runs.
\if :{?SRC_HOST}
\else
\echo 'MERGE ABORTED: missing -v SRC_HOST=... (see the header of this script). Nothing has been changed.'
DO $$ BEGIN RAISE EXCEPTION 'MERGE ABORTED: a required -v parameter is missing — see the line above. Nothing has been changed.'; END $$;
\endif
\if :{?SRC_PORT}
\else
\echo 'MERGE ABORTED: missing -v SRC_PORT=... (see the header of this script). Nothing has been changed.'
DO $$ BEGIN RAISE EXCEPTION 'MERGE ABORTED: a required -v parameter is missing — see the line above. Nothing has been changed.'; END $$;
\endif
\if :{?SRC_DB}
\else
\echo 'MERGE ABORTED: missing -v SRC_DB=... (see the header of this script). Nothing has been changed.'
DO $$ BEGIN RAISE EXCEPTION 'MERGE ABORTED: a required -v parameter is missing — see the line above. Nothing has been changed.'; END $$;
\endif
\if :{?SRC_USER}
\else
\echo 'MERGE ABORTED: missing -v SRC_USER=... (see the header of this script). Nothing has been changed.'
DO $$ BEGIN RAISE EXCEPTION 'MERGE ABORTED: a required -v parameter is missing — see the line above. Nothing has been changed.'; END $$;
\endif
\if :{?SRC_PASSWORD}
\else
\echo 'MERGE ABORTED: missing -v SRC_PASSWORD=... (see the header of this script). Nothing has been changed.'
DO $$ BEGIN RAISE EXCEPTION 'MERGE ABORTED: a required -v parameter is missing — see the line above. Nothing has been changed.'; END $$;
\endif

\echo ''
\echo '=== Telegram-Archive merge (PostgreSQL): preflight ==='

BEGIN;

-- ---------------------------------------------------------------------------
-- Attach the source, read-only, through postgres_fdw. Everything created here
-- is dropped again at the end of this script (and rolls back with everything
-- else on failure). application_name 'ta_merge' marks our own connection on
-- the source server so the nobody-else-connected check below can exclude it.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS ta_merge_source_srv CASCADE;
CREATE SERVER ta_merge_source_srv
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host :'SRC_HOST', port :'SRC_PORT', dbname :'SRC_DB', application_name 'ta_merge');

CREATE USER MAPPING FOR CURRENT_USER
  SERVER ta_merge_source_srv
  OPTIONS (user :'SRC_USER', password :'SRC_PASSWORD');

DROP SCHEMA IF EXISTS merge_source CASCADE;
CREATE SCHEMA merge_source;

IMPORT FOREIGN SCHEMA public
  LIMIT TO (alembic_version, accounts, users, chats, messages, message_versions,
            media, reactions, sync_status, forum_topics, chat_folders,
            chat_folder_members)
  FROM SERVER ta_merge_source_srv INTO merge_source;

-- A window onto the source server's own session list, for the guard that
-- nothing else is connected to the source while we read it.
CREATE FOREIGN TABLE merge_source.remote_activity
  (datname name, application_name text, backend_type text)
  SERVER ta_merge_source_srv
  OPTIONS (schema_name 'pg_catalog', table_name 'pg_stat_activity');

-- DO blocks cannot see psql variables, so the one value they need is parked in
-- a temp table. ON COMMIT DROP: gone whether this run commits or rolls back.
CREATE TEMP TABLE _merge_params ON COMMIT DROP AS
SELECT :'SRC_DB'::text AS src_db;

-- ---------------------------------------------------------------------------
-- Preflight guards. Every one RAISEs a specific, actionable message rather
-- than importing into a wrong state; any RAISE rolls the whole run back.
-- ---------------------------------------------------------------------------

-- All twelve tables must have come across, or SRC_DB is not a
-- Telegram-Archive database (or not one that finished its upgrade).
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM information_schema.tables
   WHERE table_schema = 'merge_source'
     AND table_name IN ('alembic_version', 'accounts', 'users', 'chats',
                        'messages', 'message_versions', 'media', 'reactions',
                        'sync_status', 'forum_topics', 'chat_folders',
                        'chat_folder_members');
  IF n <> 12 THEN
    RAISE EXCEPTION 'MERGE ABORTED: only % of the 12 expected tables exist in the source database — is SRC_DB really a Telegram-Archive database at 8.0? Nothing has been changed.', n;
  END IF;
END $$;

-- Both databases must be at the schema revision this script was written for.
-- Equal is not enough: equal-but-older would mean the multi-account keys this
-- import depends on do not exist yet.
DO $$
BEGIN
  IF (SELECT count(*) FROM alembic_version) <> 1
     OR (SELECT version_num FROM alembic_version) <> '023' THEN
    RAISE EXCEPTION 'MERGE ABORTED: the TARGET database is not at schema revision 023, the revision this script was written for. Upgrade the target install to the release this script shipped with, start it once, stop it, and re-run. Nothing has been changed.';
  END IF;
  IF (SELECT count(*) FROM merge_source.alembic_version) <> 1
     OR (SELECT version_num FROM merge_source.alembic_version) <> '023' THEN
    RAISE EXCEPTION 'MERGE ABORTED: the SOURCE database is not at schema revision 023, the revision this script was written for. Upgrade the source install to the same release as the target, start it once, stop it, and re-run (a source restored from a dump must be dumped AFTER that upgrade). Nothing has been changed.';
  END IF;
END $$;

-- The target must have started at least once under 8.0: every account row
-- claimed (telegram_user_id filled in on first login). An unclaimed row would
-- later be claimed by env index 1 and could swallow the imported history.
DO $$
BEGIN
  IF (SELECT count(*) FROM accounts) < 1
     OR EXISTS (SELECT 1 FROM accounts WHERE telegram_user_id IS NULL) THEN
    RAISE EXCEPTION 'MERGE ABORTED: the TARGET has an account row with no Telegram user id, which means an account has never completed a start under 8.0. Start the target backup container once (it claims its row on login), stop it, and re-run. Nothing has been changed.';
  END IF;
END $$;

-- The source must be exactly one account, and that account must have logged in
-- at least once under 8.0 — its telegram_user_id is what the target's first
-- start will match on to ADOPT the imported rows (src/db/adapter.py,
-- ensure_account: a row already carrying the user id wins outright).
DO $$
BEGIN
  IF (SELECT count(*) FROM merge_source.accounts) <> 1 THEN
    RAISE EXCEPTION 'MERGE ABORTED: the SOURCE archive holds more than one account (or none). This script merges a single-account archive; a multi-account source is out of scope — see docs/MERGING-ARCHIVES.md. Nothing has been changed.';
  END IF;
  IF (SELECT telegram_user_id FROM merge_source.accounts) IS NULL THEN
    RAISE EXCEPTION 'MERGE ABORTED: the SOURCE account row has no Telegram user id, so the merged rows could never be matched to a login. Start the source backup container once under 8.0 (it claims its row on login), stop it, and re-run. Nothing has been changed.';
  END IF;
END $$;

-- Every data row in the source must belong to that single account.
DO $$
DECLARE
  t text;
  n bigint;
  src_id integer := (SELECT id FROM merge_source.accounts);
BEGIN
  FOREACH t IN ARRAY ARRAY['chats', 'messages', 'message_versions', 'media',
                           'reactions', 'sync_status', 'forum_topics',
                           'chat_folders', 'chat_folder_members'] LOOP
    EXECUTE format('SELECT count(*) FROM merge_source.%I WHERE account_id <> $1', t)
      INTO n USING src_id;
    IF n > 0 THEN
      RAISE EXCEPTION 'MERGE ABORTED: the SOURCE archive has % row(s) in % under an account_id that is not its single account row — it is not the clean single-account archive this script expects. Nothing has been changed.', n, t;
    END IF;
  END LOOP;
END $$;

-- Two archives of the same Telegram account are not a merge — refuse.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM accounts t
             JOIN merge_source.accounts s
               ON t.telegram_user_id = s.telegram_user_id) THEN
    RAISE EXCEPTION 'MERGE ABORTED: the source account''s Telegram user id already exists in the target. These are two archives of the SAME Telegram account; merging them is message-level deduplication, a different problem this script refuses to guess at. Nothing has been changed.';
  END IF;
END $$;

-- Imported chats KEEP their refs (they are 128-bit random, and the source
-- install's share links die with that install either way), so a collision must
-- be impossible rather than improbable: checked here, and enforced again by
-- uq_chats_ref during the insert.
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM merge_source.chats s JOIN chats t ON t.ref = s.ref;
  IF n > 0 THEN
    RAISE EXCEPTION 'MERGE ABORTED: % chat ref(s) in the source already exist in the target. Refs are 128-bit random values, so this does not happen to honestly generated archives — one of these databases has hand-copied rows. Nothing has been changed.', n;
  END IF;
END $$;

-- Nobody else may be connected to the TARGET database: a still-running backup
-- or viewer container would be writing and reading around this import.
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM pg_stat_activity
   WHERE datname = current_database()
     AND pid <> pg_backend_pid()
     AND backend_type = 'client backend';
  IF n > 0 THEN
    RAISE EXCEPTION 'MERGE ABORTED: % other session(s) are connected to the target database. Stop the backup and viewer containers, close any psql/pgAdmin sessions, and re-run. Nothing has been changed.', n;
  END IF;
END $$;

-- Nobody else may be connected to the SOURCE database either (our own fdw
-- connection announces itself as application_name 'ta_merge' and is excluded).
-- For a source restored as a scratch database this is trivially true; for a
-- live source server it is the "containers stopped" precondition, enforced.
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n
    FROM merge_source.remote_activity a, _merge_params p
   WHERE a.datname = p.src_db
     AND a.backend_type = 'client backend'
     AND a.application_name IS DISTINCT FROM 'ta_merge';
  IF n > 0 THEN
    RAISE EXCEPTION 'MERGE ABORTED: % other session(s) are connected to the source database. Stop the source install''s backup and viewer containers, then re-run. Nothing has been changed.', n;
  END IF;
END $$;

\echo 'preflight OK — importing'

-- ---------------------------------------------------------------------------
-- Expected row counts, captured BEFORE anything is written so the final
-- comparison is against a snapshot the import cannot have influenced.
-- For users (a global table — a Telegram user is the same person in both
-- archives) the expectation is "every source sender exists in the target
-- afterwards"; rows the target already had are kept as they are.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _merge_counts (table_name text NOT NULL, expected bigint NOT NULL, actual bigint)
  ON COMMIT DROP;

INSERT INTO _merge_counts (table_name, expected)
SELECT 'users (new senders)', count(*) FROM merge_source.users s
WHERE NOT EXISTS (SELECT 1 FROM users t WHERE t.id = s.id);

INSERT INTO _merge_counts (table_name, expected) SELECT 'chats',               count(*) FROM merge_source.chats;
INSERT INTO _merge_counts (table_name, expected) SELECT 'messages',            count(*) FROM merge_source.messages;
INSERT INTO _merge_counts (table_name, expected) SELECT 'message_versions',    count(*) FROM merge_source.message_versions;
INSERT INTO _merge_counts (table_name, expected) SELECT 'media',               count(*) FROM merge_source.media;
INSERT INTO _merge_counts (table_name, expected) SELECT 'reactions',           count(*) FROM merge_source.reactions;
INSERT INTO _merge_counts (table_name, expected) SELECT 'sync_status',         count(*) FROM merge_source.sync_status;
INSERT INTO _merge_counts (table_name, expected) SELECT 'forum_topics',        count(*) FROM merge_source.forum_topics;
INSERT INTO _merge_counts (table_name, expected) SELECT 'chat_folders',        count(*) FROM merge_source.chat_folders;
INSERT INTO _merge_counts (table_name, expected) SELECT 'chat_folder_members', count(*) FROM merge_source.chat_folder_members;

-- ---------------------------------------------------------------------------
-- The imported account row. id is deliberately omitted: the sequence assigns
-- the next free id — the id the source used (1) already belongs to the
-- target's own first account. The label rides along as-is; it is cosmetic,
-- and the first start rewrites it from the env's TG_ACCOUNT_<N>_LABEL anyway.
-- _merge_new_account captures the assigned id; every INSERT below stamps it
-- over the source's account_id.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _merge_new_account ON COMMIT DROP AS
WITH ins AS (
  INSERT INTO accounts (label, telegram_user_id)
  SELECT label, telegram_user_id FROM merge_source.accounts
  RETURNING id
)
SELECT id FROM ins;

-- ---------------------------------------------------------------------------
-- The import. Parents before children (users/chats/folders before messages
-- before media/reactions/versions) — PostgreSQL enforces every foreign key as
-- the rows land, so a wrong order or a wrong remap fails here, loudly, instead
-- of being committed. Every column is named explicitly: a migrated schema and
-- a freshly created one can order columns differently, and this script must
-- work identically on both.
--
-- users: target rows always win. The users table is global by design (see
-- src/db/models.py) with last-writer-wins display names; a merge keeps the
-- target's version of anyone both archives know.
-- ---------------------------------------------------------------------------
INSERT INTO users (id, username, first_name, last_name, phone, is_bot, created_at, updated_at)
SELECT s.id, s.username, s.first_name, s.last_name, s.phone, s.is_bot, s.created_at, s.updated_at
FROM merge_source.users s
WHERE NOT EXISTS (SELECT 1 FROM users t WHERE t.id = s.id);

INSERT INTO chats (account_id, id, ref, type, title, username, first_name, last_name, phone,
                   description, participants_count, is_forum, is_archived,
                   last_synced_message_id, created_at, updated_at)
SELECT (SELECT id FROM _merge_new_account), s.id, s.ref, s.type, s.title, s.username,
       s.first_name, s.last_name, s.phone, s.description, s.participants_count, s.is_forum,
       s.is_archived, s.last_synced_message_id, s.created_at, s.updated_at
FROM merge_source.chats s;

INSERT INTO chat_folders (account_id, id, title, emoticon, sort_order, created_at, updated_at)
SELECT (SELECT id FROM _merge_new_account), s.id, s.title, s.emoticon, s.sort_order,
       s.created_at, s.updated_at
FROM merge_source.chat_folders s;

INSERT INTO messages (account_id, id, chat_id, sender_id, sender_name, date, text,
                      reply_to_msg_id, reply_to_top_id, reply_to_text, forward_from_id,
                      edit_date, raw_data, created_at, is_outgoing, is_pinned,
                      is_deleted, deleted_at)
SELECT (SELECT id FROM _merge_new_account), s.id, s.chat_id, s.sender_id, s.sender_name,
       s.date, s.text, s.reply_to_msg_id, s.reply_to_top_id, s.reply_to_text,
       s.forward_from_id, s.edit_date, s.raw_data, s.created_at, s.is_outgoing,
       s.is_pinned, s.is_deleted, s.deleted_at
FROM merge_source.messages s;

INSERT INTO forum_topics (account_id, id, chat_id, title, icon_color, icon_emoji_id,
                          icon_emoji, is_closed, is_pinned, is_hidden, date,
                          created_at, updated_at)
SELECT (SELECT id FROM _merge_new_account), s.id, s.chat_id, s.title, s.icon_color,
       s.icon_emoji_id, s.icon_emoji, s.is_closed, s.is_pinned, s.is_hidden, s.date,
       s.created_at, s.updated_at
FROM merge_source.forum_topics s;

-- sync_status is what makes the first merged sweep incremental: the imported
-- last_message_id cursors mean account 2 resumes where the source install
-- stopped, instead of re-capturing every chat from message 1.
INSERT INTO sync_status (account_id, chat_id, last_message_id, last_sync_date, message_count)
SELECT (SELECT id FROM _merge_new_account), s.chat_id, s.last_message_id, s.last_sync_date,
       s.message_count
FROM merge_source.sync_status s;

INSERT INTO chat_folder_members (account_id, folder_id, chat_id)
SELECT (SELECT id FROM _merge_new_account), s.folder_id, s.chat_id
FROM merge_source.chat_folder_members s;

INSERT INTO media (account_id, id, message_id, chat_id, type, file_path, file_name,
                   file_size, mime_type, width, height, duration, content_hash,
                   downloaded, download_attempts, download_date, created_at)
SELECT (SELECT id FROM _merge_new_account), s.id, s.message_id, s.chat_id, s.type,
       s.file_path, s.file_name, s.file_size, s.mime_type, s.width, s.height, s.duration,
       s.content_hash, s.downloaded, s.download_attempts, s.download_date, s.created_at
FROM merge_source.media s;

-- message_versions and reactions have their own autoincrement ids in each
-- archive; the source's values would collide with the target's, so the id is
-- omitted and the target's sequences assign fresh ones. Nothing references
-- these ids.
INSERT INTO message_versions (account_id, message_id, chat_id, text, date, change_hash, captured_at)
SELECT (SELECT id FROM _merge_new_account), s.message_id, s.chat_id, s.text, s.date,
       s.change_hash, s.captured_at
FROM merge_source.message_versions s;

INSERT INTO reactions (account_id, message_id, chat_id, emoji, user_id, "count", created_at, removed_at)
SELECT (SELECT id FROM _merge_new_account), s.message_id, s.chat_id, s.emoji, s.user_id,
       s."count", s.created_at, s.removed_at
FROM merge_source.reactions s;

\echo 'import done — verifying'

-- ---------------------------------------------------------------------------
-- Verification. Actual counts are RE-COUNTED from the target — never assumed
-- from the statements above — and compared to the pre-import snapshot.
-- ---------------------------------------------------------------------------
UPDATE _merge_counts SET actual =
  expected - (SELECT count(*) FROM merge_source.users s
              WHERE NOT EXISTS (SELECT 1 FROM users t WHERE t.id = s.id))
WHERE table_name = 'users (new senders)';

UPDATE _merge_counts SET actual = (SELECT count(*) FROM chats               WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'chats';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM messages            WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'messages';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM message_versions    WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'message_versions';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM media               WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'media';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM reactions           WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'reactions';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM sync_status         WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'sync_status';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM forum_topics        WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'forum_topics';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM chat_folders        WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'chat_folders';
UPDATE _merge_counts SET actual = (SELECT count(*) FROM chat_folder_members WHERE account_id = (SELECT id FROM _merge_new_account)) WHERE table_name = 'chat_folder_members';

-- Any mismatch between the snapshot and what actually landed aborts the run.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(format('%s (expected %s, actual %s)', table_name, expected, actual), '; ')
    INTO bad
    FROM _merge_counts
   WHERE expected IS DISTINCT FROM actual;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MERGE FAILED VERIFICATION: imported row counts do not match the source: %. Nothing has been committed.', bad;
  END IF;
END $$;

-- Referential integrity, re-checked with explicit anti-joins rather than by
-- trusting the constraints that already enforced it during the insert — a
-- check that re-runs the logic can fail, a re-validation of an already-valid
-- constraint cannot. Composite-key rows with NULLs (media metadata rows) are
-- exempt from their FK by SQL semantics and are skipped the same way here.
DO $$
DECLARE
  new_id integer := (SELECT id FROM _merge_new_account);
  n bigint;
BEGIN
  SELECT count(*) INTO n FROM messages m
   WHERE m.account_id = new_id
     AND NOT EXISTS (SELECT 1 FROM chats c WHERE c.account_id = m.account_id AND c.id = m.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported message(s) reference a missing chat. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM sync_status ss
   WHERE ss.account_id = new_id
     AND NOT EXISTS (SELECT 1 FROM chats c WHERE c.account_id = ss.account_id AND c.id = ss.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported sync cursor(s) reference a missing chat. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM forum_topics ft
   WHERE ft.account_id = new_id
     AND NOT EXISTS (SELECT 1 FROM chats c WHERE c.account_id = ft.account_id AND c.id = ft.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported forum topic(s) reference a missing chat. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM chat_folder_members m
   WHERE m.account_id = new_id
     AND (NOT EXISTS (SELECT 1 FROM chat_folders f WHERE f.account_id = m.account_id AND f.id = m.folder_id)
          OR NOT EXISTS (SELECT 1 FROM chats c WHERE c.account_id = m.account_id AND c.id = m.chat_id));
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported folder membership(s) reference a missing folder or chat. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM media md
   WHERE md.account_id = new_id
     AND md.message_id IS NOT NULL AND md.chat_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM messages m
                      WHERE m.account_id = md.account_id AND m.id = md.message_id AND m.chat_id = md.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported media row(s) reference a missing message. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM message_versions v
   WHERE v.account_id = new_id
     AND NOT EXISTS (SELECT 1 FROM messages m
                      WHERE m.account_id = v.account_id AND m.id = v.message_id AND m.chat_id = v.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported edit version(s) reference a missing message. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM reactions r
   WHERE r.account_id = new_id
     AND NOT EXISTS (SELECT 1 FROM messages m
                      WHERE m.account_id = r.account_id AND m.id = r.message_id AND m.chat_id = r.chat_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported reaction(s) reference a missing message. Nothing has been committed.', n; END IF;

  SELECT count(*) INTO n FROM reactions r
   WHERE r.account_id = new_id
     AND r.user_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = r.user_id);
  IF n > 0 THEN RAISE EXCEPTION 'MERGE FAILED VERIFICATION: % imported reaction(s) reference a missing user. Nothing has been committed.', n; END IF;

  -- Refs must still be globally unique (uq_chats_ref enforced this during the
  -- insert; this recount is the independent proof).
  IF (SELECT count(*) FROM chats) <> (SELECT count(DISTINCT ref) FROM chats) THEN
    RAISE EXCEPTION 'MERGE FAILED VERIFICATION: chat refs are not globally unique after the import. Nothing has been committed.';
  END IF;
END $$;

-- Media roots: compares the directory prefix up to and including '/media/' of
-- one stored media path from each archive, reported as yes/no only (paths
-- carry chat ids and are never printed). A NO means the imported media rows
-- point at paths the target install does not serve — the media section of
-- docs/MERGING-ARCHIVES.md tells you how to remap them before starting.
DO $$
DECLARE t text; s text;
BEGIN
  SELECT substring(file_path from '^(.*?/media/)') INTO t
    FROM media WHERE file_path LIKE '%/media/%'
   LIMIT 1;
  SELECT substring(file_path from '^(.*?/media/)') INTO s
    FROM merge_source.media WHERE file_path LIKE '%/media/%'
   LIMIT 1;
  IF t IS NULL OR s IS NULL THEN
    RAISE NOTICE 'media roots match: n/a (no stored media paths to compare)';
  ELSIF t = s THEN
    RAISE NOTICE 'media roots match: yes';
  ELSE
    RAISE WARNING 'media roots match: NO — imported media rows point at a different directory root than the target serves. See the media section of docs/MERGING-ARCHIVES.md before starting the containers.';
  END IF;
END $$;

\echo ''
\echo '=== Imported rows, per table (counts only) ==='
SELECT table_name, expected, actual,
       CASE WHEN expected IS NOT DISTINCT FROM actual THEN 'ok' ELSE 'MISMATCH' END AS status
FROM _merge_counts;

\echo '=== Summary ==='
SELECT (SELECT id FROM _merge_new_account) AS imported_account_row,
       (SELECT count(*) FROM accounts)     AS accounts_total;

-- ---------------------------------------------------------------------------
-- Take the plumbing down again and keep the merge. Reaching this line at all
-- means every guard and every verification above passed; any earlier failure
-- rolled everything — data and plumbing alike — back.
-- ---------------------------------------------------------------------------
DROP FOREIGN TABLE merge_source.remote_activity;
DROP SCHEMA merge_source CASCADE;
DROP SERVER ta_merge_source_srv CASCADE;

COMMIT;

\echo ''
\echo 'MERGE COMMITTED. Next steps (docs/MERGING-ARCHIVES.md): copy the media'
\echo 'files, move the session file, declare the TG_ACCOUNT_<N>_* variables,'
\echo 'then start the target and watch its first sweep.'
