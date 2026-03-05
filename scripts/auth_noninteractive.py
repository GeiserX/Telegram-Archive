"""
Non-interactive Telegram authentication helper.

Use this script when you cannot run the interactive setup_auth
(e.g., from SSH without TTY, CI pipelines, or automation scripts).

Usage:
    # Step 1: Send verification code to Telegram app
    docker compose run --rm backup python scripts/auth_noninteractive.py send

    # Step 2: Enter the code (and 2FA password if enabled)
    docker compose run --rm backup python scripts/auth_noninteractive.py verify CODE
    docker compose run --rm backup python scripts/auth_noninteractive.py verify CODE 2FA_PASSWORD

Environment variables required (same as normal operation):
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SESSION_NAME, BACKUP_PATH
"""

import asyncio
import os
import sys

from telethon import TelegramClient


def _get_session_path() -> str:
    """Derive session path from environment (same logic as Config)."""
    backup_path = os.getenv("BACKUP_PATH", "/data/backups")
    session_dir = os.getenv("SESSION_DIR", os.path.join(os.path.dirname(backup_path.rstrip("/\\")), "session"))
    session_name = os.getenv("SESSION_NAME", "telegram_backup")
    os.makedirs(session_dir, exist_ok=True)
    return os.path.join(session_dir, session_name)


async def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)

    action = sys.argv[1]

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ["TELEGRAM_PHONE"]
    session_path = _get_session_path()

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    if action == "send":
        result = await client.send_code_request(phone)
        print(f"Verification code sent to Telegram app ({result.type.__class__.__name__})")
        print(f"Phone code hash: {result.phone_code_hash}")
        await client.disconnect()

    elif action == "verify":
        if len(sys.argv) < 3:
            print("Usage: python scripts/auth_noninteractive.py verify CODE [2FA_PASSWORD]")
            sys.exit(1)

        code = sys.argv[2]
        password = sys.argv[3] if len(sys.argv) > 3 else None

        # Must send_code_request first to establish the flow
        await client.send_code_request(phone)

        try:
            await client.sign_in(phone, code)
        except Exception as e:
            error_str = str(e)
            if (
                "Two-steps verification" in error_str
                or "password" in error_str.lower()
                or "SessionPasswordNeeded" in error_str
            ):
                if not password:
                    print("2FA is enabled. Re-run with: verify CODE 2FA_PASSWORD")
                    await client.disconnect()
                    sys.exit(1)
                await client.sign_in(password=password)
            else:
                print(f"Authentication failed: {e}")
                await client.disconnect()
                sys.exit(1)

        me = await client.get_me()
        print(f"Authenticated as {me.first_name} (@{me.username})")
        await client.disconnect()

    else:
        print(f"Unknown action: {action}")
        print("Usage: send | verify CODE [2FA_PASSWORD]")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
