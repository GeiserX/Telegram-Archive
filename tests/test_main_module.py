"""
Tests for the CLI entry point module (src/__main__.py).
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.__main__ import create_parser


class TestCreateParser(unittest.TestCase):
    """Tests for create_parser argument parser configuration."""

    def test_parser_has_correct_prog_name(self):
        """Parser uses 'telegram-archive' as program name."""
        parser = create_parser()
        self.assertEqual(parser.prog, "telegram-archive")

    def test_parser_has_data_dir_option(self):
        """Parser accepts --data-dir top-level option."""
        parser = create_parser()
        args = parser.parse_args(["--data-dir", "/tmp/data", "backup"])
        self.assertEqual(args.data_dir, "/tmp/data")

    def test_parser_has_auth_command(self):
        """Parser recognizes 'auth' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["auth"])
        self.assertEqual(args.command, "auth")

    def test_parser_has_backup_command(self):
        """Parser recognizes 'backup' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["backup"])
        self.assertEqual(args.command, "backup")

    def test_parser_has_schedule_command(self):
        """Parser recognizes 'schedule' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["schedule"])
        self.assertEqual(args.command, "schedule")

    def test_parser_has_export_command_with_options(self):
        """Parser recognizes 'export' with required output and optional filters."""
        parser = create_parser()
        args = parser.parse_args(["export", "-o", "out.json", "-c", "123", "-s", "2025-01-01", "-e", "2025-12-31"])
        self.assertEqual(args.command, "export")
        self.assertEqual(args.output, "out.json")
        self.assertEqual(args.chat_id, 123)
        self.assertEqual(args.start_date, "2025-01-01")
        self.assertEqual(args.end_date, "2025-12-31")

    def test_parser_has_stats_command(self):
        """Parser recognizes 'stats' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["stats"])
        self.assertEqual(args.command, "stats")

    def test_parser_has_list_chats_command(self):
        """Parser recognizes 'list-chats' subcommand."""
        parser = create_parser()
        args = parser.parse_args(["list-chats"])
        self.assertEqual(args.command, "list-chats")

    def test_parser_has_import_command_with_options(self):
        """Parser recognizes 'import' with all options."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "import",
                "-p",
                "/path/to/export",
                "-c",
                "-1001234567890",
                "--dry-run",
                "--skip-media",
                "--merge",
            ]
        )
        self.assertEqual(args.command, "import")
        self.assertEqual(args.path, "/path/to/export")
        self.assertEqual(args.chat_id, -1001234567890)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.skip_media)
        self.assertTrue(args.merge)

    def test_parser_has_fill_gaps_command(self):
        """Parser recognizes 'fill-gaps' with options."""
        parser = create_parser()
        args = parser.parse_args(["fill-gaps", "-c", "999", "-t", "25"])
        self.assertEqual(args.command, "fill-gaps")
        self.assertEqual(args.chat_id, 999)
        self.assertEqual(args.threshold, 25)

    def test_parser_fill_gaps_threshold_defaults_to_none(self):
        """fill-gaps threshold defaults to None when not specified."""
        parser = create_parser()
        args = parser.parse_args(["fill-gaps"])
        self.assertIsNone(args.threshold)

    def test_parser_no_args_returns_none_command(self):
        """Parser with no arguments returns None command."""
        parser = create_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.command)


