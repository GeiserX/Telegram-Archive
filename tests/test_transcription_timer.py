"""The transcription drain on its own timer in ``schedule`` (#556).

Since 9.2.0 the full pass runs once a day, and the drain ran only at its end,
so a pressed file, a backlog and akou results for files the listener sent
waited up to a day. ``schedule`` now drains every
TRANSCRIPTION_DRAIN_INTERVAL_MINUTES too. A tick with nothing waiting sends
no request and logs nothing at info level; it reads the database only. One
drain runs at a time in a process: a tick that finds a drain running skips,
and the pass waits for a timer drain to end. Shutdown cancels the timer and
closes its adapter. Telegram is never needed.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import update

from telegram_archive.db.models import MediaTranscript
from telegram_archive.message_utils import utcnow_naive
from telegram_archive.scheduler import BackupScheduler
from telegram_archive.transcription import STALE_QUEUED, drain_if_waiting, drain_transcriptions

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription import (  # noqa: E402
    URL,
    AkouServer,
    FakeServer,
    _akou_config,
    _akou_drain,
    _client,
    _config,
    _media,
    _rows,
)

MINUTES = 15


class GatedServer(FakeServer):
    """``FakeServer`` whose transcription answers wait for ``release``, counting requests in flight."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.most_in_flight = 0
        self.transport = httpx.MockTransport(self._handle_gated)

    async def _handle_gated(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if request.url.path.endswith("/v1/audio/transcriptions"):
                self.entered.set()
                await self.release.wait()
            return self._handle(request)
        finally:
            self.in_flight -= 1


class DownServer(FakeServer):
    """A transcription server nothing can reach."""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("cannot reach the server")


def _timer_config(tmp_path, **overrides):
    config = _config(str(tmp_path), transcription_drain_interval_minutes=MINUTES, **overrides)
    config.for_account = lambda index: config
    return config


def _scheduler(config, *row_ids) -> BackupScheduler:
    """A scheduler with one account per row id (None: not resolved yet), built without signal handlers."""
    scheduler = BackupScheduler.__new__(BackupScheduler)
    scheduler.config = config
    scheduler._accounts = [
        SimpleNamespace(account=SimpleNamespace(index=index), row_id=row_id, log_prefix=f"[account {index}] ")
        for index, row_id in enumerate(row_ids, 1)
    ]
    return scheduler


def _clients(server: FakeServer):
    """Every client the drain builds itself talks to ``server``."""
    return patch(
        "telegram_archive.transcription.TranscriptionClient", side_effect=lambda config: _client(config, server)
    )


async def _voice(adapter, tmp_path, media_id: str, day: int, **kwargs) -> dict:
    return await _media(adapter, tmp_path, media_id, download_date=datetime(2026, 1, day, 3, 4, 5), **kwargs)


async def _press(adapter, media_id: str) -> dict:
    """The viewer's press: a queued row with no preset."""
    return await adapter.enqueue_media_transcript(media_id, account_id=1)


async def _set_row(adapter, row_id: int, **values) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        await session.execute(update(MediaTranscript).where(MediaTranscript.id == row_id).values(**values))
        await session.commit()


def _info(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.INFO]


def _summaries(caplog, level: int = logging.INFO) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level and r.getMessage().startswith("Transcription drain:")]


