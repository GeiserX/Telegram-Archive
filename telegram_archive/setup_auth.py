"""
Interactive authentication setup for Telegram.
Run this script once to authenticate and save the session file.
"""

import asyncio
import getpass
import logging
import os
import sqlite3
import sys

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Each QR login token lives about 30 seconds, so this waits about 5 minutes
# for the scan before giving up.
QR_LOGIN_MAX_TOKENS = 10


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


def _normalised_phone_digits(number: str) -> str:
    """Digits only, with an international call prefix removed.

    ``+34…`` and ``0034…`` are the same number written two ways, and both are
    in common use. Keeping the ``00`` would make them compare unequal.
    """
    digits = "".join(filter(str.isdigit, number))
    return digits[2:] if digits.startswith("00") else digits


def _same_phone_number(left: str | None, right: str | None) -> bool:
    """Compare two phone numbers by their digits alone.

    Telegram returns ``me.phone`` without a leading ``+`` while TELEGRAM_PHONE
    is normally written with one, so comparing the raw strings would report a
    mismatch for every correctly configured account. Separators and the ``00``
    prefix vary too. Neither value is ever logged — the caller logs the boolean.

    This deliberately does NOT match on a shared suffix. The caller fails closed
    on a mismatch, so a loose comparison would hide the stale-session case this
    exists to catch; a strict one can only ever cost a clear error message
    telling the operator to check how the number is written.
    """
    if not left or not right:
        return False
    return _normalised_phone_digits(left) == _normalised_phone_digits(right)


def _print_qr(url: str) -> None:
    """Draw ``url`` as a QR code on stdout, then the URL itself.

    ``tty=True`` makes qrcode print fixed white-on-black ANSI colours, so the
    code scans whatever the terminal theme; it raises when stdout is not a
    terminal, hence ``isatty()``. Without a terminal the inverted plain block
    is printed, which is the right polarity on a dark background.
    """
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(tty=sys.stdout.isatty(), invert=True)
    # The URL is a one-shot login token that expires in about 30 seconds. It
    # is the fallback for terminals that mangle the block characters.
    print(f"\nOr open this link on the phone that is logged in: {url}")


def _print_2fa_header() -> None:
    print("\n" + "=" * 60)
    print("Two-factor authentication is enabled on your account.")
    print("=" * 60)


async def _qr_login(client) -> None:
    """Log the client in by QR scan; returns once Telegram reports the login.

    Raises on failure so the caller's existing exception handling applies.
    Nothing here logs the phone, the user or the token.
    """
    print("\n" + "=" * 60)
    print("Scan this QR code with a Telegram app that is already logged in")
    print("to this account: Settings > Devices > Link Desktop Device.")
    print("The code refreshes by itself when it expires.")
    print("=" * 60)

    qr = await client.qr_login()
    for attempt in range(QR_LOGIN_MAX_TOKENS):
        if attempt:
            await qr.recreate()
        _print_qr(qr.url)
        try:
            # With no timeout, wait() runs until this token expires.
            await qr.wait()
            return
        except TimeoutError:
            continue
        except SessionPasswordNeededError:
            _print_2fa_header()
            password = getpass.getpass("Enter your 2FA password: ")
            await client.sign_in(password=password)
            logger.info("Authentication successful with 2FA!")
            return
    raise TimeoutError(
        f"The QR code expired {QR_LOGIN_MAX_TOKENS} times without being scanned. "
        "Run the login again when the phone is ready."
    )


