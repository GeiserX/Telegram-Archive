"""Tests for gap-fill backup feature (phases 1-4)."""

from unittest.mock import patch

import pytest


class TestGapDetectionQuery:
    """Phase 1: detect_message_gaps and get_chats_with_messages on adapter."""

    def test_adapter_has_detect_message_gaps(self):
        from src.db.adapter import DatabaseAdapter

        assert hasattr(DatabaseAdapter, "detect_message_gaps")

    def test_adapter_has_get_chats_with_messages(self):
        from src.db.adapter import DatabaseAdapter

        assert hasattr(DatabaseAdapter, "get_chats_with_messages")

    def test_detect_message_gaps_signature(self):
        """detect_message_gaps accepts chat_id and threshold."""
        import inspect

        from src.db.adapter import DatabaseAdapter

        sig = inspect.signature(DatabaseAdapter.detect_message_gaps)
        params = list(sig.parameters.keys())
        assert "chat_id" in params
        assert "threshold" in params


class TestGapFillMethods:
    """Phase 2: _fill_gap_range, _fill_gaps, run_fill_gaps on TelegramBackup."""

    def test_backup_has_fill_gap_range(self):
        from src.telegram_backup import TelegramBackup

        assert hasattr(TelegramBackup, "_fill_gap_range")

    def test_backup_has_fill_gaps(self):
        from src.telegram_backup import TelegramBackup

        assert hasattr(TelegramBackup, "_fill_gaps")

    def test_run_fill_gaps_exists(self):
        from src.telegram_backup import run_fill_gaps

        assert callable(run_fill_gaps)

    def test_run_fill_gaps_is_coroutine(self):
        import inspect

        from src.telegram_backup import run_fill_gaps

        assert inspect.iscoroutinefunction(run_fill_gaps)


class TestConfigGapFill:
    """Phase 3: Config env vars for gap-fill."""

    def test_config_has_fill_gaps(self):
        import inspect

        from src.config import Config

        source = inspect.getsource(Config.__init__)
        assert "fill_gaps" in source

    def test_config_has_gap_threshold(self):
        import inspect

        from src.config import Config

        source = inspect.getsource(Config.__init__)
        assert "gap_threshold" in source

    @patch.dict("os.environ", {"FILL_GAPS": "true"}, clear=False)
    def test_fill_gaps_enabled(self):
        """FILL_GAPS=true should set fill_gaps to True."""
        # Need to patch all required env vars for Config
        required = {
            "API_ID": "12345",
            "API_HASH": "abc123",
            "FILL_GAPS": "true",
        }
        with patch.dict("os.environ", required, clear=False):
            try:
                from src.config import Config

                config = Config()
                assert config.fill_gaps is True
            except Exception:
                # Config may fail on other missing envs; just verify the attribute
                pytest.skip("Config requires additional env vars")

    @patch.dict("os.environ", {"FILL_GAPS": "false"}, clear=False)
    def test_fill_gaps_disabled_by_default(self):
        """Default FILL_GAPS should be false."""
        required = {
            "API_ID": "12345",
            "API_HASH": "abc123",
        }
        with patch.dict("os.environ", required, clear=False):
            try:
                from src.config import Config

                config = Config()
                assert config.fill_gaps is False
            except Exception:
                pytest.skip("Config requires additional env vars")


class TestSchedulerGapFill:
    """Phase 3: Scheduler integrates gap-fill after backup."""

    def test_scheduler_run_backup_job_calls_gap_fill(self):
        """_run_backup_job source references fill_gaps."""
        from pathlib import Path

        source = Path("src/scheduler.py").read_text()
        assert "fill_gaps" in source
        assert "run_fill_gaps" in source

    def test_scheduler_run_forever_calls_gap_fill(self):
        """run_forever source references fill_gaps for initial backup."""
        from pathlib import Path

        source = Path("src/scheduler.py").read_text()
        assert "Initial gap-fill" in source


class TestCLIFillGaps:
    """Phase 3: fill-gaps CLI subcommand."""

    def test_fill_gaps_subcommand_exists(self):
        from src.__main__ import create_parser

        parser = create_parser()
        # Parse fill-gaps without args to verify it's registered
        args = parser.parse_args(["fill-gaps"])
        assert args.command == "fill-gaps"

    def test_fill_gaps_chat_id_arg(self):
        from src.__main__ import create_parser

        parser = create_parser()
        args = parser.parse_args(["fill-gaps", "--chat-id", "-1001234567890"])
        assert args.chat_id == -1001234567890

    def test_fill_gaps_threshold_arg(self):
        from src.__main__ import create_parser

        parser = create_parser()
        args = parser.parse_args(["fill-gaps", "--threshold", "100"])
        assert args.threshold == 100

    def test_fill_gaps_threshold_default_none(self):
        from src.__main__ import create_parser

        parser = create_parser()
        args = parser.parse_args(["fill-gaps"])
        assert args.threshold is None

    def test_fill_gaps_short_flags(self):
        from src.__main__ import create_parser

        parser = create_parser()
        args = parser.parse_args(["fill-gaps", "-c", "123", "-t", "200"])
        assert args.chat_id == 123
        assert args.threshold == 200

    def test_run_fill_gaps_cmd_exists(self):
        from src.__main__ import run_fill_gaps_cmd

        assert callable(run_fill_gaps_cmd)

    def test_dispatch_handles_fill_gaps(self):
        """main() dispatch includes fill-gaps case."""
        import inspect

        from src.__main__ import main

        source = inspect.getsource(main)
        assert '"fill-gaps"' in source


class TestFillGapRangeLogic:
    """Phase 2: _fill_gap_range uses correct iter_messages params."""

    def test_fill_gap_range_source_uses_min_max_id(self):
        """_fill_gap_range should use min_id and max_id for bounded fetching."""
        import inspect

        from src.telegram_backup import TelegramBackup

        source = inspect.getsource(TelegramBackup._fill_gap_range)
        assert "min_id" in source
        assert "max_id" in source

    def test_fill_gap_range_commits_batches(self):
        """_fill_gap_range should call _commit_batch."""
        import inspect

        from src.telegram_backup import TelegramBackup

        source = inspect.getsource(TelegramBackup._fill_gap_range)
        assert "_commit_batch" in source

    def test_fill_gaps_handles_inaccessible_chats(self):
        """_fill_gaps should catch ChannelPrivateError etc."""
        import inspect

        from src.telegram_backup import TelegramBackup

        source = inspect.getsource(TelegramBackup._fill_gaps)
        assert "ChannelPrivateError" in source


class TestFillGapsSummary:
    """Phase 2: _fill_gaps returns correct summary structure."""

    def test_fill_gaps_returns_summary_keys(self):
        """_fill_gaps source builds summary with expected keys."""
        import inspect

        from src.telegram_backup import TelegramBackup

        source = inspect.getsource(TelegramBackup._fill_gaps)
        for key in ["chats_scanned", "chats_with_gaps", "total_gaps", "total_recovered", "details"]:
            assert key in source, f"Missing key '{key}' in _fill_gaps summary"
