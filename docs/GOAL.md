# GOAL

_Verbatim directive — 2026-07-06_

Drive the enhancement in Telegram-Archive: back up a Telegram folder's FULL membership, not just its include_peers. Today _backup_folders (src/telegram_backup.py) only reads a DialogFilter's include_peers, so folders defined by pinned_peers or Telegram's flag-based inclusion (contacts, non_contacts, groups, broadcasts, bots, and the exclude_* flags) get no chat_folder_members rows — after the #208 fix those folders are hidden entirely. Resolve each folder's effective membership against the chats we actually archived and persist it, so those folders show their backed-up chats in the viewer. Research → implement → review-pr → merge → release, autonomously.