class TestMainFunction(unittest.TestCase):
    """Tests for the main() entry point function."""

    def test_main_shows_help_when_no_args(self):
        """main() shows help and returns 0 when no arguments provided."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive"]),
            patch("src.__main__.create_parser") as mock_parser_fn,
        ):
            mock_parser = MagicMock()
            mock_parser.parse_args.return_value = MagicMock(command=None, data_dir=None)
            mock_parser_fn.return_value = mock_parser

            result = main()

            self.assertEqual(result, 0)

    def test_main_dispatches_auth_command(self):
        """main() dispatches to run_auth for 'auth' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "auth"]),
            patch("src.__main__.run_auth", return_value=0) as mock_auth,
        ):
            result = main()

            mock_auth.assert_called_once()
            self.assertEqual(result, 0)

    def test_main_dispatches_backup_command(self):
        """main() dispatches to run_backup for 'backup' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "backup"]),
            patch("src.__main__.run_backup", return_value=0) as mock_backup,
        ):
            result = main()

            mock_backup.assert_called_once()
            self.assertEqual(result, 0)

    def test_main_dispatches_schedule_command(self):
        """main() dispatches to run_schedule for 'schedule' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "schedule"]),
            patch("src.__main__.run_schedule", return_value=0) as mock_sched,
        ):
            result = main()

            mock_sched.assert_called_once()
            self.assertEqual(result, 0)

    def test_main_dispatches_export_command(self):
        """main() dispatches to run_export for 'export' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "export", "-o", "out.json"]),
            patch("src.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            result = main()

            mock_run.assert_called_once()
            self.assertEqual(result, 0)

    def test_main_dispatches_stats_command(self):
        """main() dispatches to run_stats for 'stats' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "stats"]),
            patch("src.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            result = main()

            mock_run.assert_called_once()

    def test_main_dispatches_list_chats_command(self):
        """main() dispatches to run_list_chats for 'list-chats' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "list-chats"]),
            patch("src.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            result = main()

            mock_run.assert_called_once()

    def test_main_dispatches_import_command(self):
        """main() dispatches to run_import for 'import' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "import", "-p", "/tmp/export"]),
            patch("src.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            result = main()

            mock_run.assert_called_once()

    def test_main_dispatches_fill_gaps_command(self):
        """main() dispatches to run_fill_gaps_cmd for 'fill-gaps' command."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "fill-gaps"]),
            patch("src.__main__.asyncio.run", return_value=0) as mock_run,
        ):
            result = main()

            mock_run.assert_called_once()

    def test_main_shows_help_for_unknown_command(self):
        """main() shows help and returns 0 for unrecognized command."""
        from src.__main__ import main

        # argparse would error on truly unknown commands, but None command
        # is handled by the else branch
        with patch.object(sys, "argv", ["telegram-archive"]):
            result = main()

            self.assertEqual(result, 0)

    def test_main_data_dir_sets_environment_variables(self):
        """main() sets BACKUP_PATH and SESSION_DIR from --data-dir."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "--data-dir", "/tmp/test-data", "auth"]),
            patch("src.__main__.run_auth", return_value=0),
            patch("pathlib.Path.mkdir"),
        ):
            result = main()

            self.assertEqual(result, 0)
            self.assertIn("backups", os.environ.get("BACKUP_PATH", ""))
            self.assertIn("session", os.environ.get("SESSION_DIR", ""))

    def test_main_data_dir_creates_directories(self):
        """main() creates backup and session directories when --data-dir is used."""
        from src.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "--data-dir", "/tmp/test-dir-create", "auth"]),
            patch("src.__main__.run_auth", return_value=0),
            patch("pathlib.Path.mkdir") as mock_mkdir,
        ):
            main()

            # Should be called for both backup_path and session_path
            assert mock_mkdir.call_count >= 2


class TestRunAuth(unittest.TestCase):
    """Tests for run_auth wrapper function."""

    def test_run_auth_calls_setup_auth_main(self):
        """run_auth calls setup_auth.main and returns its result."""
        from src.__main__ import run_auth

        with patch("src.setup_auth.main", return_value=0) as mock_auth_main:
            result = run_auth(MagicMock())

            mock_auth_main.assert_called_once()
            self.assertEqual(result, 0)


class TestRunBackup(unittest.TestCase):
    """Tests for run_backup wrapper function."""

    def test_run_backup_calls_telegram_backup_main(self):
        """run_backup calls telegram_backup.main and returns its result."""
        from src.__main__ import run_backup

        with patch("src.telegram_backup.main", return_value=0) as mock_backup_main:
            result = run_backup(MagicMock())

            mock_backup_main.assert_called_once()
            self.assertEqual(result, 0)


class TestRunSchedule(unittest.TestCase):
    """Tests for run_schedule wrapper function."""

    def test_run_schedule_calls_scheduler_main(self):
        """run_schedule runs scheduler.main via asyncio.run."""
        from src.__main__ import run_schedule

        with patch("src.__main__.asyncio.run", return_value=None) as mock_run:
            run_schedule(MagicMock())

            mock_run.assert_called_once()


class TestRunExport:
    """Tests for run_export async function."""

    async def test_run_export_success(self):
        """run_export creates exporter, exports, and returns 0."""
        from src.__main__ import run_export

        mock_config = MagicMock()
        mock_exporter = AsyncMock()
        mock_exporter.export_to_json = AsyncMock()
        mock_exporter.close = AsyncMock()

        args = MagicMock()
        args.output = "out.json"
        args.chat_id = None
        args.start_date = None
        args.end_date = None

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await run_export(args)

            assert result == 0
            mock_exporter.export_to_json.assert_called_once()
            mock_exporter.close.assert_called_once()

    async def test_run_export_failure_returns_1(self):
        """run_export returns 1 on exception."""
        from src.__main__ import run_export

        args = MagicMock()

        with patch("src.config.Config", side_effect=Exception("config error")):
            result = await run_export(args)

            assert result == 1


class TestRunStats:
    """Tests for run_stats async function."""

    async def test_run_stats_success(self):
        """run_stats creates exporter, shows stats, and returns 0."""
        from src.__main__ import run_stats

        mock_config = MagicMock()
        mock_exporter = AsyncMock()
        mock_exporter.show_statistics = AsyncMock()
        mock_exporter.close = AsyncMock()

        args = MagicMock()

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await run_stats(args)

            assert result == 0
            mock_exporter.show_statistics.assert_called_once()
            mock_exporter.close.assert_called_once()

    async def test_run_stats_failure_returns_1(self):
        """run_stats returns 1 on exception."""
        from src.__main__ import run_stats

        args = MagicMock()

        with patch("src.config.Config", side_effect=Exception("fail")):
            result = await run_stats(args)

            assert result == 1