async def _authorize_account(config, account, phone_var: str, qr: bool = False) -> bool:
    """Interactively authorize one configured account; True once verified.

    The flow self-skips the interactive part when the account's session file is
    already authorized, so re-running the setup only prompts for accounts that
    still need a login. ``phone_var`` is the NAME of the variable the expected
    number came from (``TELEGRAM_PHONE`` or ``TG_ACCOUNT_<N>_PHONE_NUMBER``) —
    messages name the variable, never its value. With ``qr`` the login is a QR
    scan and no code is requested (#590).
    """
    # The phone number is deliberately not echoed. This flow looks
    # interactive, but it emits through setup_logging -> basicConfig ->
    # stderr, and where that stream ends up is the OPERATOR's choice, not
    # ours: docker-compose.yml declares no logging driver, so under
    # journald, syslog, gelf, fluentd or a Loki plugin every line is shipped
    # off-box the moment it is written and outlives the --rm container that
    # produced it. That is the argument; "docker logs captures it" is not,
    # since `docker compose exec` output never reaches docker logs at all
    # and --rm deletes the json-file log on exit.
    logger.info(f"Session will be saved to: {account.session_path}")
    logger.info("=" * 60)

    # Create Telegram client
    client = TelegramClient(
        account.session_path,
        account.api_id,
        account.api_hash,
        **config.get_telegram_client_kwargs(),
    )

    # Connect and authenticate
    logger.info("Connecting to Telegram...")
    await client.connect()

    if not await client.is_user_authorized():
        logger.info("Not authorized yet. Starting authentication process...")

        if qr:
            await _qr_login(client)
        else:
            # Send code request
            await client.send_code_request(account.phone)
            print("\n" + "=" * 60)
            print("A verification code has been sent to your Telegram app.")
            print("Please check your Telegram and enter the code below.")
            print("If no code arrives, type qr to log in with a QR code instead.")
            print("=" * 60)

            # Get code from user
            code = input("Enter verification code (or qr): ").strip()

            if code.lower() == "qr":
                await _qr_login(client)
            else:
                try:
                    # Sign in with code
                    await client.sign_in(account.phone, code)

                except Exception as e:
                    # If code is wrong or 2FA is enabled
                    if "Two-steps verification" in str(e) or "password" in str(e).lower():
                        _print_2fa_header()
                        password = input("Enter your 2FA password: ").strip()
                        await client.sign_in(password=password)
                        logger.info("Authentication successful with 2FA!")
                    else:
                        raise
        logger.info("Authentication successful!")
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
    # get_me() returns None on an unauthorized session rather than raising,
    # so read through it defensively: a session that died between the check
    # above and here must not crash with an AttributeError.
    account_matches_configured_phone = _same_phone_number(getattr(me, "phone", None), account.phone)
    logger.info("=" * 60)
    logger.info("Authentication verified!")
    logger.info(f"Authenticated account matches {phone_var}: {account_matches_configured_phone}")
    if not account_matches_configured_phone:
        # Fail closed. Reporting the mismatch and returning success anyway
        # would let main() announce a completed setup while the session
        # belongs to another account — the daemons check authorization but
        # never identity, so nothing downstream would catch it either.
        logger.error(
            f"The authorized session does not belong to {phone_var}. "
            "Delete the session file and re-run this setup, or correct "
            f"{phone_var} if the number is written in a different form."
        )
        await client.disconnect()
        return False
    logger.info("=" * 60)
    logger.info(f"Session saved to: {account.session_path}")
    logger.info("You can now use this session with Docker or the scheduler.")
    logger.info("=" * 60)

    await client.disconnect()

    return True


async def setup_authentication(qr: bool = False):
    """Interactive authentication setup, walking every configured account.

    A single (zero-config) account keeps the exact pre-8.0 flow. With indexed
    accounts each is authorized in turn — already-authorized sessions need no
    interaction — and a failure stops the walk so the operator can fix that
    account and re-run (finished accounts then skip straight through). With
    ``qr`` every account still to log in shows its own QR code.
    """
    try:
        # Load configuration
        from .config import Config, setup_logging

        config = Config()
        config.validate_credentials()
        setup_logging(config)
        config.log_summary()

        logger.info("=" * 60)
        logger.info("Telegram Authentication Setup")
        logger.info("=" * 60)

        multi = len(config.accounts) > 1
        for account in config.accounts:
            if multi:
                # Index only: the label and phone stay out of the log (#272).
                logger.info(f"--- Account {account.index} ---")
            # Name the variable the expected phone number came from.
            # ``_indexed_accounts`` is Config's own flag for "TG_ACCOUNT_* is
            # in effect"; it is read here only to pick the right NAME — the
            # credentials themselves always come from the account entry.
            if config._indexed_accounts:
                phone_var = f"TG_ACCOUNT_{account.index}_PHONE_NUMBER"
            else:
                phone_var = "TELEGRAM_PHONE"
            if not await _authorize_account(config, account, phone_var, qr=qr):
                return False

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


def main(qr: bool = False):
    """Main entry point."""
    print("\n" + "=" * 60)
    print("Telegram Backup - Authentication Setup")
    print("=" * 60)
    print("\nThis script will authenticate your Telegram account and save")
    print("the session file for use with the backup automation.")
    print("\nMake sure you have:")
    print("  1. Created a .env file with your API credentials")
    print("  2. A Telegram app logged in to the account, to receive the code or scan the QR")
    print("\n" + "=" * 60 + "\n")

    # Run authentication
    success = asyncio.run(setup_authentication(qr=qr))

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
