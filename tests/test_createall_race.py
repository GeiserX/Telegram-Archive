"""The viewer must never build tables into a database Alembic owns.

The hazard
----------
``Dockerfile.viewer`` ships no ``alembic/`` and sets no ENTRYPOINT — it runs
uvicorn directly — so the viewer container can never migrate. It does, however,
reach ``Base.metadata.create_all(checkfirst=True)`` in ``src/db/base.py`` on
every SQLite start, and ``docker-compose.yml`` starts it concurrently with the
backup container, which *is* running ``alembic upgrade head`` on the same file.

Unguarded, ``checkfirst=True`` adds whole missing tables. Land one of those
inside a database a migration is halfway through rebuilding and the migration's
own ``CREATE TABLE`` dies with "table already exists"; the backup container then
crash-loops against what is usually the only copy of someone's Telegram history.

What is asserted
----------------
An existing database gains **nothing**: not a table, not an index, not a
trigger. ``sqlite_master`` is snapshotted before and after a real
``DatabaseManager.init()`` and must come back byte-identical.

Why the fixtures drop tables
----------------------------
A 7.x file is, to a newer viewer, exactly "a database Alembic owns that is
missing tables the current ORM declares". Building that shape by dropping real
tables reproduces it without pinning the test to whichever tables the next
migration happens to add, and — unlike a same-version database, where
``create_all`` is a no-op anyway — it gives the assertion something real to
catch. ``test_removing_the_guard_puts_the_tables_back`` is the positive control:
it defeats the guard and watches those same assertions go red.

Which of these can go red
-------------------------
Deleting the guard line from ``src/db/base.py`` fails
``test_alembic_owned_database_gains_nothing``,
``test_pre_alembic_database_gains_nothing``,
``test_the_viewers_own_startup_path_creates_nothing`` and both
``test_repeated_starts_stay_a_no_op`` cases — measured, 5 failures. The other
three are meant to survive it: the two fresh-database tests assert the fallback
still works, which the guard does not change, and the alembic_version test
guards a second invariant (nothing here ever stamps) that ``create_all`` was
never going to break on its own.
"""

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from src.db.base import DatabaseManager, close_database, init_database
from src.db.models import Base

# Stand-ins for "a table the newer ORM declares and this file does not have".
# Both are leaf tables (nothing references them), and both are asserted to
# still exist in models.py before use, so a rename fails loudly here instead of
# quietly turning this file into a test of nothing.
MISSING_IN_OLD_DB = ("push_subscriptions", "viewer_sessions")

# A revision well below head: the point is only that alembic_version is present
# and untouched, never which revision it names.
OLD_REVISION = "018"


