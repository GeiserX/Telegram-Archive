"""telegram-archive status: the payload, the health verdict and the command.

``collect_status`` is the body of the viewer's ``GET /api/status`` (the route
equivalence test lives with the other route tests in test_web_coverage.py).
``health_problems`` turns that payload into an exit code. The real-engine tests
at the bottom run the command against SQLite and, when TEST_POSTGRES_URL points
at a server (CI), PostgreSQL.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger

from telegram_archive.__main__ import create_parser, run_status
from telegram_archive.status import (
    collect_status,
    format_status,
    health_problems,
    parse_utc,
    previous_fire_times,
)

NOW = datetime(2026, 1, 15, 7, 30, tzinfo=UTC)
EVERY_SIX_HOURS = "0 */6 * * *"


def _mock_db(metadata: dict, *, account_ids=None, is_sqlite: bool = True) -> MagicMock:
    db = MagicMock()
    db.get_metadata = AsyncMock(side_effect=lambda key: metadata.get(key))
    if isinstance(account_ids, Exception):
        db.get_account_ids = AsyncMock(side_effect=account_ids)
    else:
        db.get_account_ids = AsyncMock(return_value=account_ids or [])
    db.get_operator_status_counts = AsyncMock(
        return_value={"downloaded": 10, "pending": 2, "exhausted": 1, "skipped": 0}
    )
    db.get_database_size_bytes = AsyncMock(return_value=4096)
    db.db_manager = MagicMock(_is_sqlite=is_sqlite)
    return db


def _mock_config() -> MagicMock:
    config = MagicMock()
    config.should_skip_topic = MagicMock(return_value=False)
    config.download_youtube_videos = False
    config.max_media_download_attempts = 5
    config.schedule = EVERY_SIX_HOURS
    return config


def _status(last_run=None, *, in_progress=False, stats_at=None) -> dict:
    return {
        "backup": {"last_run": last_run, "in_progress": in_progress},
        "stats_calculated_at": stats_at,
        "listeners": [],
        "media": {"downloaded": 0, "pending": 0, "exhausted": 0, "skipped": 0},
        "database": {"backend": "sqlite", "size_bytes": 0},
    }


class TestCollectStatus(unittest.IsolatedAsyncioTestCase):
    async def test_payload_shape_and_values(self):
        db = _mock_db(
            {
                "last_backup_time": "2026-01-15T06:00:00Z",
                "backup_in_progress": "0",
                "stats_calculated_at": "2026-01-15T06:20:00",
                "listener_active_since_account_2": "2026-01-15T05:00:00",
            },
            account_ids=[1, 2],
        )

        payload = await collect_status(db, _mock_config())

        self.assertEqual(
            payload,
            {
                "backup": {"last_run": "2026-01-15T06:00:00Z", "in_progress": False},
                "stats_calculated_at": "2026-01-15T06:20:00",
                "listeners": [
                    {"account_id": 1, "active": False, "active_since": None},
                    {"account_id": 2, "active": True, "active_since": "2026-01-15T05:00:00"},
                ],
                "media": {"downloaded": 10, "pending": 2, "exhausted": 1, "skipped": 0},
                "database": {"backend": "sqlite", "size_bytes": 4096},
            },
        )
        self.assertEqual(list(payload), ["backup", "stats_calculated_at", "listeners", "media", "database"])
        db.get_operator_status_counts.assert_awaited_once_with(max_attempts=5)

    async def test_in_progress_flag_and_postgresql_backend(self):
        db = _mock_db({"backup_in_progress": "1"}, account_ids=[1], is_sqlite=False)

        payload = await collect_status(db, _mock_config())

        self.assertTrue(payload["backup"]["in_progress"])
        self.assertEqual(payload["database"]["backend"], "postgresql")

    async def test_account_list_failure_falls_back_to_the_default_account(self):
        db = _mock_db({"listener_active_since": "2026-01-15T05:00:00"}, account_ids=RuntimeError("no table"))

        payload = await collect_status(db, _mock_config())

        self.assertEqual(
            payload["listeners"], [{"account_id": 1, "active": True, "active_since": "2026-01-15T05:00:00"}]
        )

    async def test_metadata_failure_propagates(self):
        db = _mock_db({})
        db.get_metadata = AsyncMock(side_effect=RuntimeError("database down"))

        with self.assertRaises(RuntimeError):
            await collect_status(db, _mock_config())


class TestParseUtc(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(parse_utc(None))

    def test_both_stored_forms_are_utc(self):
        expected = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        self.assertEqual(parse_utc("2026-01-15T06:00:00Z"), expected)
        self.assertEqual(parse_utc("2026-01-15T06:00:00"), expected)

    def test_an_offset_is_converted(self):
        self.assertEqual(parse_utc("2026-01-15T08:00:00+02:00"), datetime(2026, 1, 15, 6, 0, tzinfo=UTC))


class TestPreviousFireTimes(unittest.TestCase):
    def test_every_six_hours(self):
        trigger = CronTrigger.from_crontab(EVERY_SIX_HOURS, timezone="UTC")

        fires = previous_fire_times(trigger, NOW)

        self.assertEqual(fires, [datetime(2026, 1, 15, 0, 0, tzinfo=UTC), datetime(2026, 1, 15, 6, 0, tzinfo=UTC)])

    def test_every_minute_uses_the_shortest_window(self):
        trigger = CronTrigger.from_crontab("* * * * *", timezone="UTC")

        fires = previous_fire_times(trigger, NOW)

        self.assertEqual(fires, [NOW - timedelta(minutes=1), NOW])

    def test_a_yearly_schedule_reaches_the_long_window(self):
        trigger = CronTrigger.from_crontab("0 0 1 1 *", timezone="UTC")

        fires = previous_fire_times(trigger, NOW)

        self.assertEqual(fires, [datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)])

    def test_a_schedule_that_never_fires_gives_nothing(self):
        trigger = CronTrigger.from_crontab("0 0 30 2 *", timezone="UTC")

        self.assertEqual(previous_fire_times(trigger, NOW), [])


class TestHealthProblems(unittest.TestCase):
    def _problems(self, status: dict, schedule: str = EVERY_SIX_HOURS) -> list[str]:
        return health_problems(status, schedule, now=NOW, timezone=UTC)

    def test_healthy_after_the_latest_scheduled_run(self):
        status = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T06:40:00")

        self.assertEqual(self._problems(status), [])

    def test_one_missed_tick_is_tolerated(self):
        # Started at 00:00; the 06:00 tick passed without a new run (a long run
        # makes the scheduler skip it). Not yet a missed schedule.
        status = _status("2026-01-15T00:00:05Z", stats_at="2026-01-15T05:00:00")

        self.assertEqual(self._problems(status), [])

    def test_no_backup_ever(self):
        self.assertEqual(self._problems(_status()), ["no backup has run yet"])

    def test_statistics_older_than_the_start_mean_it_did_not_finish(self):
        status = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T00:40:00")

        self.assertEqual(self._problems(status), ["the last backup, started 2026-01-15T06:00:05Z, did not finish"])

    def test_statistics_at_the_start_time_mean_it_finished(self):
        status = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T06:00:05")

        self.assertEqual(self._problems(status), [])

    def test_no_statistics_at_all_mean_it_did_not_finish(self):
        status = _status("2026-01-15T06:00:05Z")

        self.assertEqual(len(self._problems(status)), 1)

    def test_a_running_backup_is_not_unfinished(self):
        status = _status("2026-01-15T06:00:05Z", in_progress=True, stats_at="2026-01-15T00:40:00")

        self.assertEqual(self._problems(status), [])

    def test_missed_schedule(self):
        # Last start 18:00 the day before; 00:00 and 06:00 both passed.
        status = _status("2026-01-14T18:00:05Z", stats_at="2026-01-14T18:30:00")

        self.assertEqual(
            self._problems(status),
            [f"no backup has started since the run SCHEDULE ({EVERY_SIX_HOURS}) expected at 2026-01-15T00:00:00+00:00"],
        )

    def test_a_backup_running_past_a_whole_interval_is_flagged(self):
        status = _status("2026-01-14T18:00:05Z", in_progress=True, stats_at="2026-01-14T12:30:00")

        problems = self._problems(status)

        self.assertEqual(len(problems), 1)
        self.assertIn("no backup has started since", problems[0])

    def test_unfinished_and_missed_are_both_reported(self):
        status = _status("2026-01-14T18:00:05Z", stats_at="2026-01-14T12:30:00")

        self.assertEqual(len(self._problems(status)), 2)

    def test_a_schedule_that_never_fires_checks_nothing_about_age(self):
        status = _status("2020-01-01T00:00:00Z", stats_at="2020-01-01T01:00:00")

        self.assertEqual(self._problems(status, schedule="0 0 30 2 *"), [])

    def test_an_invalid_schedule_raises(self):
        status = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T06:40:00")

        with self.assertRaises(ValueError):
            self._problems(status, schedule="every day")

    def test_an_invalid_schedule_raises_before_any_backup(self):
        with self.assertRaises(ValueError):
            self._problems(_status(), schedule="0 3 * * 7")

    def test_defaults_to_the_current_time_and_local_zone(self):
        recent = datetime.now(UTC) - timedelta(minutes=5)
        status = _status(recent.strftime("%Y-%m-%dT%H:%M:%SZ"), stats_at=datetime.now(UTC).isoformat())

        self.assertEqual(health_problems(status, EVERY_SIX_HOURS), [])


class TestFormatStatus(unittest.TestCase):
    def test_healthy_summary(self):
        status = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T06:40:00")
        status["listeners"] = [
            {"account_id": 1, "active": True, "active_since": "2026-01-15T05:00:00"},
            {"account_id": 2, "active": False, "active_since": None},
        ]
        status["media"] = {"downloaded": 10, "pending": 2, "exhausted": 1, "skipped": 0}
        status["database"] = {"backend": "postgresql", "size_bytes": 5 * 1024 * 1024}

        text = format_status(status, [])

        self.assertEqual(
            text.splitlines(),
            [
                "Archive status: healthy",
                "  Last backup started:  2026-01-15T06:00:05Z",
                "  Statistics updated:   2026-01-15T06:40:00",
                "  Listener, account 1: active since 2026-01-15T05:00:00",
                "  Listener, account 2: not running",
                "  Media files:          10 downloaded, 2 pending, 1 exhausted, 0 skipped",
                "  Database:             postgresql, 5.0 MB",
            ],
        )

    def test_unhealthy_summary_lists_the_problems(self):
        status = _status(in_progress=True)
        status["database"]["size_bytes"] = None

        lines = format_status(status, ["first reason", "second reason"]).splitlines()

        self.assertEqual(lines[0], "Archive status: UNHEALTHY")
        self.assertEqual(lines[1], "  Last backup started:  never (running now)")
        self.assertEqual(lines[2], "  Statistics updated:   never")
        self.assertIn("  Database:             sqlite, size unknown", lines)
        self.assertEqual(lines[-3:], ["Problems:", "  - first reason", "  - second reason"])

    def test_size_units(self):
        cases = {512: "512.0 B", 2048: "2.0 KB", 3 * 1024**3: "3.0 GB", 2 * 1024**4: "2.0 TB"}
        for size, expected in cases.items():
            status = _status()
            status["database"]["size_bytes"] = size
            self.assertIn(f"sqlite, {expected}", format_status(status, []))


class TestStatusParser(unittest.TestCase):
    def test_status_command(self):
        args = create_parser().parse_args(["status"])

        self.assertEqual(args.command, "status")
        self.assertFalse(args.json)

    def test_status_json_flag(self):
        self.assertTrue(create_parser().parse_args(["status", "--json"]).json)

    def test_main_dispatches_status(self):
        from telegram_archive.__main__ import main

        with (
            patch.object(sys, "argv", ["telegram-archive", "status"]),
            patch("telegram_archive.__main__.run_status", new=MagicMock(return_value="coroutine")) as run,
            patch("telegram_archive.__main__.asyncio.run", return_value=1) as mock_run,
        ):
            self.assertEqual(main(), 1)

        run.assert_called_once()
        mock_run.assert_called_once_with("coroutine")


class TestRunStatus(unittest.IsolatedAsyncioTestCase):
    """The command with the database and the clock mocked out."""

    def setUp(self):
        self.config = _mock_config()
        self.manager = MagicMock()
        self.healthy = _status("2026-01-15T06:00:05Z", stats_at="2026-01-15T06:40:00")

    async def _run(
        self,
        *,
        json_output=False,
        status=None,
        problems=None,
        init_error=None,
        collect_error=None,
        health_error=None,
    ):
        args = MagicMock()
        args.json = json_output
        init = AsyncMock(return_value=self.manager, side_effect=init_error)
        collect = AsyncMock(return_value=status or self.healthy, side_effect=collect_error)
        close = AsyncMock()
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("telegram_archive.config.Config", return_value=self.config),
            patch("telegram_archive.config.setup_logging"),
            patch("telegram_archive.db.init_database", init),
            patch("telegram_archive.db.close_database", close),
            patch("telegram_archive.status.collect_status", collect),
            patch(
                "telegram_archive.status.health_problems", return_value=problems or [], side_effect=health_error
            ) as health,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = await run_status(args)
        return code, out.getvalue(), err.getvalue(), close, health

    async def test_healthy_prints_the_summary_and_exits_0(self):
        code, out, err, close, health = await self._run()

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Archive status: healthy"))
        self.assertEqual(err, "")
        close.assert_awaited_once()
        health.assert_called_once_with(self.healthy, EVERY_SIX_HOURS)

    async def test_unhealthy_exits_1(self):
        code, out, _, _, _ = await self._run(problems=["no backup has run yet"])

        self.assertEqual(code, 1)
        self.assertIn("  - no backup has run yet", out)

    async def test_json_is_the_route_payload_plus_the_verdict(self):
        code, out, _, _, _ = await self._run(json_output=True)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {**self.healthy, "healthy": True, "problems": []})

    async def test_json_unhealthy_exits_1(self):
        code, out, _, _, _ = await self._run(json_output=True, problems=["reason"])

        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["healthy"])
        self.assertEqual(payload["problems"], ["reason"])

    async def test_unreachable_database_exits_1_and_still_closes(self):
        code, out, err, close, _ = await self._run(init_error=ConnectionRefusedError("refused"))

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Status failed: refused", err)
        close.assert_awaited_once()

    async def test_a_failing_query_exits_1_and_still_closes(self):
        code, _, err, close, _ = await self._run(collect_error=RuntimeError("no such table"))

        self.assertEqual(code, 1)
        self.assertIn("Status failed", err)
        close.assert_awaited_once()

    async def test_an_invalid_schedule_exits_1(self):
        code, out, err, close, _ = await self._run(health_error=ValueError("bad cron"))

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Status failed: bad cron", err)
        close.assert_awaited_once()

    async def test_a_config_error_exits_1(self):
        args = MagicMock()
        err = io.StringIO()
        with patch("telegram_archive.config.Config", side_effect=ValueError("bad setting")), redirect_stderr(err):
            self.assertEqual(await run_status(args), 1)
        self.assertIn("bad setting", err.getvalue())


# ---------------------------------------------------------------------------
# Real engines: SQLite always, PostgreSQL when TEST_POSTGRES_URL reaches one.
# ---------------------------------------------------------------------------


def _iso_z(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat() + "Z"


async def _seed(adapter, last_run: datetime, stats_at: datetime) -> None:
    await adapter.ensure_account(telegram_user_id=1000001, env_index=1, label="Account A")
    await adapter.set_metadata("last_backup_time", _iso_z(last_run))
    await adapter.set_metadata("backup_in_progress", "0")
    await adapter.set_metadata("stats_calculated_at", stats_at.replace(tzinfo=None).isoformat())
    await adapter.set_metadata("listener_active_since", "2026-01-15T05:00:00")


async def test_collect_status_on_a_real_engine(real_adapter):
    now = datetime.now(UTC)
    await _seed(real_adapter, now - timedelta(minutes=5), now)

    payload = await collect_status(real_adapter, _mock_config())

    backend = "sqlite" if real_adapter.db_manager._is_sqlite else "postgresql"
    assert payload["backup"] == {"last_run": _iso_z(now - timedelta(minutes=5)), "in_progress": False}
    assert payload["listeners"] == [{"account_id": 1, "active": True, "active_since": "2026-01-15T05:00:00"}]
    assert payload["media"] == {"downloaded": 0, "pending": 0, "exhausted": 0, "skipped": 0}
    assert payload["database"]["backend"] == backend
    assert isinstance(payload["database"]["size_bytes"], int)
    assert payload["database"]["size_bytes"] > 0


@pytest.mark.parametrize(
    ("age", "expected_code"),
    [(timedelta(minutes=5), 0), (timedelta(days=30), 1)],
    ids=["recent", "stale"],
)
async def test_status_command_on_a_real_engine(real_adapter, age, expected_code, tmp_path):
    now = datetime.now(UTC)
    await _seed(real_adapter, now - age, now - age + timedelta(minutes=1))
    args = create_parser().parse_args(["status", "--json"])
    env = {
        "DATABASE_URL": real_adapter.db_manager.database_url,
        "BACKUP_PATH": str(tmp_path),
        "SCHEDULE": EVERY_SIX_HOURS,
    }
    out = io.StringIO()
    with patch.dict(os.environ, env), redirect_stdout(out):
        code = await run_status(args)

    payload = json.loads(out.getvalue())
    assert code == expected_code
    assert payload["healthy"] is (expected_code == 0)
    assert payload["backup"]["last_run"] == _iso_z(now - age)
