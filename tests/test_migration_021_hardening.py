"""Migration 021 against the install shapes that only exist in the field.

The parity gate (tests/test_schema_parity.py) compares freshly built schemas,
so it cannot see what 021 does to a database that arrived in a shape no fresh
build produces. Each test here manufactures one of those shapes and runs the
real migration chain over it:

* a 7.x SQLite archive provisioned by ``create_all`` was stamped past
  migration 007 before models.py declared the ``viewer_audit_log`` indexes,
  so 021 has to create ``idx_audit_log_username``/``idx_audit_log_created``
  itself — on both backends;
* a ``viewer_tokens`` row holding NULL ``is_revoked`` never matched the
  validator's ``is_revoked = 0`` filter, so it was a dead token; the NOT NULL
  backfill must keep it dead (fill 1), not resurrect it;
* a crash during an earlier SQLite table rebuild can strand the batch copy's
  ``_alembic_tmp_<table>`` table (pysqlite autocommits DDL); 021 must clear
  it instead of failing "already exists" on every restart forever;
* a re-run of 021 over an already-aligned schema must change nothing.

Deliberately synchronous throughout: Alembic's env.py calls ``asyncio.run()``,
which cannot be nested inside a running loop.
"""

import asyncio
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = REPO_ROOT / "alembic"

AUDIT_INDEXES = ("idx_audit_log_username", "idx_audit_log_created")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _run_alembic(async_url: str, action: str, target: str) -> None:
    """Run ``alembic <stamp|upgrade> <target>`` against ``async_url``.

    Mirrors tests/test_schema_parity.py: a ``Config`` with no ini file keeps
    env.py from reconfiguring pytest's logging, and env.py reads the URL from
    ``DATABASE_URL``.
    """
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", async_url)

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = async_url
    try:
        if action == "stamp":
            command.stamp(config, target)
        else:
            command.upgrade(config, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _build_seven_x_audit_shape(sync_url: str) -> None:
    """``create_all`` at head models, minus the two audit indexes.

    That is the shape a 7.x ``create_all`` install is really in: the same
    tables, but ViewerAuditLog predates the ``Index()`` declarations, so the
    audit table was born unindexed.
    """
    engine = sa.create_engine(sync_url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            for name in AUDIT_INDEXES:
                conn.execute(sa.text(f"DROP INDEX {name}"))
    finally:
        engine.dispose()


def _audit_index_names(sync_url: str) -> set[str]:
    engine = sa.create_engine(sync_url)
    try:
        return {index["name"] for index in sa.inspect(engine).get_indexes("viewer_audit_log")}
    finally:
        engine.dispose()


def _hash_token(plaintext: str, salt: str) -> str:
    """Exactly the share-token hash the web layer stores (src/web/main.py)."""
    return hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(salt), 600_000).hex()


def _seed_tokens_at_020(sync_url: str) -> tuple[str, str]:
    """Insert a NULL-``is_revoked`` token and a live one; return their plaintexts.

    NULL is unreachable through the app (model and migration 010 both default
    the column to 0), so it is planted the way it happens in the field: a raw
    UPDATE-style edit outside the application.
    """
    plaintexts = (secrets.token_hex(32), secrets.token_hex(32))
    engine = sa.create_engine(sync_url)
    try:
        with engine.begin() as conn:
            for plaintext, revoked in zip(plaintexts, (None, 0), strict=True):
                salt = secrets.token_hex(16)
                conn.execute(
                    sa.text(
                        "INSERT INTO viewer_tokens"
                        " (label, token_hash, token_salt, created_by, allowed_chat_ids, is_revoked)"
                        " VALUES ('synthetic', :token_hash, :salt, 'admin', '[]', :revoked)"
                    ),
                    {"token_hash": _hash_token(plaintext, salt), "salt": salt, "revoked": revoked},
                )
    finally:
        engine.dispose()
    return plaintexts


def _verify_token(async_url: str, plaintext: str) -> dict[str, Any] | None:
    """Run the real validator stack (manager -> adapter) against the database."""

    async def check() -> dict[str, Any] | None:
        manager = DatabaseManager(async_url)
        await manager.init()
        try:
            return await DatabaseAdapter(manager).verify_viewer_token(plaintext)
        finally:
            await manager.close()

    return asyncio.run(check())


def _raw_live_token_count(sync_url: str) -> int:
    """How many tokens the 7.x validator filter (``is_revoked = 0``) can see.

    The premise checks run against a 020-shaped database, where the 8.0
    validator stack cannot execute (its entitlement columns arrive in 022), so
    the pre-upgrade semantics are asserted with the filter's own SQL instead.
    """
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT COUNT(*) FROM viewer_tokens WHERE is_revoked = 0")).scalar_one()
    finally:
        engine.dispose()


def _raw_is_revoked(sync_url: str) -> list[int | None]:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(sa.text("SELECT is_revoked FROM viewer_tokens ORDER BY id")).scalars())
    finally:
        engine.dispose()


