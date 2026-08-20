"""Healthchecks (9t6.8.10): a dead archiver must not look healthy.

The backup container's liveness is a heartbeat file the scheduler touches
while its event loop is responsive; the viewer's is its own /api/health
answering "ok". Both checkers are real scripts so they are testable here
and identical to what the HEALTHCHECK runs in the image.
"""

import asyncio
import importlib.util
import json
import os
import time
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBackupChecker:
    def test_fresh_heartbeat_is_healthy(self, tmp_path, monkeypatch):
        checker = _load("healthcheck_backup")
        beat = tmp_path / "beat"
        beat.write_text("now")
        monkeypatch.setenv("HEARTBEAT_FILE", str(beat))
        assert checker.main() == 0

    def test_stale_heartbeat_is_unhealthy(self, tmp_path, monkeypatch):
        checker = _load("healthcheck_backup")
        beat = tmp_path / "beat"
        beat.write_text("old")
        stale = time.time() - 10_000
        os.utime(beat, (stale, stale))
        monkeypatch.setenv("HEARTBEAT_FILE", str(beat))
        assert checker.main() == 1

    def test_missing_heartbeat_is_unhealthy(self, tmp_path, monkeypatch):
        checker = _load("healthcheck_backup")
        monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "never-written"))
        assert checker.main() == 1

    def test_max_age_is_configurable(self, tmp_path, monkeypatch):
        checker = _load("healthcheck_backup")
        beat = tmp_path / "beat"
        beat.write_text("x")
        old = time.time() - 120
        os.utime(beat, (old, old))
        monkeypatch.setenv("HEARTBEAT_FILE", str(beat))
        monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "60")
        assert checker.main() == 1
        monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "600")
        assert checker.main() == 0


class TestViewerChecker:
    def _respond(self, status_code: int, payload: dict):
        response = unittest.mock.MagicMock()
        response.status = status_code
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = lambda s: s
        response.__exit__ = lambda s, *a: False
        return response

    def test_ok_is_healthy(self):
        checker = _load("healthcheck_viewer")
        with unittest.mock.patch.object(
            checker.urllib.request, "urlopen", return_value=self._respond(200, {"status": "ok"})
        ):
            assert checker.main() == 0

    def test_degraded_is_unhealthy(self):
        checker = _load("healthcheck_viewer")
        with unittest.mock.patch.object(
            checker.urllib.request, "urlopen", return_value=self._respond(200, {"status": "degraded"})
        ):
            assert checker.main() == 1

    def test_unreachable_is_unhealthy(self):
        checker = _load("healthcheck_viewer")
        with unittest.mock.patch.object(checker.urllib.request, "urlopen", side_effect=OSError("refused")):
            assert checker.main() == 1


class TestHeartbeatLoop:
    async def test_heartbeat_writes_and_refreshes(self, tmp_path, monkeypatch):
        from src.scheduler import BackupScheduler

        beat = tmp_path / "beat"
        monkeypatch.setenv("HEARTBEAT_FILE", str(beat))
        scheduler = BackupScheduler.__new__(BackupScheduler)

        ticks = 0
        real_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            nonlocal ticks
            ticks += 1
            if ticks >= 2:
                raise asyncio.CancelledError
            await real_sleep(0)

        with unittest.mock.patch("src.scheduler.asyncio.sleep", side_effect=fast_sleep):
            try:
                await scheduler._heartbeat_loop()
            except asyncio.CancelledError:
                pass

        assert beat.exists()
        assert beat.read_text().isdigit()

    async def test_write_failure_warns_but_does_not_die(self, tmp_path, monkeypatch):
        from src.scheduler import BackupScheduler

        monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "nodir" / "beat"))
        scheduler = BackupScheduler.__new__(BackupScheduler)

        async def one_tick(seconds):
            raise asyncio.CancelledError

        with unittest.mock.patch("src.scheduler.asyncio.sleep", side_effect=one_tick):
            try:
                await scheduler._heartbeat_loop()  # OSError inside must not propagate
            except asyncio.CancelledError:
                pass


class TestImageWiring:
    def test_both_dockerfiles_declare_healthchecks(self):
        backup = (REPO / "Dockerfile").read_text()
        viewer = (REPO / "Dockerfile.viewer").read_text()
        assert "HEALTHCHECK" in backup and "healthcheck_backup.py" in backup
        assert "HEALTHCHECK" in viewer and "healthcheck_viewer.py" in viewer
        assert "COPY scripts/healthcheck_viewer.py" in viewer

    def test_scheduler_starts_the_heartbeat_before_connecting(self):
        src = (REPO / "src" / "scheduler.py").read_text()
        start = src.index("health_heartbeat")
        connect = src.index("await self._connect()")
        assert start < connect, "heartbeat must start before the (possibly slow) connect"


class TestHeartbeatLifetime:
    async def test_startup_failure_cancels_the_heartbeat(self, tmp_path, monkeypatch):
        """Review edge: a failed connect must not leave the heartbeat ticking
        a healthy file behind on a still-alive event loop."""
        from src.scheduler import BackupScheduler

        monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "beat"))
        scheduler = BackupScheduler.__new__(BackupScheduler)
        scheduler._connect = unittest.mock.AsyncMock(side_effect=RuntimeError("no network"))
        # The consolidated finally now always runs full teardown.
        scheduler._stop_listener = unittest.mock.AsyncMock()
        scheduler._disconnect = unittest.mock.AsyncMock()
        scheduler.stop = unittest.mock.MagicMock()

        try:
            await scheduler.run_forever()
        except RuntimeError:
            pass

        lingering = [t for t in asyncio.all_tasks() if t.get_name() == "health_heartbeat" and not t.done()]
        assert lingering == [], "heartbeat task must be cancelled on startup failure"
