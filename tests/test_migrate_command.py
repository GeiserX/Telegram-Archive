"""``telegram-archive migrate`` builds the schema from the migrations inside the package.

A pip install has no entrypoint script and no repository checkout, so the
Alembic config, env.py and every revision must be found relative to the
package itself. The real command runs in a fresh interpreter: Alembic's
env.py reconfigures logging, which must not leak into this test process.
"""

import os
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


def _migrate(tmp_path, *extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "DB_", "DATABASE_"))}
    env.pop("POSTGRES_HOST", None)
    env["PYTHONPATH"] = str(ROOT)
    env["DB_TYPE"] = "sqlite"
    # Deliberately not created: a fresh install has no data directory yet.
    env["BACKUP_PATH"] = str(tmp_path / "backups")
    return subprocess.run(
        [sys.executable, "-m", "telegram_archive", *extra, "migrate"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


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
