"""
Unified CLI entry point for Telegram Archive.

Provides a single interface for all backup operations including authentication,
backup execution, scheduling, and data export.
"""

import argparse
import asyncio
import json
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

  2. Installed with pip? Create the database schema, and again after upgrades
     (the Docker image does this itself on start):
     telegram-archive migrate

  3. Run backup:
     telegram-archive backup       # One-time manual backup
     telegram-archive schedule     # Continuous scheduled backups (recommended)

  4. View and export data:
     telegram-archive list-chats   # List all backed up chats
     telegram-archive stats        # Show backup statistics
     telegram-archive status       # Is the archive healthy? (exit code 1 if not)
     telegram-archive check-media  # Any media file missing? (--repair fixes them)
     telegram-archive export -o file.json  # Export to JSON

  5. Import Telegram Desktop exports:
     telegram-archive import -p /path/to/export              # JSON (full account export)
     telegram-archive import -p /path/to/export -c -1001234567890 --merge
     telegram-archive import -p /path/to/chat_folder -c 123  # HTML (per-chat export)

LOCAL DEVELOPMENT:

  Use --data-dir to specify an alternative data location (default: /data):
    telegram-archive --data-dir ./data list-chats
    telegram-archive --data-dir ~/telegram-data backup

  Or use the Python module directly:
    python -m telegram_archive --data-dir ./data list-chats

DOCKER USAGE:

  Authentication (first time only):
    docker run -it --rm --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:<version> \\
      python -m telegram_archive auth

  Start scheduled backups:
    docker run -d --env-file .env \\
      -v ./data:/data \\
      drumsergio/telegram-archive:<version> \\
      python -m telegram_archive schedule

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

    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show whether the archive is healthy (exit code 1 if not)",
        description=(
            "Report the last backup run, listener state, media pipeline counts and "
            "database size, the same answer as the viewer's status panel, without "
            "the viewer. Exits 1 when no backup has run, the last backup did not "
            "finish, SCHEDULE has missed a run, or the database cannot be read."
        ),
    )
    status_parser.add_argument("--json", action="store_true", help="Print the status as JSON")

    check_media_parser = subparsers.add_parser(
        "check-media",
        help="Find media files that are missing or behind a broken link, and repair them with --repair",
        description=(
            "Check every downloaded media row of every account: is its file where the row "
            "says? A missing file is looked for on disk (under its name in _shared, in the "
            "chat's other id-form folder, or at another row with the same content hash). "
            "With --repair a copy found on disk is put back, never replacing anything, and "
            "a file with no copy is marked not downloaded so the next backup fetches it "
            "from Telegram again. Without --repair nothing is changed. Exit code 1 when "
            "the dry run finds a file missing."
        ),
    )
    check_media_parser.add_argument(
        "--repair", action="store_true", help="Restore files from copies on disk and mark the rest to download again"
    )
    check_media_parser.add_argument("-c", "--chat-id", type=int, help="Only this chat (default: every chat)")

    # List chats command
    list_parser = subparsers.add_parser(
        "list-chats", help="List all backed up chats", description="Show a table of all chats in the backup database."
    )

    # Import command
    import_parser = subparsers.add_parser(
        "import",
        help="Import Telegram Desktop chat export",
        description=(
            "Import a Telegram Desktop chat export into the database. "
            "Supports both JSON format (result.json from Settings > Advanced > Export Telegram data) "
            "and HTML format (messages.html from per-chat export). "
            "For HTML exports, --chat-id is required."
        ),
    )
    import_parser.add_argument(
        "-p", "--path", required=True, help="Path to export folder (containing result.json or messages.html)"
    )
    import_parser.add_argument(
        "-c",
        "--chat-id",
        type=int,
        help="Chat ID (marked format, e.g. -1001234567890). Required for HTML exports.",
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

    # Fill gaps command
    fill_gaps_parser = subparsers.add_parser(
        "fill-gaps",
        help="Detect and fill message gaps from failed backups",
        description=(
            "Scans backed-up chats for gaps in message ID sequences "
            "and recovers skipped messages from Telegram. "
            "Gaps are caused by API errors, rate limits, or interruptions "
            "during previous backup runs."
        ),
    )
    fill_gaps_parser.add_argument("-c", "--chat-id", type=int, help="Fill gaps only for this specific chat ID")
    fill_gaps_parser.add_argument(
        "-t",
        "--threshold",
        type=int,
        default=None,
        help="Minimum gap size to investigate (overrides GAP_THRESHOLD env var)",
    )

    backfill_parser = subparsers.add_parser(
        "backfill-topics",
        help="Re-sweep one chat so imported forum messages regain their topic",
        description=(
            "Telegram Desktop HTML exports carry no forum-topic metadata, so "
            "imported forum messages land in the General topic. This resets the "
            "chat's sync cursor and runs a text-only resweep (media downloads, "
            "deletion/edit sync and media verification all disabled) scoped to "
            "that chat — the upsert refreshes reply_to_top_id in place."
        ),
    )
    backfill_parser.add_argument("-c", "--chat-id", type=int, required=True, help="Chat ID to backfill")

    subparsers.add_parser(
        "migrate",
        help="Create or upgrade the database schema (alembic upgrade head)",
        description=(
            "Apply every database migration the archive has not seen yet. "
            "Run it once before the first backup and again after each upgrade "
            "when installed with pip. The Docker image does this itself on start."
        ),
    )

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge another archive's accounts into this archive",
        description=(
            "Copy every account of another archive (the source) into this one "
            "(the target) under new account ids, with their chats, messages, "
            "media rows, versions, reactions, topics, folders, sync cursors, "
            "avatar history and transcripts, then copy their media files. The "
            "source is only read. Nothing already in the target is changed or "
            "deleted. Viewer accounts, sessions, share tokens and push "
            "subscriptions are not merged. Stop both installs first."
        ),
    )
    merge_parser.add_argument("--source", required=True, help="The other archive: a SQLite file path or a database URL")
    merge_parser.add_argument(
        "--source-media",
        metavar="DIR",
        help="The other archive's media folder (default: 'media' beside a SQLite source file)",
    )
    merge_parser.add_argument(
        "--account",
        metavar="LABEL_OR_ID",
        help="Merge only this source account, by label or account id (default: every account)",
    )
    merge_parser.add_argument(
        "--add-missing-parents",
        action="store_true",
        help=(
            "Add an empty placeholder chat, message, folder or user for each source row whose parent row "
            "the source lacks (needed to merge such a SQLite source into PostgreSQL)"
        ),
    )
    merge_parser.add_argument(
        "--dry-run", action="store_true", help="Print the row counts and media size without writing"
    )

    # Reclassify round videos
    round_parser = subparsers.add_parser(
        "reclassify-round-videos",
        help="Re-type archived round videos ('video notes') captured before 8.5.0",
        description=(
            "Before 8.5.0 neither capture lane looked at the flag that marks a "
            "circular video message, so round videos were archived as ordinary "
            "videos and the viewer showed them as rectangles. Roundness is an "
            "MTProto attribute that is not in the stored file, so this asks "
            "Telegram which messages are round (a server-side filtered search, "
            "so a chat with none costs one request) and corrects those rows in "
            "place. Nothing is downloaded, re-keyed or deleted. Run it with the "
            "viewer idle: an open tab holds the old media URLs and will show "
            "'Media not found' for a re-typed video until it is reloaded."
        ),
    )
    round_parser.add_argument("-c", "--chat-id", type=int, help="Only this chat (default: every chat with videos)")
    round_parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")

    return parser


