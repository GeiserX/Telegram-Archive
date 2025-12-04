# Finding Chat IDs in Telegram

There are several ways to find chat IDs before running the backup:

## Method 1: Use @userinfobot in Telegram

1. Open the chat/channel/group you want to find the ID for
2. Add **@userinfobot** to the chat (for groups/channels)  
   OR just send `/start` to @userinfobot directly (for channels you own)
3. The bot will reply with the chat ID

## Method 2: Telegram Web (Desktop)

1. Open https://web.telegram.org
2. Navigate to the chat/channel/group
3. Look at the URL:
   - For channels: `https://web.telegram.org/#/im?p=c1234567890_...`  
     The ID is `-100` + the number after `c` = `-1001234567890`
   - For private chats: The number in the URL directly
   - For groups: Similar to channels

## Method 3: Check Backup Logs (Current Method)

Run the backup once with verbose logging:
```bash
docker-compose logs -f telegram-backup
```

You'll see lines like:
```
[1/50] Backing up: John Doe (ID: 123456789)
[2/50] Backing up: My Channel (ID: -1001234567890)
```

## Method 4: Add a `--list-chats` Flag (Future Enhancement)

We could add a CLI flag to list all chats without backing them up:
```bash
docker-compose run telegram-backup python -m src.telegram_backup --list-chats
```

This would output:
```
Available chats:
  - John Doe (ID: 123456789, Type: private)
  - My Group (ID: -987654321, Type: group)
  - News Channel (ID: -1001234567890, Type: channel)
```

Would you like me to implement this `--list-chats` feature?

## Quick Tip: ID Format

- **Private chats**: Positive number (e.g., `123456789`)
- **Groups**: Negative number (e.g., `-987654321`)
- **Channels/Supergroups**: Very large negative number starting with `-100` (e.g., `-1001234567890`)