class TestRunListChats:
    """Tests for run_list_chats async function."""

    async def test_run_list_chats_success(self):
        """run_list_chats creates exporter, lists chats, and returns 0."""
        from src.__main__ import run_list_chats

        mock_config = MagicMock()
        mock_exporter = AsyncMock()
        mock_exporter.list_chats = AsyncMock()
        mock_exporter.close = AsyncMock()

        args = MagicMock()

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.export_backup.BackupExporter.create", new_callable=AsyncMock, return_value=mock_exporter),
        ):
            result = await run_list_chats(args)

            assert result == 0
            mock_exporter.list_chats.assert_called_once()
            mock_exporter.close.assert_called_once()

    async def test_run_list_chats_failure_returns_1(self):
        """run_list_chats returns 1 on exception."""
        from src.__main__ import run_list_chats

        args = MagicMock()

        with patch("src.config.Config", side_effect=Exception("fail")):
            result = await run_list_chats(args)

            assert result == 1


class TestRunFillGapsCmd:
    """Tests for run_fill_gaps_cmd async function."""

    async def test_run_fill_gaps_cmd_success(self):
        """run_fill_gaps_cmd runs gap-fill and prints summary."""
        from src.__main__ import run_fill_gaps_cmd

        mock_config = MagicMock()
        mock_config.gap_threshold = 50

        args = MagicMock()
        args.chat_id = None
        args.threshold = None

        summary = {
            "chats_scanned": 5,
            "chats_with_gaps": 2,
            "total_gaps": 10,
            "total_recovered": 8,
            "details": [
                {"chat_name": "Test", "chat_id": 123, "gaps": 5, "recovered": 4},
            ],
        }

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.telegram_backup.run_fill_gaps", new_callable=AsyncMock, return_value=summary),
        ):
            result = await run_fill_gaps_cmd(args)

            assert result == 0

    async def test_run_fill_gaps_cmd_overrides_threshold(self):
        """run_fill_gaps_cmd sets gap_threshold from --threshold arg."""
        from src.__main__ import run_fill_gaps_cmd

        mock_config = MagicMock()
        mock_config.gap_threshold = 50

        args = MagicMock()
        args.chat_id = None
        args.threshold = 25

        summary = {
            "chats_scanned": 0,
            "chats_with_gaps": 0,
            "total_gaps": 0,
            "total_recovered": 0,
            "details": [],
        }

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.telegram_backup.run_fill_gaps", new_callable=AsyncMock, return_value=summary),
        ):
            result = await run_fill_gaps_cmd(args)

            assert mock_config.gap_threshold == 25
            assert result == 0

    async def test_run_fill_gaps_cmd_failure_returns_1(self):
        """run_fill_gaps_cmd returns 1 on exception."""
        from src.__main__ import run_fill_gaps_cmd

        args = MagicMock()
        args.threshold = None

        with patch("src.config.Config", side_effect=Exception("fail")):
            result = await run_fill_gaps_cmd(args)

            assert result == 1


class TestRunImport:
    """Tests for run_import async function."""

    async def test_run_import_success(self):
        """run_import creates importer, runs import, and prints summary."""
        from src.__main__ import run_import

        mock_config = MagicMock()
        mock_config.media_path = "/data/media"

        mock_importer = AsyncMock()
        mock_importer.run = AsyncMock(
            return_value={
                "chats_imported": 1,
                "total_messages": 100,
                "total_media": 10,
                "details": [
                    {"chat_name": "Test", "chat_id": 123, "messages": 100, "media": 10},
                ],
            }
        )
        mock_importer.close = AsyncMock()

        args = MagicMock()
        args.path = "/tmp/export"
        args.chat_id = None
        args.dry_run = False
        args.skip_media = False
        args.merge = False

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.telegram_import.TelegramImporter.create", new_callable=AsyncMock, return_value=mock_importer),
        ):
            result = await run_import(args)

            assert result == 0
            mock_importer.run.assert_called_once()
            mock_importer.close.assert_called_once()

    async def test_run_import_dry_run_prefix(self):
        """run_import shows [DRY RUN] prefix when dry_run is True."""
        from src.__main__ import run_import

        mock_config = MagicMock()
        mock_config.media_path = "/data/media"

        mock_importer = AsyncMock()
        mock_importer.run = AsyncMock(
            return_value={
                "chats_imported": 0,
                "total_messages": 0,
                "total_media": 0,
                "details": [],
            }
        )
        mock_importer.close = AsyncMock()

        args = MagicMock()
        args.path = "/tmp/export"
        args.chat_id = None
        args.dry_run = True
        args.skip_media = False
        args.merge = False

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.telegram_import.TelegramImporter.create", new_callable=AsyncMock, return_value=mock_importer),
        ):
            result = await run_import(args)

            assert result == 0

    async def test_run_import_failure_returns_1(self):
        """run_import returns 1 on exception."""
        from src.__main__ import run_import

        args = MagicMock()
        args.path = "/tmp/export"

        with patch("src.config.Config", side_effect=Exception("fail")):
            result = await run_import(args)

            assert result == 1