async def run_export(args) -> int:
    """Run export command."""
    from .config import Config, setup_logging
    from .export_backup import BackupExporter

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()

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
        config.log_summary()

        exporter = await BackupExporter.create(config)
        try:
            await exporter.show_statistics()
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"Stats failed: {e}", file=sys.stderr)
        return 1


async def run_status(args) -> int:
    """Run status command: 0 when the archive is healthy, 1 when it is not."""
    from .config import Config, setup_logging
    from .db import DatabaseAdapter, close_database, init_database
    from .status import collect_status, format_status, health_problems

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()

        try:
            manager = await init_database()
            status = await collect_status(DatabaseAdapter(manager), config)
        finally:
            await close_database()
        problems = health_problems(status, config.schedule)
    except Exception as e:
        print(f"Status failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({**status, "healthy": not problems, "problems": problems}, indent=2))
    else:
        print(format_status(status, problems))
    return 1 if problems else 0


async def run_check_media(args) -> int:
    """Run check-media: 1 when a dry run finds a file missing or a repair fails."""
    from .config import Config, setup_logging
    from .db import DatabaseAdapter, close_database, init_database
    from .media_integrity import check_media, format_media_check

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()
        try:
            manager = await init_database()
            report = await check_media(
                DatabaseAdapter(manager), config.media_path, repair=args.repair, chat_id=args.chat_id
            )
        finally:
            await close_database()
    except Exception as e:
        # The type only: a driver or filesystem error can quote a path.
        print(f"Media check failed: {type(e).__name__}", file=sys.stderr)
        return 1
    for line in format_media_check(report, repair=args.repair):
        print(line)
    if args.repair:
        return 1 if report["restore_failed"] else 0
    return 1 if report["broken_links"] or report["missing_files"] else 0


async def run_list_chats(args) -> int:
    """Run list-chats command."""
    from .config import Config, setup_logging
    from .export_backup import BackupExporter

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()

        exporter = await BackupExporter.create(config)
        try:
            await exporter.list_chats()
        finally:
            await exporter.close()
        return 0
    except Exception as e:
        print(f"List chats failed: {e}", file=sys.stderr)
        return 1


async def run_fill_gaps_cmd(args) -> int:
    """Run fill-gaps command."""
    from .config import Config, setup_logging
    from .telegram_backup import run_fill_gaps

    try:
        config = Config()
        if args.threshold is not None:
            config.gap_threshold = args.threshold
        setup_logging(config)
        config.log_summary()

        summary = await run_fill_gaps(config, chat_id=args.chat_id)
        print("\nGap-fill complete:")
        print(f"  Chats scanned: {summary['chats_scanned']}")
        print(f"  Chats with gaps: {summary['chats_with_gaps']}")
        print(f"  Total gaps found: {summary['total_gaps']}")
        print(f"  Messages recovered: {summary['total_recovered']}")
        if summary.get("chats_with_leading_holes"):
            print(
                f"  Chats with history missing before their earliest archived message: "
                f"{summary['chats_with_leading_holes']} (reported only - "
                "fill it deliberately with a targeted import or backup if wanted)"
            )
        if summary["details"]:
            for detail in summary["details"]:
                line = (
                    f"  - {detail['chat_name']} (ID {detail['chat_id']}): "
                    f"{detail['gaps']} gaps, {detail['recovered']} recovered"
                )
                if detail.get("leading_missing"):
                    line += f", ~{detail['leading_missing']} ids missing before id {detail['leading_hole_before_id']}"
                print(line)
        return 0
    except Exception as e:
        print(f"Gap-fill failed: {e}", file=sys.stderr)
        return 1


async def run_import(args) -> int:
    """Run import command."""
    from .config import Config, setup_logging
    from .telegram_import import TelegramImporter

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()

        importer = await TelegramImporter.create(config.media_path, config.max_filename_bytes)
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


def run_migrate(args) -> int:
    """Upgrade the database schema to the newest migration."""
    from .db.migrations import upgrade_to_head

    try:
        upgrade_to_head()
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1
    print("Database schema is up to date.")
    return 0


def run_merge(args) -> int:
    """Merge another archive into the configured one."""
    from .config import Config
    from .merge import MergeError, format_report, merge_archives, target_database_url

    try:
        report = merge_archives(
            source=args.source,
            target_url=target_database_url(),
            target_media=Config().media_path,
            source_media=args.source_media,
            dry_run=args.dry_run,
            account=args.account,
            add_missing_parents=args.add_missing_parents,
        )
    except MergeError as e:
        print(f"Merge refused: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # The type only: a driver error can quote a row or a path.
        print(f"Merge failed: {type(e).__name__}. Nothing was committed to the target database.", file=sys.stderr)
        return 1
    for line in format_report(report):
        print(line)
    return 0


def run_reclassify_round_videos(args) -> int:
    """Ask Telegram which archived videos are round, and re-type those rows."""
    from .config import Config, setup_logging
    from .telegram_backup import run_reclassify_round_videos as reclassify

    try:
        config = Config()
        setup_logging(config)
        config.log_summary()
        summary = asyncio.run(reclassify(config, chat_id=args.chat_id, dry_run=args.dry_run))
    except Exception as e:
        print(f"Reclassification failed: {e}", file=sys.stderr)
        return 1

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{prefix}Round-video reclassification complete:")
    print(f"  Chats scanned:      {summary['chats_scanned']}")
    print(f"  Round videos found: {summary['round_videos_found']}")
    print(f"  Rows re-typed:      {summary['rows_retyped']}")
    if summary["errors"]:
        print(f"  Chats with errors:  {summary['errors']}")
    return 0


def run_backfill_topics(args) -> int:
    """Reset one chat's cursor and resweep it text-only (topic backfill)."""
    # The documented recovery procedure for imported forum chats, minus its
    # footguns: DOWNLOAD_MEDIA off (nothing to fetch, the files are local),
    # SYNC_DELETIONS_EDITS off (a full-history pass must never mass-delete),
    # VERIFY_MEDIA off, scope pinned to the one chat.
    os.environ["DOWNLOAD_MEDIA"] = "false"
    os.environ["SYNC_DELETIONS_EDITS"] = "false"
    os.environ["VERIFY_MEDIA"] = "false"
    os.environ["CHAT_IDS"] = str(args.chat_id)

    from .db import create_adapter

    async def _reset_cursor() -> int:
        db = await create_adapter()
        try:
            return await db.reset_chat_sync_cursor(args.chat_id)
        finally:
            await db.close()

    try:
        known_chat_rows = asyncio.run(_reset_cursor())
    except Exception as e:
        print(f"Topic backfill failed: {e}", file=sys.stderr)
        return 1

    if known_chat_rows == 0:
        print("That chat is not in the archive yet — import or back it up first.")
        return 1

    from .telegram_backup import main as backup_main

    return backup_main()


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
    elif args.command == "migrate":
        return run_migrate(args)
    elif args.command == "merge":
        return run_merge(args)
    elif args.command == "reclassify-round-videos":
        return run_reclassify_round_videos(args)
    elif args.command == "backfill-topics":
        return run_backfill_topics(args)
    elif args.command == "schedule":
        return run_schedule(args)
    elif args.command == "export":
        return asyncio.run(run_export(args))
    elif args.command == "stats":
        return asyncio.run(run_stats(args))
    elif args.command == "status":
        return asyncio.run(run_status(args))
    elif args.command == "check-media":
        return asyncio.run(run_check_media(args))
    elif args.command == "list-chats":
        return asyncio.run(run_list_chats(args))
    elif args.command == "import":
        return asyncio.run(run_import(args))
    elif args.command == "fill-gaps":
        return asyncio.run(run_fill_gaps_cmd(args))
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
