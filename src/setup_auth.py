"""
Interactive authentication setup for Telegram.
Run this script once to authenticate and save the session file.
"""

import asyncio
import logging
import sys
from pathlib import Path

from telethon import TelegramClient
from .config import Config, setup_logging

logger = logging.getLogger(__name__)


async def setup_authentication():
    """Interactive authentication setup."""
    try:
        # Load configuration
        from .config import Config, setup_logging
        config = Config()
        setup_logging(config)
        
        logger.info("=" * 60)
        logger.info("Telegram Authentication Setup")
        logger.info("=" * 60)
        logger.info(f"Phone: {config.phone}")
        logger.info(f"Session will be saved to: {config.session_path}")
        logger.info("=" * 60)
        
        # Create Telegram client
        client = TelegramClient(
            config.session_path,
            config.api_id,
            config.api_hash
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
        
        # Verify authentication
        me = await client.get_me()
        logger.info("=" * 60)
        logger.info("Authentication verified!")
        logger.info(f"Logged in as: {me.first_name} {me.last_name or ''}")
        logger.info(f"Username: @{me.username}" if me.username else "Username: (none)")
        logger.info(f"Phone: {me.phone}")
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
    except Exception as e:
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


if __name__ == '__main__':
    main()
