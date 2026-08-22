"""Tests for Alembic migrations 007-013 idempotency guards.

These are the migrations CLAUDE.md's crash-loop warning is about: a database
provisioned via ``Base.metadata.create_all()`` and stamped below the revision
already has every object the migration creates, so each ``sa.inspect`` guard is
load-bearing — removing one turns ``alembic upgrade head`` into a crash-loop on
every existing deployment. Until this file, none of 007-013 was executed by any
test (010/013 appeared only in stamping-string assertions), so a broken guard
shipped green.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _load_migration(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _VERSIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _columns(conn, table):
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _indexes(conn, table):
    return {ix["name"] for ix in sa.inspect(conn).get_indexes(table)}


def _tables(conn):
    return set(sa.inspect(conn).get_table_names())


# ============================================================================
# Migration 007 - viewer_accounts + viewer_audit_log
# ============================================================================

migration_007 = _load_migration("20260227_007_add_viewer_accounts.py", "migration_007")


def test_007_revision_chain():
    assert migration_007.revision == "007"
    assert migration_007.down_revision == "006"


def test_007_upgrade_creates_tables_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_007.upgrade)
        assert "viewer_accounts" in _tables(conn)
        assert "viewer_audit_log" in _tables(conn)
        assert "idx_audit_log_username" in _indexes(conn, "viewer_audit_log")
        assert "idx_audit_log_created" in _indexes(conn, "viewer_audit_log")

        _run(conn, migration_007.upgrade)  # re-run must be a no-op
        assert "viewer_accounts" in _tables(conn)


def test_007_upgrade_noop_when_tables_already_exist():
    """Simulates create_all(): both tables and both indexes already declared."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE viewer_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username VARCHAR(255) NOT NULL UNIQUE, password_hash VARCHAR(128) NOT NULL, "
                "salt VARCHAR(64) NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE viewer_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, "
                "action VARCHAR(100) NOT NULL, created_at DATETIME)"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_audit_log_username ON viewer_audit_log (username)"))
        conn.execute(sa.text("CREATE INDEX idx_audit_log_created ON viewer_audit_log (created_at)"))

        _run(conn, migration_007.upgrade)  # must not raise
        assert "idx_audit_log_username" in _indexes(conn, "viewer_audit_log")


def test_007_upgrade_backfills_indexes_on_bare_audit_table():
    """Audit table exists (create_all from an older model) but its indexes don't."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE viewer_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, "
                "action VARCHAR(100) NOT NULL, created_at DATETIME)"
            )
        )

        _run(conn, migration_007.upgrade)
        assert "idx_audit_log_username" in _indexes(conn, "viewer_audit_log")
        assert "idx_audit_log_created" in _indexes(conn, "viewer_audit_log")


def test_007_downgrade_drops_everything():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_007.upgrade)

        _run(conn, migration_007.downgrade)
        assert "viewer_accounts" not in _tables(conn)
        assert "viewer_audit_log" not in _tables(conn)


# ============================================================================
# Migration 008 - push_subscriptions.username / allowed_chat_ids
# ============================================================================

migration_008 = _load_migration("20260227_008_add_push_subscription_user.py", "migration_008")


def _create_pre008_push_subscriptions(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE push_subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "endpoint TEXT NOT NULL UNIQUE, p256dh VARCHAR(255) NOT NULL, "
            "auth VARCHAR(255) NOT NULL, chat_id BIGINT)"
        )
    )


def test_008_revision_chain():
    assert migration_008.revision == "008"
    assert migration_008.down_revision == "007"


def test_008_upgrade_adds_columns_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre008_push_subscriptions(conn)

        _run(conn, migration_008.upgrade)
        assert "username" in _columns(conn, "push_subscriptions")
        assert "allowed_chat_ids" in _columns(conn, "push_subscriptions")
        assert "idx_push_sub_username" in _indexes(conn, "push_subscriptions")

        _run(conn, migration_008.upgrade)  # re-run must be a no-op
        assert "username" in _columns(conn, "push_subscriptions")


def test_008_upgrade_noop_when_columns_already_exist():
    """Simulates create_all(): model already declares both columns and the index."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE push_subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "endpoint TEXT NOT NULL UNIQUE, p256dh VARCHAR(255) NOT NULL, "
                "auth VARCHAR(255) NOT NULL, chat_id BIGINT, "
                "username VARCHAR(255), allowed_chat_ids TEXT)"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_push_sub_username ON push_subscriptions (username)"))

        _run(conn, migration_008.upgrade)  # must not raise
        assert "idx_push_sub_username" in _indexes(conn, "push_subscriptions")