def snapshot_schema(db_path: Path) -> list[tuple[str, str, str | None]]:
    """Every schema object SQLite knows about, read without SQLAlchemy.

    Indexes and triggers are included deliberately: create_all makes those too,
    and a gate that only counted tables would miss them.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def table_names(db_path: Path) -> set[str]:
    """Just the table names, for readable assertions."""
    return {name for kind, name, _ in snapshot_schema(db_path) if kind == "table"}


def build_old_database(db_path: Path, *, stamped: bool) -> None:
    """Write a database that predates the current ORM.

    ``stamped=True`` gives it an ``alembic_version`` row — every database a
    recent release has ever started. ``stamped=False`` is the pre-Alembic shape
    that ``scripts/entrypoint.sh`` stamps by inspecting the schema, which is the
    read this guard must not disturb.
    """
    for table in MISSING_IN_OLD_DB:
        assert table in Base.metadata.tables, f"{table} is no longer an ORM table — pick another stand-in"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            for table in MISSING_IN_OLD_DB:
                conn.execute(sa.text(f"DROP TABLE {table}"))
            if stamped:
                conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": OLD_REVISION})
    finally:
        engine.dispose()

    present = table_names(db_path)
    assert not (present & set(MISSING_IN_OLD_DB)), "fixture failed to remove the newer tables"
    assert ("alembic_version" in present) is stamped


async def open_with_manager(db_path: Path) -> None:
    """Do exactly what the viewer does on startup, and nothing else."""
    manager = DatabaseManager(f"sqlite+aiosqlite:///{db_path}")
    await manager.init()
    await manager.close()


async def test_alembic_owned_database_gains_nothing(tmp_path: Path) -> None:
    """The gate: an 8.0 viewer against a 7.x file creates zero tables."""
    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=True)

    before = snapshot_schema(db_path)
    await open_with_manager(db_path)
    after = snapshot_schema(db_path)

    assert after == before
    assert not (table_names(db_path) & set(MISSING_IN_OLD_DB))


async def test_alembic_version_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    """The viewer must not stamp, re-stamp or clear the revision either."""
    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=True)

    await open_with_manager(db_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(OLD_REVISION,)]
    finally:
        conn.close()


async def test_pre_alembic_database_gains_nothing(tmp_path: Path) -> None:
    """A database with tables but no alembic_version is still not ours.

    The entrypoint stamps this shape by inspecting which schema objects exist.
    A table appearing between that inspection and the upgrade gets it stamped at
    the wrong revision — the same crash-loop, reached a different way.
    """
    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=False)

    before = snapshot_schema(db_path)
    await open_with_manager(db_path)
    after = snapshot_schema(db_path)

    assert after == before


async def test_the_viewers_own_startup_path_creates_nothing(tmp_path: Path) -> None:
    """Same assertion through ``init_database()``, which is what the app calls.

    ``src/web/main.py`` builds its manager from the environment via
    ``init_database()``; testing only ``DatabaseManager`` would leave the real
    entry point unproven.
    """
    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=True)

    before = snapshot_schema(db_path)
    with patch.dict(os.environ, {"DB_PATH": str(db_path), "DATABASE_URL": "", "DB_TYPE": "sqlite"}):
        try:
            await init_database()
        finally:
            await close_database()

    assert snapshot_schema(db_path) == before


async def test_a_genuinely_fresh_database_is_still_built(tmp_path: Path) -> None:
    """The reverse check: the fallback this guard narrows must still work.

    A fresh install with no entrypoint — the viewer image, or ``python -m src``
    — has to be able to provision an empty file, and the whole ORM schema has to
    land, not part of it.
    """
    db_path = tmp_path / "archive.db"

    await open_with_manager(db_path)

    assert table_names(db_path) == set(Base.metadata.tables)


async def test_an_empty_file_that_already_exists_is_still_built(tmp_path: Path) -> None:
    """A zero-byte file is fresh too — an empty mount must not read as owned."""
    db_path = tmp_path / "archive.db"
    db_path.touch()
    assert table_names(db_path) == set()

    await open_with_manager(db_path)

    assert table_names(db_path) == set(Base.metadata.tables)


async def test_removing_the_guard_puts_the_tables_back(tmp_path: Path) -> None:
    """POSITIVE CONTROL: with the guard gone, the assertions above go red.

    This is the pre-fix body of ``_create_schema_if_absent``. If this test ever
    passes *and* the tests above also pass, the guard has stopped being what
    protects the database and something else is masking the bug.
    """

    def unguarded(sync_conn: sa.Connection) -> bool:
        Base.metadata.create_all(sync_conn, checkfirst=True)
        return True

    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=True)
    before = snapshot_schema(db_path)

    with patch.object(DatabaseManager, "_create_schema_if_absent", staticmethod(unguarded)):
        await open_with_manager(db_path)

    after = snapshot_schema(db_path)
    assert after != before, "the guard was not what stopped create_all — this file proves nothing"
    assert set(MISSING_IN_OLD_DB) <= table_names(db_path)


@pytest.mark.parametrize("stamped", [True, False])
async def test_repeated_starts_stay_a_no_op(tmp_path: Path, stamped: bool) -> None:
    """Restart loops must not accumulate anything either."""
    db_path = tmp_path / "archive.db"
    build_old_database(db_path, stamped=stamped)

    before = snapshot_schema(db_path)
    for _ in range(3):
        await open_with_manager(db_path)

    assert snapshot_schema(db_path) == before