class TestTick:
    async def test_a_tick_drains_a_press_and_the_backlog_without_a_pass(self, real_adapter, tmp_path):
        """The press goes first even on the oldest file, then the newest backlog, up to the budget."""
        config = _timer_config(tmp_path, transcription_backfill_per_run=2)
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        await _voice(real_adapter, tmp_path, "m_2_voice", 2)
        await _voice(real_adapter, tmp_path, "m_3_voice", 3)
        await _voice(real_adapter, tmp_path, "m_4_voice", 4)
        await _press(real_adapter, "m_1_voice")
        server = FakeServer()

        with _clients(server):
            await _scheduler(config, 1)._drain_tick(real_adapter, AsyncMock())

        assert len(server.transcribe_requests) == 2
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["done"]
        assert [r["status"] for r in await _rows(real_adapter, "m_4_voice")] == ["done"]
        assert await _rows(real_adapter, "m_2_voice") == []
        assert await _rows(real_adapter, "m_3_voice") == []

    async def test_a_tick_fills_an_open_akou_job_from_the_event_feed(self, real_adapter, tmp_path, caplog):
        """A file the listener sent, no callback set: the result arrives without a pass, one info line."""
        config = _akou_config(tmp_path, transcription_drain_interval_minutes=MINUTES)
        config.for_account = lambda index: config
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        server = AkouServer()
        await _akou_drain(config, real_adapter, server)
        [row] = await _rows(real_adapter, "m_1_voice")
        server.finish(row["job_id"])
        caplog.set_level(logging.INFO)
        caplog.clear()

        with _clients(server):
            await _scheduler(config, 1)._drain_tick(real_adapter, AsyncMock())

        [row] = await _rows(real_adapter, "m_1_voice")
        assert (row["status"], row["text"]) == ("done", "hola desde akou")
        assert len(server.submits) == 1
        [summary] = _summaries(caplog)
        assert "1 filled from the event feed" in summary.getMessage()

    @pytest.mark.parametrize("state", ("empty", "done", "file_missing"))
    async def test_nothing_waiting_sends_no_request_and_logs_no_info(self, real_adapter, tmp_path, caplog, state):
        config = _timer_config(tmp_path)
        if state == "done":
            await _voice(real_adapter, tmp_path, "m_1_voice", 1)
            row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
            await real_adapter.fill_media_transcript(row["id"], status="done", text="", completed_at=utcnow_naive())
        elif state == "file_missing":
            # Its file is still missing: the check of it waits for a drain with other work, or the pass.
            await _voice(real_adapter, tmp_path, "m_1_voice", 1, on_disk=False)
            row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
            await real_adapter.fill_media_transcript(row["id"], status="failed", error="file_missing")
        server = FakeServer()
        caplog.set_level(logging.DEBUG)

        with _clients(server):
            await _scheduler(config, 1)._drain_tick(real_adapter, AsyncMock())

        assert server.requests == []
        assert _info(caplog) == []

    async def test_an_unresolved_account_is_skipped_and_a_failing_one_stops_nothing(self, tmp_path, caplog):
        config = _timer_config(tmp_path)
        calls = []

        async def drain(config, db, *, account_id, notifier):
            calls.append(account_id)
            if account_id == 7:
                raise RuntimeError(f"text that may hold {URL}")

        with patch("telegram_archive.transcription.drain_if_waiting", side_effect=drain):
            await _scheduler(config, None, 7, 8)._drain_tick(MagicMock(), AsyncMock())

        assert calls == [7, 8]
        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning.getMessage() == "[account 2] Transcription drain failed: RuntimeError"

    @pytest.mark.parametrize(
        "make_server", (DownServer, lambda: FakeServer(transcribe_down=True)), ids=("detect", "upload")
    )
    async def test_an_unreachable_server_leaves_the_press_queued_with_one_warning(
        self, real_adapter, tmp_path, caplog, make_server
    ):
        config = _timer_config(tmp_path)
        server = make_server()
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        await _press(real_adapter, "m_1_voice")
        caplog.set_level(logging.INFO)

        with _clients(server):
            await _scheduler(config, 1)._drain_tick(real_adapter, AsyncMock())

        assert server.requests
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["queued"]
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