def test_008_downgrade_removes_columns():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre008_push_subscriptions(conn)
        _run(conn, migration_008.upgrade)

        _run(conn, migration_008.downgrade)
        assert "username" not in _columns(conn, "push_subscriptions")
        assert "allowed_chat_ids" not in _columns(conn, "push_subscriptions")


# ============================================================================
# Migration 009 - viewer_sessions
# ============================================================================

migration_009 = _load_migration("20260305_009_add_viewer_sessions.py", "migration_009")


def _create_all_style_viewer_sessions(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE viewer_sessions (token VARCHAR(64) NOT NULL PRIMARY KEY, "
            "username VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, "
            "allowed_chat_ids TEXT, created_at FLOAT NOT NULL, last_accessed FLOAT NOT NULL)"
        )
    )


def test_009_revision_chain():
    assert migration_009.revision == "009"
    assert migration_009.down_revision == "008"


def test_009_upgrade_creates_table_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_009.upgrade)
        assert "viewer_sessions" in _tables(conn)
        assert "idx_viewer_sessions_username" in _indexes(conn, "viewer_sessions")
        assert "idx_viewer_sessions_created_at" in _indexes(conn, "viewer_sessions")

        _run(conn, migration_009.upgrade)  # re-run must be a no-op
        assert "viewer_sessions" in _tables(conn)


def test_009_upgrade_backfills_indexes_when_table_exists_bare():
    """The else-branch: create_all() made the table, but not the migration's indexes."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_all_style_viewer_sessions(conn)

        _run(conn, migration_009.upgrade)
        assert "idx_viewer_sessions_username" in _indexes(conn, "viewer_sessions")
        assert "idx_viewer_sessions_created_at" in _indexes(conn, "viewer_sessions")


def test_009_upgrade_noop_when_table_and_indexes_exist():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_all_style_viewer_sessions(conn)
        conn.execute(sa.text("CREATE INDEX idx_viewer_sessions_username ON viewer_sessions (username)"))
        conn.execute(sa.text("CREATE INDEX idx_viewer_sessions_created_at ON viewer_sessions (created_at)"))

        _run(conn, migration_009.upgrade)  # must not raise
        assert "viewer_sessions" in _tables(conn)


def test_009_downgrade_drops_table():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_009.upgrade)

        _run(conn, migration_009.downgrade)
        assert "viewer_sessions" not in _tables(conn)


# ============================================================================
# Migration 010 - viewer_tokens, app_settings, no_download columns
# ============================================================================

migration_010 = _load_migration("20260310_010_add_tokens_settings_no_download.py", "migration_010")


def _create_pre010_schema(conn):
    """Chain-realistic state: 007 and 009 have run (fresh path)."""
    _run(conn, migration_007.upgrade)
    _run(conn, migration_009.upgrade)


def test_010_revision_chain():
    assert migration_010.revision == "010"
    assert migration_010.down_revision == "009"


def test_010_upgrade_creates_everything_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre010_schema(conn)

        _run(conn, migration_010.upgrade)
        assert "viewer_tokens" in _tables(conn)
        assert "app_settings" in _tables(conn)
        assert "idx_viewer_tokens_created_by" in _indexes(conn, "viewer_tokens")
        assert "idx_viewer_tokens_is_revoked" in _indexes(conn, "viewer_tokens")
        assert "no_download" in _columns(conn, "viewer_accounts")
        assert "no_download" in _columns(conn, "viewer_sessions")
        assert "source_token_id" in _columns(conn, "viewer_sessions")
        assert "idx_viewer_sessions_source_token" in _indexes(conn, "viewer_sessions")

        _run(conn, migration_010.upgrade)  # re-run must be a no-op
        assert "viewer_tokens" in _tables(conn)


def test_010_upgrade_backfills_token_indexes_on_bare_table():
    """viewer_tokens exists from create_all() but without the migration's indexes."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre010_schema(conn)
        conn.execute(
            sa.text(
                "CREATE TABLE viewer_tokens (id INTEGER PRIMARY KEY, token_hash VARCHAR(128) NOT NULL, "
                "token_salt VARCHAR(64) NOT NULL, created_by VARCHAR(255) NOT NULL, "
                "allowed_chat_ids TEXT NOT NULL, is_revoked INTEGER DEFAULT 0)"
            )
        )

        _run(conn, migration_010.upgrade)
        assert "idx_viewer_tokens_created_by" in _indexes(conn, "viewer_tokens")
        assert "idx_viewer_tokens_is_revoked" in _indexes(conn, "viewer_tokens")


