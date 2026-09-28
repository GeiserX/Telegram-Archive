"""The Alembic migrations that ship inside the package.

``alembic.ini``, ``env.py`` and every revision live in ``telegram_archive/``,
so a pip install carries them. ``env.py`` reads the database from the same
environment variables as the app (``DATABASE_URL``, ``DB_TYPE``, ``DB_PATH``
and the rest), so the config needs no URL.

Alembic is imported inside the functions: the viewer image copies this
package without Alembic installed.
"""

from pathlib import Path

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def alembic_config():
    """An Alembic ``Config`` pointing at the bundled migrations."""
    from alembic.config import Config

    return Config(str(ALEMBIC_INI))


def upgrade_to_head() -> None:
    """Run every migration the database has not seen yet (``alembic upgrade head``)."""
    from alembic import command

    command.upgrade(alembic_config(), "head")