class TestOneDrainAtATime:
    async def test_a_tick_skips_while_the_pass_drains(self, real_adapter, tmp_path):
        config = _timer_config(tmp_path, transcription_backfill_per_run=1)
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        await _voice(real_adapter, tmp_path, "m_2_voice", 2)
        gated = GatedServer()
        other = FakeServer()
        backup = asyncio.create_task(
            drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, gated)
            )
        )
        await asyncio.wait_for(gated.entered.wait(), 5)

        # A file still waits, so only the running drain can stop this tick.
        result = await asyncio.wait_for(
            drain_if_waiting(config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, other)),
            5,
        )

        assert result is None
        assert other.requests == []
        gated.release.set()
        assert (await backup)["done"] == 1

    async def test_the_pass_waits_for_a_timer_drain(self, real_adapter, tmp_path):
        config = _timer_config(tmp_path, transcription_backfill_per_run=1)
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        await _voice(real_adapter, tmp_path, "m_2_voice", 2)
        gated = GatedServer()
        timer = asyncio.create_task(
            drain_if_waiting(config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, gated))
        )
        await asyncio.wait_for(gated.entered.wait(), 5)
        backup = asyncio.create_task(
            drain_transcriptions(
                config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, gated)
            )
        )
        # Without the shared lock the pass would ask the server at once.
        for _ in range(100):
            if gated.most_in_flight > 1:
                break
            await asyncio.sleep(0.01)

        assert not backup.done()
        assert gated.most_in_flight == 1
        gated.release.set()
        assert (await timer)["done"] == 1
        assert (await backup)["done"] == 1
        assert gated.most_in_flight == 1
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["done"]
        assert [r["status"] for r in await _rows(real_adapter, "m_2_voice")] == ["done"]


class TestSummaryLine:
    async def test_a_drain_of_file_checks_alone_logs_no_info_line(self, real_adapter, tmp_path, caplog):
        """The pass still looks at a missing file; finding it still missing changes nothing worth a line."""
        # The media folder is not mounted: nothing is marked to download again.
        config = _config(str(tmp_path / "not-mounted"))
        await _voice(real_adapter, tmp_path, "m_1_voice", 1, on_disk=False)
        row = await real_adapter.enqueue_media_transcript("m_1_voice", account_id=1)
        await real_adapter.fill_media_transcript(row["id"], status="failed", error="file_missing")
        caplog.set_level(logging.DEBUG)

        stats = await drain_transcriptions(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, FakeServer())
        )

        assert stats["noop"] == 1
        assert _summaries(caplog) == []
        assert len(_summaries(caplog, logging.DEBUG)) == 1

    async def test_a_picked_up_press_waits_out_the_ten_minutes(self, real_adapter, tmp_path):
        """A press a drain picked up and left queued (an outage) is work again after ten minutes, as in the pass."""
        config = _timer_config(tmp_path)
        await _voice(real_adapter, tmp_path, "m_1_voice", 1)
        row = await _press(real_adapter, "m_1_voice")
        await _set_row(real_adapter, row["id"], preset="auto")
        server = FakeServer()

        fresh = await drain_if_waiting(config, real_adapter, account_id=1, client=_client(config, server))
        await _set_row(real_adapter, row["id"], requested_at=utcnow_naive() - STALE_QUEUED * 2)
        stale = await drain_if_waiting(
            config, real_adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
        )

        assert fresh is None
        assert stale["done"] == 1