def _sqlite_schema(sync_url: str) -> list[tuple[str, str, str | None]]:
    """Every DDL statement in the database, as one comparable snapshot."""
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
            )
            return [tuple(row) for row in rows]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# (a) audit indexes for the create_all-provisioned 7.x population
# ---------------------------------------------------------------------------


def test_021_creates_missing_audit_indexes_on_sqlite(tmp_path):
    """A 7.x create_all archive stamped at 018 must come out of head indexed."""
    db = tmp_path / "archive.db"
    sync_url, async_url = f"sqlite:///{db}", f"sqlite+aiosqlite:///{db}"

    _build_seven_x_audit_shape(sync_url)
    assert _audit_index_names(sync_url).isdisjoint(AUDIT_INDEXES)  # premise: really unindexed

    _run_alembic(async_url, "stamp", "018")
    _run_alembic(async_url, "upgrade", "head")

    assert _audit_index_names(sync_url) >= set(AUDIT_INDEXES)


def test_021_creates_missing_audit_indexes_on_postgresql(require_postgres, make_postgres_database):
    """Same repair on PostgreSQL, and a re-run over its own work stays clean."""
    async_url, sync_url = make_postgres_database("telegram_archive_mig021_idx")

    _build_seven_x_audit_shape(sync_url)
    assert _audit_index_names(sync_url).isdisjoint(AUDIT_INDEXES)

    _run_alembic(async_url, "stamp", "018")
    _run_alembic(async_url, "upgrade", "head")
    assert _audit_index_names(sync_url) >= set(AUDIT_INDEXES)

    _run_alembic(async_url, "stamp", "020")  # make 021 execute a second time
    _run_alembic(async_url, "upgrade", "head")
    assert _audit_index_names(sync_url) >= set(AUDIT_INDEXES)


# ---------------------------------------------------------------------------
# (b) NULL is_revoked must stay dead through the NOT NULL backfill
# ---------------------------------------------------------------------------


def test_null_is_revoked_stays_dead_through_021_on_sqlite(tmp_path):
    db = tmp_path / "archive.db"
    sync_url, async_url = f"sqlite:///{db}", f"sqlite+aiosqlite:///{db}"

    _run_alembic(async_url, "upgrade", "020")  # migration-built: is_revoked still nullable
    tampered, control = _seed_tokens_at_020(sync_url)
    assert _raw_live_token_count(sync_url) == 1  # premise: NULL never matched the filter

    _run_alembic(async_url, "upgrade", "head")

    assert _raw_is_revoked(sync_url) == [1, 0]
    assert _verify_token(async_url, tampered) is None
    assert _verify_token(async_url, control) is not None  # the gate itself still opens


