-- Fix Reactions Sequence (PostgreSQL only)
-- =========================================
--
-- WHEN TO USE:
-- If you see this error during backup:
--   "duplicate key value violates unique constraint "reactions_pkey""
--   "Key (id)=(XXXX) already exists"
--
-- WHY IT HAPPENS:
-- The PostgreSQL sequence for reactions.id got out of sync with the actual
-- data. This commonly occurs after:
--   - Database restores from backup
--   - Manual data imports
--   - Database migrations between versions
--
-- SOLUTION:
-- Run this script to reset the sequence to the correct value.
--
-- HOW TO RUN:
--   docker exec -i <postgres-container> psql -U telegram -d telegram_backup < fix_reactions_sequence.sql
--
-- Example:
--   docker exec -i telegram-postgres psql -U telegram -d telegram_backup < fix_reactions_sequence.sql
--
-- NOTE: v4.1.2+ automatically recovers from this issue, but you can run this
-- script manually if you're on an older version or want to fix it immediately.

-- Reset the reactions sequence to max(id) + 1
SELECT setval('reactions_id_seq', COALESCE((SELECT MAX(id) FROM reactions), 0) + 1, false);

-- Verify the fix
SELECT 
    'Current sequence value' as info,
    last_value as value 
FROM reactions_id_seq
UNION ALL
SELECT 
    'Max ID in reactions table' as info,
    COALESCE(MAX(id), 0) as value 
FROM reactions;