def test_010_upgrade_skips_viewer_sessions_when_absent():
    """The viewer_sessions block is guarded: only viewer_accounts is required."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_007.upgrade)  # viewer_accounts only, no viewer_sessions

        _run(conn, migration_010.upgrade)  # must not raise
        assert "no_download" in _columns(conn, "viewer_accounts")
        assert "viewer_sessions" not in _tables(conn)


def test_010_downgrade_removes_everything():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre010_schema(conn)
        _run(conn, migration_010.upgrade)

        _run(conn, migration_010.downgrade)
        assert "viewer_tokens" not in _tables(conn)
        assert "app_settings" not in _tables(conn)
        assert "no_download" not in _columns(conn, "viewer_accounts")
        assert "no_download" not in _columns(conn, "viewer_sessions")


# ============================================================================
# Migration 011 - media.content_hash
# ============================================================================

migration_011 = _load_migration("20260503_011_add_media_content_hash.py", "migration_011")


def _create_pre011_media(conn):
    conn.execute(
        sa.text(
            "CREATE TABLE media (id VARCHAR(255) NOT NULL PRIMARY KEY, chat_id BIGINT, "
            "type VARCHAR(50), file_path VARCHAR(500), downloaded INTEGER DEFAULT 0)"
        )
    )


def test_011_revision_chain():
    assert migration_011.revision == "011"
    assert migration_011.down_revision == "010"


def test_011_upgrade_adds_column_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)

        _run(conn, migration_011.upgrade)
        assert "content_hash" in _columns(conn, "media")
        assert "idx_media_content_hash" in _indexes(conn, "media")

        _run(conn, migration_011.upgrade)  # re-run must be a no-op
        assert "content_hash" in _columns(conn, "media")


def test_011_upgrade_noop_when_column_and_index_exist():
    """Simulates create_all(): Media model already declares content_hash + index."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE media (id VARCHAR(255) NOT NULL PRIMARY KEY, chat_id BIGINT, "
                "type VARCHAR(50), content_hash VARCHAR(64))"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_media_content_hash ON media (content_hash)"))

        _run(conn, migration_011.upgrade)  # must not raise
        assert "content_hash" in _columns(conn, "media")


def test_011_downgrade_removes_column():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)
        _run(conn, migration_011.upgrade)

        _run(conn, migration_011.downgrade)
        assert "content_hash" not in _columns(conn, "media")


# ============================================================================
# Migration 012 - idx_media_chat_type (guarded both directions)
# ============================================================================

migration_012 = _load_migration("20260523_012_add_media_chat_type_index.py", "migration_012")


def test_012_revision_chain():
    assert migration_012.revision == "012"
    assert migration_012.down_revision == "011"


def test_012_upgrade_creates_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)

        _run(conn, migration_012.upgrade)
        assert "idx_media_chat_type" in _indexes(conn, "media")

        _run(conn, migration_012.upgrade)  # re-run must be a no-op
        assert "idx_media_chat_type" in _indexes(conn, "media")


def test_012_upgrade_noop_when_index_exists():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)
        conn.execute(sa.text("CREATE INDEX idx_media_chat_type ON media (chat_id, type)"))

        _run(conn, migration_012.upgrade)  # must not raise
        assert "idx_media_chat_type" in _indexes(conn, "media")


