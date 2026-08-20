"""Graceful shutdown (9t6.8.9): docker stop must not end in SIGKILL.

The sync signal handlers only flip ``running``, which nothing checks until
the keep-alive loop — so a SIGTERM during connect or an hours-long initial
sweep used to ride out the grace period into SIGKILL, mid-write. The
asyncio-native handler cancels run_forever so the finally teardown runs.
"""

import asyncio
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _bare_scheduler(monkeypatch, tmp_path):
    from src.scheduler import BackupScheduler

    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "beat"))
    scheduler = BackupScheduler.__new__(BackupScheduler)
    scheduler.running = True
    scheduler.scheduler = unittest.mock.MagicMock()
    scheduler._accounts = []
    scheduler.config = unittest.mock.MagicMock()
    scheduler.start = unittest.mock.MagicMock()
    scheduler.stop = unittest.mock.MagicMock()
    scheduler._start_listener = unittest.mock.AsyncMock()
    scheduler._stop_listener = unittest.mock.AsyncMock()
    scheduler._disconnect = unittest.mock.AsyncMock()
    scheduler._backup_lock = asyncio.Lock()
    return scheduler


class TestGracefulCancel:
    async def test_cancel_during_connect_runs_full_teardown(self, monkeypatch, tmp_path):
        scheduler = _bare_scheduler(monkeypatch, tmp_path)
        connect_started = asyncio.Event()

        async def hanging_connect():
            connect_started.set()
            await asyncio.sleep(3600)

        scheduler._connect = hanging_connect

        task = asyncio.create_task(scheduler.run_forever())
        await connect_started.wait()
        scheduler._request_shutdown(task, 15)

        # Clean return: the cancel is the graceful path, not an error.
        await task
        assert task.done() and not task.cancelled()
        scheduler._stop_listener.assert_awaited()
        scheduler._disconnect.assert_awaited()
        lingering = [t for t in asyncio.all_tasks() if t.get_name() == "health_heartbeat" and not t.done()]
        assert lingering == []

    async def test_second_signal_does_not_recancel_during_teardown(self, monkeypatch, tmp_path):
        scheduler = _bare_scheduler(monkeypatch, tmp_path)
        scheduler._shutdown_requested = False
        fake_task = unittest.mock.MagicMock()
        fake_task.done.return_value = False

        scheduler._request_shutdown(fake_task, 15)
        scheduler._request_shutdown(fake_task, 15)

        assert fake_task.cancel.call_count == 1


class TestOpsWiring:
    def test_compose_has_grace_period_and_log_rotation_on_both_services(self):
        compose = (REPO / "docker-compose.yml").read_text()
        assert compose.count("stop_grace_period: 90s") == 2
        assert compose.count('max-size: "10m"') == 2

    def test_run_forever_registers_loop_signal_handlers(self):
        src = (REPO / "src" / "scheduler.py").read_text()
        assert "loop.add_signal_handler(signum, self._request_shutdown, main_task, signum)" in src
        assert "except asyncio.CancelledError:" in src.split("async def run_forever", 1)[1]
