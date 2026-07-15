"""Tests for the SQLite pre-Alembic stamping logic embedded in scripts/entrypoint.sh.

The stamping logic runs inside a bash-double-quoted `python -c "..."` heredoc, so
embedded double quotes are escaped as `\\"`. These tests extract the real block
straight out of the shipped script, de-escape it, and exec() it against real
sqlite3 databases seeded to look like historical schema shapes -- so a wrong
boolean in the has_0XX ladder fails the test, not just a missing substring.

The extraction deliberately stops at the `conn.close()` that follows the
stamping block, excluding the trailing `Config(...)/command.upgrade(...)` tail.
That tail invokes real Alembic against a hardcoded `/app/alembic.ini` Docker
path that doesn't exist in this environment and is orthogonal to the
detection/stamping logic under test here.
"""

import sqlite3
from pathlib import Path

ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"


def _extract_sqlite_stamping_block(script: str) -> str:
    """Slice the pure schema-detection/stamping logic out of the SQLite python heredoc."""
    start_marker = "database_url = os.getenv('DATABASE_URL', '')"
    end_marker = "conn.close()"
    start = script.index(start_marker)
    end = script.index(end_marker, start) + len(end_marker)
    block = script[start:end]
    return block.replace('\\"', '"')


_ENTRYPOINT_SOURCE = ENTRYPOINT_PATH.read_text(encoding="utf-8")
SQLITE_STAMPING_SOURCE = _extract_sqlite_stamping_block(_ENTRYPOINT_SOURCE)


def _run_stamping(db_path: Path) -> dict:
    """Execute the real extracted stamping logic against a seeded sqlite file.

    Sets DB_PATH the same way the shipped script's os.getenv() fallback chain
    reads it, runs the extracted source, and returns the exec() globals so
    tests can inspect `stamp_version` (only set if the stamping branch ran).
    """
    import os

    exec_globals = {"os": os, "sqlite3": sqlite3}
    saved_database_url = os.environ.get("DATABASE_URL")
    saved_db_path = os.environ.get("DB_PATH")
    os.environ.pop("DATABASE_URL", None)
    os.environ["DB_PATH"] = str(db_path)
    try:
        exec(compile(SQLITE_STAMPING_SOURCE, str(ENTRYPOINT_PATH), "exec"), exec_globals)
    finally:
        if saved_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved_database_url
        if saved_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = saved_db_path
    return exec_globals