def test_012_downgrade_drops_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)
        _run(conn, migration_012.upgrade)

        _run(conn, migration_012.downgrade)
        assert "idx_media_chat_type" not in _indexes(conn, "media")

        _run(conn, migration_012.downgrade)  # guarded: re-run must be a no-op
        assert "idx_media_chat_type" not in _indexes(conn, "media")


# ============================================================================
# Migration 013 - rewrite stale positive-ID folder segments in file_path
# ============================================================================

migration_013 = _load_migration("20260524_013_fix_media_file_paths.py", "migration_013")


def _seed_media_for_013(conn):
    _create_pre011_media(conn)
    rows = [
        # Channel: marked -1000000000123, pre-v4.0.5 folder used raw "123".
        ("m1", -1000000000123, "/data/media/123/photo.jpg"),
        # Basic group: marked -456, stale folder "456".
        ("m2", -456, "/data/media/456/doc.pdf"),
        # Already-correct marked folder: must stay untouched.
        ("m3", -1000000000123, "/data/media/-1000000000123/video.mp4"),
        # User (positive id): never mismatched, untouched.
        ("m4", 789, "/data/media/789/voice.ogg"),
        # NULL file_path: filtered out by the query.
        ("m5", -456, None),
    ]
    for mid, cid, path in rows:
        conn.execute(
            sa.text("INSERT INTO media (id, chat_id, file_path) VALUES (:i, :c, :p)"),
            {"i": mid, "c": cid, "p": path},
        )


def _file_path(conn, media_id):
    return conn.execute(sa.text("SELECT file_path FROM media WHERE id = :i"), {"i": media_id}).scalar()


def test_013_revision_chain():
    assert migration_013.revision == "013"
    assert migration_013.down_revision == "012"


def test_013_derive_stale_folder():
    assert migration_013._derive_stale_folder(-1000000000123) == "123"
    assert migration_013._derive_stale_folder(-456) == "456"
    assert migration_013._derive_stale_folder(789) is None


def test_013_upgrade_rewrites_stale_folders_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _seed_media_for_013(conn)

        _run(conn, migration_013.upgrade)
        assert _file_path(conn, "m1") == "/data/media/-1000000000123/photo.jpg"
        assert _file_path(conn, "m2") == "/data/media/-456/doc.pdf"
        assert _file_path(conn, "m3") == "/data/media/-1000000000123/video.mp4"
        assert _file_path(conn, "m4") == "/data/media/789/voice.ogg"
        assert _file_path(conn, "m5") is None

        _run(conn, migration_013.upgrade)  # re-run: stale patterns no longer match
        assert _file_path(conn, "m1") == "/data/media/-1000000000123/photo.jpg"
        assert _file_path(conn, "m2") == "/data/media/-456/doc.pdf"


def test_013_upgrade_leaves_other_chats_rows_alone():
    """The rewrite is scoped by chat_id: a same-named folder under another chat stays."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre011_media(conn)
        conn.execute(sa.text("INSERT INTO media (id, chat_id, file_path) VALUES ('a', -456, '/data/media/456/x.jpg')"))
        # Different chat whose path happens to contain /456/ — must not be rewritten.
        conn.execute(sa.text("INSERT INTO media (id, chat_id, file_path) VALUES ('b', -999, '/data/media/456/y.jpg')"))

        _run(conn, migration_013.upgrade)
        assert _file_path(conn, "a") == "/data/media/-456/x.jpg"
        assert _file_path(conn, "b") == "/data/media/456/y.jpg"


def test_013_downgrade_restores_positive_folders():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _seed_media_for_013(conn)
        _run(conn, migration_013.upgrade)

        _run(conn, migration_013.downgrade)
        assert _file_path(conn, "m1") == "/data/media/123/photo.jpg"
        assert _file_path(conn, "m2") == "/data/media/456/doc.pdf"
        # LOSSY by design: m3 was born with the marked folder (never migrated),
        # but the downgrade cannot tell it apart from a row 013 rewrote — both
        # collapse to the raw-id folder. Pinned so the rollback's real
        # behavior is documented rather than assumed.
        assert _file_path(conn, "m3") == "/data/media/123/video.mp4"
        assert _file_path(conn, "m4") == "/data/media/789/voice.ogg"