class TestLoop:
    async def test_it_sleeps_the_interval_then_ticks_with_one_adapter(self, tmp_path):
        scheduler = _scheduler(_timer_config(tmp_path), 1)
        sleeps, ticks = [], []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        async def tick(db, notifier):
            ticks.append((len(sleeps), db))

        scheduler._drain_tick = tick
        db = SimpleNamespace(db_manager=None, close=AsyncMock())
        create = AsyncMock(return_value=db)
        with patch("telegram_archive.db.create_adapter", create), pytest.raises(asyncio.CancelledError):
            await scheduler._drain_loop(MINUTES * 60, sleep=sleep)

        assert sleeps == [MINUTES * 60] * 3
        assert ticks == [(1, db), (2, db)]
        create.assert_awaited_once()
        db.close.assert_awaited_once()

    async def test_a_database_that_cannot_be_opened_is_tried_on_the_next_tick(self, tmp_path, caplog):
        scheduler = _scheduler(_timer_config(tmp_path), 1)
        sleeps, ticks = [], []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        async def tick(db, notifier):
            ticks.append(len(sleeps))

        scheduler._drain_tick = tick
        db = SimpleNamespace(db_manager=None, close=AsyncMock())
        create = AsyncMock(side_effect=[OSError("no database at /data/archive.db"), db])
        with patch("telegram_archive.db.create_adapter", create), pytest.raises(asyncio.CancelledError):
            await scheduler._drain_loop(60, sleep=sleep)

        assert ticks == [2]
        [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning.getMessage() == "Transcription drain: database not available (OSError)"


def _bare(config) -> BackupScheduler:
    """run_forever with every Telegram step mocked out, as in test_graceful_shutdown."""
    scheduler = BackupScheduler.__new__(BackupScheduler)
    scheduler.running = False
    scheduler.scheduler = MagicMock()
    scheduler._accounts = []
    scheduler.config = config
    scheduler.start = MagicMock()
    scheduler.stop = MagicMock()
    scheduler._connect = AsyncMock()
    scheduler._start_listener = AsyncMock()
    scheduler._stop_listener = AsyncMock()
    scheduler._disconnect = AsyncMock()
    scheduler._backup_lock = asyncio.Lock()
    return scheduler


def _settings(enabled=True, url=URL, minutes=MINUTES) -> SimpleNamespace:
    return SimpleNamespace(
        enable_listener=False,
        transcription_enabled=enabled,
        transcription_url=url,
        transcription_drain_interval_minutes=minutes,
    )


class TestRunForever:
    @pytest.mark.parametrize(
        ("config", "expected"),
        (
            (_settings(), [MINUTES * 60]),
            (_settings(minutes=0), []),
            (_settings(enabled=False), []),
            (_settings(url=""), []),
            (MagicMock(), []),
        ),
        ids=("on", "interval-0", "transcription-off", "no-url", "mock-config"),
    )
    async def test_the_timer_starts_only_when_it_has_work_to_do(self, monkeypatch, tmp_path, caplog, config, expected):
        monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "beat"))
        scheduler = _bare(config)
        started = []

        def loop(interval, sleep=None):
            started.append(interval)
            return asyncio.Event().wait()

        scheduler._drain_loop = loop
        caplog.set_level(logging.INFO)

        await asyncio.wait_for(scheduler.run_forever(), 5)

        assert started == expected
        lines = [r.getMessage() for r in caplog.records if "TRANSCRIPTION_DRAIN_INTERVAL_MINUTES" in r.getMessage()]
        assert lines == (
            ["Transcription drain every 15 minutes (TRANSCRIPTION_DRAIN_INTERVAL_MINUTES)"] if expected else []
        )
        assert [t for t in asyncio.all_tasks() if t.get_name() == "transcription_drain"] == []

    async def test_shutdown_cancels_a_running_drain_and_closes_its_adapter(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "beat"))
        scheduler = _bare(_settings())
        scheduler.running = True
        in_tick = asyncio.Event()
        seen = []

        async def tick(db, notifier):
            in_tick.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise

        async def no_wait(seconds):
            await asyncio.sleep(0)

        scheduler._drain_tick = tick
        scheduler._drain_loop = lambda interval: BackupScheduler._drain_loop(scheduler, interval, sleep=no_wait)
        db = SimpleNamespace(db_manager=None, close=AsyncMock())
        with patch("telegram_archive.db.create_adapter", AsyncMock(return_value=db)):
            task = asyncio.create_task(scheduler.run_forever())
            await asyncio.wait_for(in_tick.wait(), 5)
            scheduler._request_shutdown(task, 15)
            # Not wait_for: its own cancel on a timeout would reach the drain and hide a teardown that never cancels it.
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                task.cancel()

        assert done == {task}
        assert seen == ["cancelled"]
        db.close.assert_awaited_once()
        assert scheduler._drain_task is None
        assert [t for t in asyncio.all_tasks() if t.get_name() == "transcription_drain"] == []