def _seed_pre_alembic_db(
    db_path: Path,
    *,
    omit_017_index: bool = False,
    omit_016_download_attempts: bool = False,
    omit_015_message_versions: bool = False,
    omit_014_soft_delete: bool = False,
    include_push_subscriptions: bool = False,
) -> None:
    """Seed a SQLite file with the specific artifacts entrypoint.sh's has_0XX checks inspect.

    This mirrors what Base.metadata.create_all() leaves behind at various points
    along the migration ladder -- not the full ORM schema, just the tables,
    columns, and indexes the stamping logic actually queries for.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY)")

    messages_cols = ["id INTEGER PRIMARY KEY", "chat_id INTEGER"]
    if not omit_014_soft_delete:
        messages_cols += ["is_deleted BOOLEAN", "deleted_at TEXT"]
    conn.execute(f"CREATE TABLE messages ({', '.join(messages_cols)})")

    if not omit_017_index:
        conn.execute("CREATE INDEX idx_messages_chat_id_id ON messages (chat_id, id)")

    if not omit_015_message_versions:
        conn.execute("CREATE TABLE message_versions (id INTEGER PRIMARY KEY)")

    # file_path must exist whenever the media table exists: the has_013_paths
    # check unconditionally queries it once has_media_table is True.
    media_cols = ["id INTEGER PRIMARY KEY", "chat_id INTEGER", "file_path TEXT"]
    if not omit_016_download_attempts:
        media_cols.append("download_attempts INTEGER")
    conn.execute(f"CREATE TABLE media ({', '.join(media_cols)})")

    if include_push_subscriptions:
        conn.execute("CREATE TABLE push_subscriptions (id INTEGER PRIMARY KEY)")

    conn.commit()
    conn.close()


def _stamped_version(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    conn.close()
    return row[0] if row else None


def test_fully_modern_schema_stamps_017(tmp_path):
    """create_all() shape: soft-delete cols + message_versions + download_attempts + 017 index."""
    db_path = tmp_path / "modern.db"
    _seed_pre_alembic_db(db_path)

    result = _run_stamping(db_path)

    assert result["stamp_version"] == "017"
    assert _stamped_version(db_path) == "017"


def test_missing_017_index_stamps_016(tmp_path):
    """Same as fully-modern minus the idx_messages_chat_id_id index."""
    db_path = tmp_path / "no_017_index.db"
    _seed_pre_alembic_db(db_path, omit_017_index=True)

    result = _run_stamping(db_path)

    assert result["stamp_version"] == "016"
    assert _stamped_version(db_path) == "016"


def test_missing_download_attempts_stamps_015(tmp_path):
    """Same as fully-modern minus media.download_attempts."""
    db_path = tmp_path / "no_016_download_attempts.db"
    _seed_pre_alembic_db(db_path, omit_016_download_attempts=True)

    result = _run_stamping(db_path)

    assert result["stamp_version"] == "015"
    assert _stamped_version(db_path) == "015"


def test_missing_message_versions_stamps_014(tmp_path):
    """Same as fully-modern minus the message_versions table."""
    db_path = tmp_path / "no_015_message_versions.db"
    _seed_pre_alembic_db(db_path, omit_015_message_versions=True)

    result = _run_stamping(db_path)

    assert result["stamp_version"] == "014"
    assert _stamped_version(db_path) == "014"


def test_pre_010_shape_stamps_003(tmp_path):
    """Old database: push_subscriptions table exists but messages.is_pinned does not.

    None of the 014-017 artifacts are present, so the ladder falls all the way
    down to has_push_subs (migration 003), the last has_0XX rung above the
    unconditional '002' fallback.
    """
    db_path = tmp_path / "pre_010.db"
    _seed_pre_alembic_db(
        db_path,
        omit_017_index=True,
        omit_016_download_attempts=True,
        omit_015_message_versions=True,
        omit_014_soft_delete=True,
        include_push_subscriptions=True,
    )

    result = _run_stamping(db_path)

    assert result["stamp_version"] == "003"
    assert _stamped_version(db_path) == "003"


def test_existing_alembic_version_skips_stamping(tmp_path):
    """When alembic_version already exists, the stamping branch never runs at all."""
    db_path = tmp_path / "already_stamped.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE alembic_version ("
        "version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    )
    conn.execute("INSERT INTO alembic_version (version_num) VALUES ('005')")
    conn.commit()
    conn.close()

    result = _run_stamping(db_path)

    assert "stamp_version" not in result
    assert _stamped_version(db_path) == "005"


# ============================================================================
# PostgreSQL ladder: substring-only checks
# ============================================================================
#
# The PostgreSQL stamping branch can't be executed here -- it needs a live
# PostgreSQL server plus psycopg2 connectivity, unlike the SQLite branch above
# which now runs for real against seeded files. These keep light textual
# coverage so the two branches don't silently drift apart.


def test_postgres_branch_checks_message_versions_table():
    assert "has_015_message_versions" in _ENTRYPOINT_SOURCE
    assert "if has_015_message_versions and has_014_soft_delete:" in _ENTRYPOINT_SOURCE


def test_postgres_branch_checks_017_index_via_pg_indexes():
    assert "idx_messages_chat_id_id" in _ENTRYPOINT_SOURCE
    assert "FROM pg_indexes" in _ENTRYPOINT_SOURCE


def test_postgres_branch_does_not_use_old_table_name():
    old_table_name = "message_" + "edit_history"
    assert old_table_name not in _ENTRYPOINT_SOURCE
