"""The old package name ``src`` keeps working for existing Docker deployments.

Compose files written for older images run ``python -m src schedule``,
``python -m src auth`` and ``uvicorn src.web.main:app``. The compatibility
package in ``src/`` must serve those without loading a second copy of any
module: a second copy would mean a second config, a second database engine
and a second FastAPI app. Everything here runs in a fresh interpreter so the
import state of the test process never decides the result.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTE = "the 'src' module name is deprecated"


def _run(args, tmp_path, **kwargs):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env["BACKUP_PATH"] = str(tmp_path / "backups")
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, **kwargs)


def test_python_m_src_help_runs_the_cli(tmp_path):
    result = _run([sys.executable, "-m", "src", "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "usage: telegram-archive" in result.stdout
    assert "schedule" in result.stdout
    assert result.stderr.count(NOTE) == 1, result.stderr


def test_python_m_src_matches_python_m_telegram_archive(tmp_path):
    old = _run([sys.executable, "-m", "src", "--help"], tmp_path)
    new = _run([sys.executable, "-m", "telegram_archive", "--help"], tmp_path)
    assert new.returncode == 0, new.stderr
    assert old.stdout == new.stdout
    assert NOTE not in new.stderr


def test_python_m_src_unknown_subcommand_fails_like_the_new_name(tmp_path):
    result = _run([sys.executable, "-m", "src", "no-such-command"], tmp_path)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_old_module_paths_are_the_same_module_objects(tmp_path):
    script = textwrap.dedent(
        """
        import sys

        import src.web.main
        import telegram_archive.web.main
        from src.config import Config as OldConfig
        from telegram_archive.config import Config

        assert src.web.main is telegram_archive.web.main
        assert src.web.main.app is telegram_archive.web.main.app
        assert OldConfig is Config
        assert telegram_archive.web.main.__spec__.name == "telegram_archive.web.main"
        assert telegram_archive.web.main.__name__ == "telegram_archive.web.main"

        import src.db.base
        import src.realtime
        assert src.db.base is sys.modules["telegram_archive.db.base"]
        assert src.realtime is sys.modules["telegram_archive.realtime"]

        # No module was loaded twice: every src.* entry IS its telegram_archive twin.
        aliases = [name for name in sys.modules if name.startswith("src.")]
        assert aliases, "nothing was imported under the old name"
        for name in aliases:
            assert sys.modules[name] is sys.modules["telegram_archive" + name[3:]], name

        import src
        import telegram_archive
        assert src.__version__ == telegram_archive.__version__

        try:
            import src.no_such_module  # noqa: F401
        except ModuleNotFoundError as exc:
            assert exc.name == "src.no_such_module", exc.name
        else:
            raise AssertionError("an unknown old-name module must not import")
        print("ok")
        """
    )
    result = _run([sys.executable, "-c", script], tmp_path)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-3000:]
    assert result.stderr.count(NOTE) == 1, result.stderr


def test_python_m_src_dotted_module_runs_the_real_code(tmp_path):
    # ``python -m src.setup_auth`` was the documented Windows login command.
    script = "import importlib.util; spec = importlib.util.find_spec('src.config'); print(spec.loader.get_code(spec.name).co_filename)"
    result = _run([sys.executable, "-c", script], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(os.path.join("telegram_archive", "config.py"))


def _entrypoint(tmp_path, *args):
    """Run scripts/entrypoint.sh with a fake ``python`` that only echoes its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python"
    fake.write_text('#!/bin/sh\necho "fake-python $*"\n')
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DB_TYPE": "not-a-database"}
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "entrypoint.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_entrypoint_skips_migrations_for_auth_under_both_names(tmp_path):
    for package in ("telegram_archive", "src"):
        result = _entrypoint(tmp_path, "python", "-m", package, "auth")
        assert result.returncode == 0, (package, result.stderr)
        assert "skipping database migrations" in result.stdout, package
        assert f"fake-python -m {package} auth" in result.stdout


def test_entrypoint_still_migrates_for_other_commands(tmp_path):
    # With an unrecognised DB_TYPE the migration branch refuses to start,
    # which proves the auth shortcut did not swallow a normal command.
    result = _entrypoint(tmp_path, "python", "-m", "telegram_archive", "schedule")
    assert result.returncode == 1
    assert "unrecognised database configuration" in result.stderr
