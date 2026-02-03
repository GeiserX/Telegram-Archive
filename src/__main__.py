"""
Unified CLI entry point for Telegram Archive.

Provides a single interface for all backup operations including authentication,
backup execution, scheduling, and data export.
"""

import sys
import argparse
import asyncio
from typing import Optional


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog='telegram-archive',
        description='Telegram Archive - Automated Telegram Backup',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
GETTING STARTED:

  1. First time setup (authenticate with Telegram):
     python -m src auth

  2. Run backup:
     python -m src backup          # One-time manual backup
     python -m src schedule        # Continuous scheduled backups (recommended)

  3. View and export data:
     python -m src list-chats      # List all backed up chats
     python -m src stats           # Show backup statistics
     python -m src export -o file.json  # Export to JSON

DOCKER USAGE:

  Authentication (first time only):
    docker run -it --rm --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:latest \\
      python -m src auth

  Start scheduled backups:
    docker run -d --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:latest \\
      python -m src schedule

For more information, visit: https://github.com/GeiserX/Telegram-Archive
"""
    )

    subparsers = parser.add_subparsers(
        dest='command',
        help='Command to execute',
        metavar='<command>'
    )

    # Auth command
    auth_parser = subparsers.add_parser(
        'auth',
        help='Authenticate with Telegram (interactive)',
        description='Set up Telegram authentication. Creates a session file for future use.'
    )

    # Backup command
    backup_parser = subparsers.add_parser(
        'backup',
        help='Run backup once',
        description='Execute a one-time backup of all configured chats.'
    )

    # Schedule command
    schedule_parser = subparsers.add_parser(
        'schedule',
        help='Run scheduled backups (default for Docker)',
        description='Start the backup scheduler. Runs backups according to SCHEDULE env variable.'
    )

    # Export command
    export_parser = subparsers.add_parser(
        'export',
        help='Export messages to JSON',
        description='Export backup data to JSON format with optional filtering.'
    )
    export_parser.add_argument(
        '-o', '--output',
        required=True,
        help='Output JSON file path'
    )
    export_parser.add_argument(
        '-c', '--chat-id',
        type=int,
        help='Filter by specific chat ID'
    )
    export_parser.add_argument(
        '-s', '--start-date',
        help='Start date (YYYY-MM-DD)'
    )
    export_parser.add_argument(
        '-e', '--end-date',
        help='End date (YYYY-MM-DD)'
    )

    # Stats command
    stats_parser = subparsers.add_parser(
        'stats',
        help='Show backup statistics',
        description='Display statistics about backed up chats, messages, and media.'
    )

    # List chats command
    list_parser = subparsers.add_parser(
        'list-chats',
        help='List all backed up chats',
        description='Show a table of all chats in the backup database.'
    )

    return parser


async def run_export(args) -> int:
    """Run export command."""
    from .export_backup import BackupExporter
    from .config import Config, setup_logging
    from .db import init_database, close_database

    try:
        config = Config()
        setup_logging(config)

        exporter = await BackupExporter.create(config)
        try:
            await exporter.export_to_json(
                args.output,
                args.chat_id,
                args.start_date,
                args.end_date
            )
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1


async def run_stats(args) -> int:
    """Run stats command."""
    from .export_backup import BackupExporter
    from .config import Config, setup_logging

    try:
        config = Config()
        setup_logging(config)

        exporter = await BackupExporter.create(config)
        try:
            await exporter.show_statistics()
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"Stats failed: {e}", file=sys.stderr)
        return 1


async def run_list_chats(args) -> int:
    """Run list-chats command."""
    from .export_backup import BackupExporter
    from .config import Config, setup_logging

    try:
        config = Config()
        setup_logging(config)

        exporter = await BackupExporter.create(config)
        try:
            await exporter.list_chats()
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"List chats failed: {e}", file=sys.stderr)
        return 1


def run_auth(args) -> int:
    """Run authentication setup."""
    from .setup_auth import main as auth_main
    return auth_main()


def run_backup(args) -> int:
    """Run one-time backup."""
    from .telegram_backup import main as backup_main
    return backup_main()


def run_schedule(args) -> int:
    """Run scheduled backups."""
    from .scheduler import main as scheduler_main
    return scheduler_main()


def main() -> int:
    """Main entry point."""
    parser = create_parser()

    # If no arguments, show help
    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()

    # Dispatch to appropriate command
    if args.command == 'auth':
        return run_auth(args)
    elif args.command == 'backup':
        return run_backup(args)
    elif args.command == 'schedule':
        return run_schedule(args)
    elif args.command == 'export':
        return asyncio.run(run_export(args))
    elif args.command == 'stats':
        return asyncio.run(run_stats(args))
    elif args.command == 'list-chats':
        return asyncio.run(run_list_chats(args))
    else:
        parser.print_help()
        return 0


if __name__ == '__main__':
    sys.exit(main())
