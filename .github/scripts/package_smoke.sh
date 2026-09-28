#!/usr/bin/env bash
# Install the built wheel into a clean venv and use it the way a pip user would:
# the CLI, "python -m telegram_archive", the Python API import, and
# "telegram-archive migrate" against a throwaway SQLite database.
#
# Usage: .github/scripts/package_smoke.sh dist/telegram_archive-<version>-py3-none-any.whl
set -euo pipefail

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "usage: $0 <wheel>" >&2
  exit 2
fi

WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

uv venv --quiet --python 3.14 "$WORK/venv"
uv pip install --quiet --python "$WORK/venv/bin/python" "$WHEEL"
PY="$WORK/venv/bin/python"
CLI="$WORK/venv/bin/telegram-archive"

# An empty working directory and no PYTHONPATH, so a checkout's
# telegram_archive/ can never stand in for the installed package.
cd "$WORK"
unset PYTHONPATH DATABASE_URL DATABASE_PATH DATABASE_DIR DB_PATH
export DB_TYPE=sqlite
export BACKUP_PATH="$WORK/data/backups"

"$CLI" --help | grep -q "usage: telegram-archive"
"$CLI" --help | grep -q "migrate"
"$PY" -m telegram_archive --help | grep -q "usage: telegram-archive"
echo "CLI and python -m telegram_archive: ok"

"$PY" - <<'EOF'
import inspect
import sys

import telegram_archive
from telegram_archive import Config, TelegramBackup, run_backup

assert "site-packages" in telegram_archive.__file__, telegram_archive.__file__
assert inspect.isclass(Config) and inspect.isclass(TelegramBackup)
assert inspect.iscoroutinefunction(run_backup)
try:
    import src  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    sys.exit("the wheel must not ship the src compatibility package")
print(f"Python API {telegram_archive.__version__}: ok")
EOF

"$CLI" migrate

"$PY" - <<'EOF'
import os
import sqlite3

from alembic.script import ScriptDirectory

from telegram_archive.db.migrations import alembic_config

head = ScriptDirectory.from_config(alembic_config()).get_current_head()
db_file = os.path.join(os.environ["BACKUP_PATH"], "telegram_backup.db")
with sqlite3.connect(db_file) as conn:
    versions = [row[0] for row in conn.execute("SELECT version_num FROM alembic_version")]
assert versions == [head], (versions, head)
print(f"migrate: database at head {head}: ok")
EOF
