"""The small public Python API: ``from telegram_archive import Config, TelegramBackup, run_backup``.

The names are lazy. ``import telegram_archive`` must not pull in telethon,
because the viewer image imports the package without it installed. Each
check runs in a fresh interpreter so this process's imports cannot hide an
eager one.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _python(script: str, tmp_path) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["PYTHONPATH"] = str(ROOT)
    env["BACKUP_PATH"] = str(tmp_path / "backups")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_importing_the_package_loads_nothing_heavy(tmp_path):
    result = _python(
        """
        import sys

        import telegram_archive

        heavy = [name for name in ("telethon", "telegram_archive.config", "telegram_archive.telegram_backup") if name in sys.modules]
        assert not heavy, heavy
        assert telegram_archive.__version__
        print("ok")
        """,
        tmp_path,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-3000:]


def test_the_public_names_are_the_real_objects(tmp_path):
    result = _python(
        """
        import inspect

        import telegram_archive
        from telegram_archive import Config, TelegramBackup, run_backup
        from telegram_archive import config, telegram_backup

        assert Config is config.Config
        assert TelegramBackup is telegram_backup.TelegramBackup
        assert run_backup is telegram_backup.run_backup
        assert inspect.iscoroutinefunction(run_backup)
        assert sorted(telegram_archive.__all__) == ["Config", "TelegramBackup", "__version__", "run_backup"]
        assert {"Config", "TelegramBackup", "run_backup"} <= set(dir(telegram_archive))

        try:
            telegram_archive.no_such_name
        except AttributeError as exc:
            assert "no_such_name" in str(exc)
        else:
            raise AssertionError("an unknown name must raise AttributeError")
        print("ok")
        """,
        tmp_path,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-3000:]


def test_config_reads_the_environment(tmp_path):
    result = _python(
        """
        import os

        os.environ["TELEGRAM_API_ID"] = "12345"
        os.environ["TELEGRAM_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["TELEGRAM_PHONE"] = "+15550100"

        from telegram_archive import Config

        config = Config()
        assert config.api_id == 12345
        print("ok")
        """,
        tmp_path,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-3000:]
