"""``telegram-archive migrate`` builds the schema from the migrations inside the package.

A pip install has no entrypoint script and no repository checkout, so the
Alembic config, env.py and every revision must be found relative to the
package itself. The real command runs in a fresh interpreter: Alembic's
env.py reconfigures logging, which must not leak into this test process.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.script import ScriptDirectory

from telegram_archive.__main__ import create_parser
from telegram_archive.db.migrations import ALEMBIC_INI, alembic_config

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "telegram_archive"


def _head() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _run(tmp_path, *args, pythonpath=ROOT, extra_env=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "DB_", "DATABASE_"))}
    for key in ("POSTGRES_HOST", "BACKUP_PATH", "SESSION_DIR", "MEDIA_PATH"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(pythonpath)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "telegram_archive", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _migrate(tmp_path, *extra):
    # Deliberately not created: a fresh install has no data directory yet.
    return _run(tmp_path, *extra, "migrate", extra_env={"DB_TYPE": "sqlite", "BACKUP_PATH": str(tmp_path / "backups")})


def _copy_package(project: Path) -> Path:
    """The package alone in ``project``, like a pip install outside this checkout."""
    shutil.copytree(PACKAGE, project / "telegram_archive", ignore=shutil.ignore_patterns("__pycache__"))
    return project


def _version(db_file: Path) -> list[str]:
    with sqlite3.connect(db_file) as conn:
        return [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]


def test_parser_has_migrate_command():
    assert create_parser().parse_args(["migrate"]).command == "migrate"


def test_the_config_uses_the_packaged_migrations():
    assert ALEMBIC_INI == PACKAGE / "alembic.ini"
    script = ScriptDirectory.from_config(alembic_config())
    assert Path(script.dir) == PACKAGE / "alembic"
    on_disk = {path.stem for path in (PACKAGE / "alembic" / "versions").glob("*.py")}
    found = {Path(rev.path).stem for rev in script.walk_revisions()}
    assert found == on_disk and len(found) > 30


def test_migrate_builds_a_fresh_sqlite_database_at_head(tmp_path):
    result = _migrate(tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "Database schema is up to date." in result.stdout

    db_file = tmp_path / "backups" / "telegram_backup.db"
    with sqlite3.connect(db_file) as conn:
        versions = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert versions == [_head()]
    assert {"chats", "messages", "media", "accounts"} <= tables

    # A second run finds nothing to do and still succeeds.
    again = _migrate(tmp_path)
    assert again.returncode == 0, again.stderr[-3000:]


def test_migrate_follows_data_dir(tmp_path):
    result = _migrate(tmp_path, "--data-dir", str(tmp_path / "elsewhere"))
    assert result.returncode == 0, result.stderr[-3000:]
    assert (tmp_path / "elsewhere" / "backups" / "telegram_backup.db").is_file()


def test_migrate_reads_the_same_env_file_as_the_other_commands(tmp_path):
    # The .env is loaded by importing config. migrate used to import only the
    # models, so it ignored the .env and migrated /data/backups instead, while
    # stats and backup built an unversioned schema where the .env pointed.
    project = _copy_package(tmp_path / "project")
    backups = tmp_path / "from-env" / "backups"
    (project / ".env").write_text(f"BACKUP_PATH={backups}\nDB_TYPE=sqlite\n", encoding="utf-8")

    result = _run(tmp_path, "migrate", pythonpath=project)
    assert result.returncode == 0, result.stderr[-3000:]
    assert _version(backups / "telegram_backup.db") == [_head()]

    stats = _run(tmp_path, "stats", pythonpath=project)
    assert stats.returncode == 0, stats.stderr[-3000:]
    assert sorted(path.name for path in backups.glob("*.db")) == ["telegram_backup.db"]
    assert _version(backups / "telegram_backup.db") == [_head()]


def test_migrate_finds_the_revisions_under_a_path_with_a_space(tmp_path):
    # A version_locations line without path_separator is split on spaces, so
    # the revisions went missing and migrate left an empty alembic_version.
    project = _copy_package(tmp_path / "with space")
    backups = tmp_path / "backups"
    result = _run(tmp_path, "migrate", pythonpath=project, extra_env={"DB_TYPE": "sqlite", "BACKUP_PATH": str(backups)})
    assert result.returncode == 0, result.stderr[-3000:]
    assert _version(backups / "telegram_backup.db") == [_head()]
