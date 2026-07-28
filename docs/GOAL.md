# GOAL

_Verbatim directive — 2026-07-06_

Drive the enhancement in Telegram-Archive: back up a Telegram folder's FULL membership, not just its include_peers. Today _backup_folders (src/telegram_backup.py) only reads a DialogFilter's include_peers, so folders defined by pinned_peers or Telegram's flag-based inclusion (contacts, non_contacts, groups, broadcasts, bots, and the exclude_* flags) get no chat_folder_members rows — after the #208 fix those folders are hidden entirely. Resolve each folder's effective membership against the chats we actually archived and persist it, so those folders show their backed-up chats in the viewer. Research → implement → review-pr → merge → release, autonomously.

## 2026-07-20 — continuation

until this is fixed https://github.com/GeiserX/Telegram-Archive/issues/224 then merge release, test on my servers telegramm-archives. 
make sure you answer close issue and thank him afterwards

## 2026-07-23 — continuation

about these 2 issues https://github.com/GeiserX/Telegram-Archive/issues
---
ponder over them, really investigate and then, if you think its fine:
/sergio-loop over them until all is merged released and both Issues answered and closed, inviting him to test the result

## 2026-07-25 — continuation

Check latest issues in the telegram-archive repo, check for ai slop and if they are worth tracking, /sergio-loop over them until all is merged and released, and issues closed and answered

## 2026-07-28 — continuation

Think well how if https://github.com/GeiserX/Telegram-Archive issues are ai slop or are actually interesting to build or not, then again if interesting, bringing good ideas, and improving parity with the real ios/android apps (or other official ones) is good /sergio-loop over them when really planning and assessing everything
