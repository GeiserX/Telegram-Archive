-- Migration: Convert chat IDs from raw (positive) to marked (negative) format
-- SQLite version for Telegram-Archive v4.0.6
--
-- This fixes the v4.0.5 inconsistency where some code used entity.id and some used get_peer_id()
--
-- ID Format Rules:
-- - Users (type='private'): no change (stay positive)
-- - Basic groups (Chat): marked_id = -id (only for id < 1000000000)
-- - Supergroups/Channels: marked_id = -1000000000000 - id (for id >= 1000000000)
--
-- Run this BEFORE updating to v4.0.6 or later.

-- Enable foreign keys check
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- 1. Update chats table first
-- type='channel' -> always Channel, use channel formula
UPDATE chats 
SET id = -1000000000000 - id 
WHERE type = 'channel' AND id > 0;

-- type='group' with large ID (>= 1 billion) -> Channel.megagroup
UPDATE chats 
SET id = -1000000000000 - id 
WHERE type = 'group' AND id >= 1000000000;

-- type='group' with small ID (< 1 billion) -> basic Chat
UPDATE chats 
SET id = -id 
WHERE type = 'group' AND id > 0 AND id < 1000000000;

-- type='private' stays positive (no change needed)

-- 2. Update messages table (references chats)
-- Same logic based on original positive IDs
UPDATE messages 
SET chat_id = -1000000000000 - chat_id 
WHERE chat_id >= 1000000000;

UPDATE messages 
SET chat_id = -chat_id 
WHERE chat_id > 0 AND chat_id < 1000000000;

-- 3. Update reactions table (if exists)
UPDATE reactions 
SET chat_id = -1000000000000 - chat_id 
WHERE chat_id >= 1000000000;

UPDATE reactions 
SET chat_id = -chat_id 
WHERE chat_id > 0 AND chat_id < 1000000000;

-- 4. Update sync_status table (if exists)
UPDATE sync_status 
SET chat_id = -1000000000000 - chat_id 
WHERE chat_id >= 1000000000;

UPDATE sync_status 
SET chat_id = -chat_id 
WHERE chat_id > 0 AND chat_id < 1000000000;

COMMIT;

-- Re-enable foreign keys
PRAGMA foreign_keys = ON;

-- Verify migration
SELECT 
    type,
    COUNT(*) as count,
    MIN(id) as min_id,
    MAX(id) as max_id
FROM chats 
GROUP BY type 
ORDER BY type;
