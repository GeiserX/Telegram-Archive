"""
Interactive authentication setup for Telegram.
Run this script once to authenticate and save the session file.
"""

import asyncio
import logging
import os
import sqlite3
import sys

from telethon import TelegramClient


def _print_permission_error_help():
    """Print helpful guidance for permission errors."""
    print("\n" + "=" * 60)
    print("PERMISSION ERROR - Unable to write to session directory")
    print("=" * 60)
    print("\nThis often happens when your user ID doesn't match the")
    print("container's UID (1000). Common solutions:\n")
    print("For Podman users:")
    print("  Add --userns=keep-id:uid=1000,gid=1000 to your run command:")
    print("  podman run --userns=keep-id:uid=1000,gid=1000 -it --rm ...")
    print("\nFor Docker users:")
    print("  Ensure the data directory is owned by UID 1000:")
    print("  mkdir -p data && sudo chown -R 1000:1000 data")
    print("\nAlternatively, run the container with your host UID:")
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    print(f"  docker run --user {uid}:{gid} ...")
    print("=" * 60)


logger = logging.getLogger(__name__)


def _same_phone_number(left: str | None, right: str | None) -> bool:
    """Compare two phone numbers by digits alone.

    Telegram returns ``me.phone`` without a leading ``+`` while TELEGRAM_PHONE
    is normally written with one, so comparing the raw strings would report a
    mismatch for every correctly configured account. Separators vary too. Only
    the digits are meaningful, and neither value is ever logged — the caller
    logs the boolean.
    """
    if not left or not right:
        return False
    return "".join(filter(str.isdigit, left)) == "".join(filter(str.isdigit, right))


async def setup_authentication():
    """Interactive authentication setup."""
    try:
        # Load configuration
        from .config import Config, setup_logging

        config = Config()
        config.validate_credentials()
        setup_logging(config)

        logger.info("=" * 60)
        logger.info("Telegram Authentication Setup")
        logger.info("=" * 60)
        # The phone number is deliberately not echoed. This flow looks
        # interactive, but it emits through setup_logging -> basicConfig ->
        # stderr, and where that stream ends up is the OPERATOR's choice, not
        # ours: docker-compose.yml declares no logging driver, so under
        # journald, syslog, gelf, fluentd or a Loki plugin every line is shipped
        # off-box the moment it is written and outlives the --rm container that
        # produced it. That is the argument; "docker logs captures it" is not,
        # since `docker compose exec` output never reaches docker logs at all
        # and --rm deletes the json-file log on exit.
        logger.info(f"Session will be saved to: {config.session_path}")
        logger.info("=" * 60)

        # Create Telegram client
        client = TelegramClient(
            config.session_path,
            config.api_id,
            config.api_hash,
            **config.get_telegram_client_kwargs(),
        )

        # Connect and authenticate
        logger.info("Connecting to Telegram...")
        await client.connect()

        if not await client.is_user_authorized():
            logger.info("Not authorized yet. Starting authentication process...")

            # Send code request
            await client.send_code_request(config.phone)
            print("\n" + "=" * 60)
            print("A verification code has been sent to your Telegram app.")
            print("Please check your Telegram and enter the code below.")
            print("=" * 60)

            # Get code from user
            code = input("Enter verification code: ").strip()

            try:
                # Sign in with code
                await client.sign_in(config.phone, code)
                logger.info("Authentication successful!")

            except Exception as e:
                # If code is wrong or 2FA is enabled
                if "Two-steps verification" in str(e) or "password" in str(e).lower():
                    print("\n" + "=" * 60)
                    print("Two-factor authentication is enabled on your account.")
                    print("=" * 60)
                    password = input("Enter your 2FA password: ").strip()
                    await client.sign_in(password=password)
                    logger.info("Authentication successful with 2FA!")
                else:
                    raise
        else:
            logger.info("Already authorized!")

        # Confirm WHICH account answered, without naming it. The session path
        # below cannot do this: SESSION_NAME defaults to "telegram_backup" in
        # config.py, docker-compose.yml and .env.example alike, so it is the
        # same string for every user — and even when it is customised it names
        # the destination slot the operator chose, not the identity Telegram
        # returned. Comparing the two is what actually answers "did I
        # authenticate the account I meant to?", and it answers it for the
        # `Already authorized!` branch above, where a stale session file from a
        # different account would otherwise pass in silence. The result is a
        # boolean, so nothing identifying reaches the log.
        me = await client.get_me()
        account_matches_configured_phone = _same_phone_number(me.phone, config.phone)
        logger.info("=" * 60)
        logger.info("Authentication verified!")
        logger.info(f"Authenticated account matches TELEGRAM_PHONE: {account_matches_configured_phone}")
        if not account_matches_configured_phone:
            logger.warning(
                "The authorized session belongs to a different number than TELEGRAM_PHONE. "
                "Delete the session file and re-run this setup if that is not intended."
            )
        logger.info("=" * 60)
        logger.info(f"Session saved to: {config.session_path}")
        logger.info("You can now use this session with Docker or the scheduler.")
        logger.info("=" * 60)

        await client.disconnect()

        return True

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Please check your .env file and ensure all required variables are set.")
        return False
    except PermissionError as e:
        logger.error(f"Permission denied: {e}")
        _print_permission_error_help()
        return False
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e).lower():
            logger.error(f"Database error: {e}")
            _print_permission_error_help()
        else:
            logger.error(f"Database error: {e}", exc_info=True)
        return False
    except Exception as e:
        # Check if it's a permission-related error wrapped in another exception
        error_msg = str(e).lower()
        if "permission denied" in error_msg or "unable to open database file" in error_msg:
            logger.error(f"Authentication failed: {e}")
            _print_permission_error_help()
        else:
            logger.error(f"Authentication failed: {e}", exc_info=True)
        return False


def main():
    """Main entry point."""
    print("\n" + "=" * 60)
    print("Telegram Backup - Authentication Setup")
    print("=" * 60)
    print("\nThis script will authenticate your Telegram account and save")
    print("the session file for use with the backup automation.")
    print("\nMake sure you have:")
    print("  1. Created a .env file with your API credentials")
    print("  2. Access to your Telegram account to receive verification code")
    print("\n" + "=" * 60 + "\n")

    # Run authentication
    success = asyncio.run(setup_authentication())

    if success:
        print("\n✓ Setup completed successfully!")
        print("\nNext steps:")
        print("  1. Review your .env configuration")
        print("  2. Run 'python scheduler.py' to start scheduled backups")
        print("  3. Or build and run the Docker container")
        sys.exit(0)
    else:
        print("\n✗ Setup failed. Please check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