def test_null_is_revoked_stays_dead_through_021_on_postgresql(require_postgres, make_postgres_database):
    async_url, sync_url = make_postgres_database("telegram_archive_mig021_revoked")

    _run_alembic(async_url, "upgrade", "020")
    tampered, control = _seed_tokens_at_020(sync_url)
    assert _raw_live_token_count(sync_url) == 1  # premise: NULL never matched the filter

    _run_alembic(async_url, "upgrade", "head")

    assert _raw_is_revoked(sync_url) == [1, 0]
    assert _verify_token(async_url, tampered) is None
    assert _verify_token(async_url, control) is not None


# ---------------------------------------------------------------------------
# (c) a stranded _alembic_tmp_<table> must not wedge the upgrade
# ---------------------------------------------------------------------------


def test_stranded_batch_tmp_table_does_not_wedge_the_upgrade(tmp_path):
    db = tmp_path / "archive.db"
    sync_url, async_url = f"sqlite:///{db}", f"sqlite+aiosqlite:///{db}"

    engine = sa.create_engine(sync_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    conn = sqlite3.connect(db)
    try:
        # Rebuild media byte-identical except for the FK name: the shape of a
        # manually repaired table, and the one thing that makes 021 rebuild it.
        (media_sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media'").fetchone()
        index_sql = [
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'media' AND sql IS NOT NULL"
            )
        ]
        needle = next(n for n in ('CONSTRAINT "fk_media_message" ', "CONSTRAINT fk_media_message ") if n in media_sql)
        conn.execute(
            # ref is supplied raw: the column is NOT NULL with a Python-side
            # default only, and this fixture writes past the ORM on purpose.
            "INSERT INTO chats (id, title, type, is_archived, is_forum, last_synced_message_id, ref)"
            " VALUES (-1, 'synthetic', 'channel', 0, 0, 0, 'strandtestref000000000')"
        )
        conn.execute(
            "INSERT INTO messages (id, chat_id, date, text, is_outgoing, is_pinned)"
            " VALUES (10, -1, '2026-01-01 00:00:00', 'synthetic', 0, 0)"
        )
        conn.execute(
            "INSERT INTO media (id, message_id, chat_id, type, file_path, downloaded)"
            " VALUES ('file-a', 10, -1, 'photo', '/data/media/a.jpg', 1)"
        )
        conn.execute("ALTER TABLE media RENAME TO media_old")
        conn.execute(media_sql.replace(needle, "", 1))
        conn.execute("INSERT INTO media SELECT * FROM media_old")
        conn.execute("DROP TABLE media_old")
        for statement in index_sql:
            conn.execute(statement)
        # The strand itself: pysqlite autocommits DDL, so a kill right after
        # batch mode's CREATE TABLE leaves exactly this table behind.
        conn.execute("CREATE TABLE _alembic_tmp_media (id TEXT)")
        conn.commit()
    finally:
        conn.close()

    _run_alembic(async_url, "stamp", "020")
    _run_alembic(async_url, "upgrade", "head")  # must clear the strand, not die on it

    conn = sqlite3.connect(db)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "_alembic_tmp_media" not in tables
        (rebuilt_sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media'"
        ).fetchone()
        assert "fk_media_message" in rebuilt_sql  # the rebuild itself still happened
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1  # and kept the data
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (d) re-running 021 over an aligned schema is a no-op
# ---------------------------------------------------------------------------


def test_rerunning_021_over_an_aligned_schema_changes_nothing(tmp_path):
    db = tmp_path / "archive.db"
    sync_url, async_url = f"sqlite:///{db}", f"sqlite+aiosqlite:///{db}"

    _build_seven_x_audit_shape(sync_url)
    _run_alembic(async_url, "stamp", "018")
    _run_alembic(async_url, "upgrade", "head")
    aligned = _sqlite_schema(sync_url)
    assert aligned  # premise: the snapshot really captured a schema

    _run_alembic(async_url, "stamp", "020")  # make 021 execute a second time
    _run_alembic(async_url, "upgrade", "head")

    assert _sqlite_schema(sync_url) == aligned
