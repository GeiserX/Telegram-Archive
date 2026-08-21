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

    def test_compose_loads_dotenv_into_both_containers(self):
        """The README path is `cp .env.example .env; docker compose up -d` —
        but compose uses .env only to interpolate ${VAR} references in the
        file. Without env_file, every documented variable missing from an
        environment: block is silently inert inside the container; worst is
        DISPLAY_CHAT_IDS, where a viewer shared assuming restriction
        restricts nothing."""
        compose = (REPO / "docker-compose.yml").read_text()
        backup, viewer = compose.split("telegram-viewer:")
        # Backup: wholesale env_file — every documented variable reaches it.
        assert backup.count("env_file:") == 1
        assert backup.count("- path: .env") == 1
        assert backup.count("required: false") == 1
        # Viewer: DELIBERATELY no env_file (capture credentials must not enter
        # the exposed container) — instead every documented viewer setting has
        # an explicit interpolated entry.
        assert "env_file:" not in viewer
        for var in (
            "DISPLAY_CHAT_IDS",
            "CORS_ORIGINS",
            "SECURE_COOKIES",
            "PUSH_NOTIFICATIONS",
            "VAPID_PRIVATE_KEY",
            "VAPID_PUBLIC_KEY",
            "VAPID_CONTACT",
            "AUTH_PROXY_HEADER",
            "AUTH_PROXY_ADMIN_USERS",
            "AUTH_PROXY_DEFAULT_ACCESS",
        ):
            assert f"{var}: ${{{var}:-" in viewer, f"viewer allowlist missing {var}"

    def test_run_forever_registers_loop_signal_handlers(self):
        src = (REPO / "src" / "scheduler.py").read_text()
        assert "loop.add_signal_handler(signum, self._request_shutdown, main_task, signum)" in src
        assert "except asyncio.CancelledError:" in src.split("async def run_forever", 1)[1]


class TestTeardownRobustness:
    async def test_failing_listener_stop_never_skips_disconnect(self, monkeypatch, tmp_path):
        """Review finding: teardown steps must be independent."""
        scheduler = _bare_scheduler(monkeypatch, tmp_path)
        scheduler._stop_listener = unittest.mock.AsyncMock(side_effect=RuntimeError("close failed"))
        connect_started = asyncio.Event()

        async def hanging_connect():
            connect_started.set()
            await asyncio.sleep(3600)

        scheduler._connect = hanging_connect
        task = asyncio.create_task(scheduler.run_forever())
        await connect_started.wait()
        scheduler._request_shutdown(task, 15)
        await task

        scheduler._disconnect.assert_awaited()

    async def test_failing_scheduler_stop_never_skips_disconnect(self, monkeypatch, tmp_path):
        scheduler = _bare_scheduler(monkeypatch, tmp_path)
        scheduler.stop = unittest.mock.MagicMock(side_effect=RuntimeError("apscheduler wedged"))
        connect_started = asyncio.Event()

        async def hanging_connect():
            connect_started.set()
            await asyncio.sleep(3600)

        scheduler._connect = hanging_connect
        task = asyncio.create_task(scheduler.run_forever())
        await connect_started.wait()
        scheduler._request_shutdown(task, 15)
        await task

        scheduler._disconnect.assert_awaited()

    async def test_failing_disconnect_still_completes_cleanly(self, monkeypatch, tmp_path):
        scheduler = _bare_scheduler(monkeypatch, tmp_path)
        scheduler._disconnect = unittest.mock.AsyncMock(side_effect=RuntimeError("transport gone"))
        connect_started = asyncio.Event()

        async def hanging_connect():
            connect_started.set()
            await asyncio.sleep(3600)

        scheduler._connect = hanging_connect
        task = asyncio.create_task(scheduler.run_forever())
        await connect_started.wait()
        scheduler._request_shutdown(task, 15)
        await task  # must not raise despite the failing disconnect

        assert task.done() and not task.cancelled()

    def test_signal_handlers_are_removed_after_teardown(self):
        """Review finding: removal restores SIG_DFL, so it must run last."""
        src = (REPO / "src" / "scheduler.py").read_text()
        finally_block = src.split("async def run_forever", 1)[1]
        assert finally_block.index("await self._disconnect()") < finally_block.index(
            "loop.remove_signal_handler(signum)"
        )
