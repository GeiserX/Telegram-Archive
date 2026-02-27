"""
Unified CLI entry point for Telegram Archive.

Provides a single interface for all backup operations including authentication,
backup execution, scheduling, and data export.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="telegram-archive",
        description="Telegram Archive - Automated Telegram Backup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
GETTING STARTED:

  1. First time setup (authenticate with Telegram):
     telegram-archive auth

  2. Run backup:
     telegram-archive backup       # One-time manual backup
     telegram-archive schedule     # Continuous scheduled backups (recommended)

  3. View and export data:
     telegram-archive list-chats   # List all backed up chats
     telegram-archive stats        # Show backup statistics
     telegram-archive export -o file.json  # Export to JSON

  4. Import Telegram Desktop exports:
     telegram-archive import -p /path/to/export
     telegram-archive import -p /path/to/export -c -1001234567890 --merge

LOCAL DEVELOPMENT:

  Use --data-dir to specify an alternative data location (default: /data):
    telegram-archive --data-dir ./data list-chats
    telegram-archive --data-dir ~/telegram-data backup

  Or use the Python module directly:
    python -m src --data-dir ./data list-chats

DOCKER USAGE:

  Authentication (first time only):
    docker run -it --rm --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:<version> \\
      python -m src auth

  Start scheduled backups:
    docker run -d --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:<version> \\
      python -m src schedule

For more information, visit: https://github.com/GeiserX/Telegram-Archive
""",
    )

    # Add top-level options (before subcommands)
    parser.add_argument(
        "--data-dir", metavar="PATH", help="Base data directory (default: /data). Sets BACKUP_PATH to PATH/backups"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute", metavar="<command>")

    # Auth command
    auth_parser = subparsers.add_parser(
        "auth",
        help="Authenticate with Telegram (interactive)",
        description="Set up Telegram authentication. Creates a session file for future use.",
    )

    # Backup command
    backup_parser = subparsers.add_parser(
        "backup", help="Run backup once", description="Execute a one-time backup of all configured chats."
    )

    # Schedule command
    schedule_parser = subparsers.add_parser(
        "schedule",
        help="Run scheduled backups (default for Docker)",
        description="Start the backup scheduler. Runs backups according to SCHEDULE env variable.",
    )

    # Export command
    export_parser = subparsers.add_parser(
        "export",
        help="Export messages to JSON",
        description="Export backup data to JSON format with optional filtering.",
    )
    export_parser.add_argument("-o", "--output", required=True, help="Output JSON file path")
    export_parser.add_argument("-c", "--chat-id", type=int, help="Filter by specific chat ID")
    export_parser.add_argument("-s", "--start-date", help="Start date (YYYY-MM-DD)")
    export_parser.add_argument("-e", "--end-date", help="End date (YYYY-MM-DD)")

    # Stats command
    stats_parser = subparsers.add_parser(
        "stats",
        help="Show backup statistics",
        description="Display statistics about backed up chats, messages, and media.",
    )

    # List chats command
    list_parser = subparsers.add_parser(
        "list-chats", help="List all backed up chats", description="Show a table of all chats in the backup database."
    )

    # Import command
    import_parser = subparsers.add_parser(
        "import",
        help="Import Telegram Desktop chat export",
        description="Import a Telegram Desktop chat export (result.json + media) into the database.",
    )
    import_parser.add_argument("-p", "--path", required=True, help="Path to export folder containing result.json")
    import_parser.add_argument(
        "-c", "--chat-id", type=int, help="Override chat ID (marked format, e.g. -1001234567890)"
    )
    import_parser.add_argument(
        "--dry-run", action="store_true", help="Parse and validate without writing to DB or copying media"
    )
    import_parser.add_argument(
        "--skip-media", action="store_true", help="Import only messages/metadata, skip media files"
    )
    import_parser.add_argument(
        "--merge", action="store_true", help="Allow importing into a chat that already has messages"
    )

    return parser


async def run_export(args) -> int:
    """Run export command."""
    from .config import Config, setup_logging
    from .export_backup import BackupExporter

    try:
        config = Config()
        setup_logging(config)

        exporter = await BackupExporter.create(config)
        try:
            await exporter.export_to_json(args.output, args.chat_id, args.start_date, args.end_date)
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1


async def run_stats(args) -> int:
    """Run stats command."""
    from .config import Config, setup_logging
    from .export_backup import BackupExporter

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
    from .config import Config, setup_logging
    from .export_backup import BackupExporter

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


async def run_import(args) -> int:
    """Run import command."""
    from .config import Config, setup_logging
    from .telegram_import import TelegramImporter

    try:
        config = Config()
        setup_logging(config)

        importer = await TelegramImporter.create(config.media_path)
        try:
            summary = await importer.run(
                export_path=args.path,
                chat_id_override=args.chat_id,
                dry_run=args.dry_run,
                skip_media=args.skip_media,
                merge=args.merge,
            )
            prefix = "[DRY RUN] " if args.dry_run else ""
            print(f"\n{prefix}Import complete:")
            print(f"  Chats: {summary['chats_imported']}")
            print(f"  Messages: {summary['total_messages']}")
            print(f"  Media files: {summary['total_media']}")
            for detail in summary["details"]:
                print(
                    f"  - {detail['chat_name']} (ID {detail['chat_id']}): "
                    f"{detail['messages']} messages, {detail['media']} media"
                )
        finally:
            await importer.close()
        return 0
    except Exception as e:
        print(f"Import failed: {e}", file=sys.stderr)
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

    return asyncio.run(scheduler_main())


def main() -> int:
    """Main entry point."""
    parser = create_parser()

    # If no arguments, show help
    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()

    # Handle --data-dir option
    if args.data_dir:
        data_path = Path(args.data_dir).resolve()
        backup_path = data_path / "backups"
        session_path = data_path / "session"

        # Set environment variables that Config will read
        os.environ["BACKUP_PATH"] = str(backup_path)
        os.environ["SESSION_DIR"] = str(session_path)

        # Create directories if they don't exist
        backup_path.mkdir(parents=True, exist_ok=True)
        session_path.mkdir(parents=True, exist_ok=True)

    # Dispatch to appropriate command
    if args.command == "auth":
        return run_auth(args)
    elif args.command == "backup":
        return run_backup(args)
    elif args.command == "schedule":
        return run_schedule(args)
    elif args.command == "export":
        return asyncio.run(run_export(args))
    elif args.command == "stats":
        return asyncio.run(run_stats(args))
    elif args.command == "list-chats":
        return asyncio.run(run_list_chats(args))
    elif args.command == "import":
        return asyncio.run(run_import(args))
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
