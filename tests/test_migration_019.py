"""Tests for Alembic migration 019 (delete phantom chat-action message rows)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "20260719_019_delete_phantom_chat_action_rows.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_019", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(conn, func):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        func()


def _create_schema(conn):
    """Minimal schema mirroring the real (message_id, chat_id) child columns."""
    conn.execute(
        sa.text(
            "CREATE TABLE messages ("
            "id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "date DATETIME, "
            "text TEXT, "
            "raw_data TEXT, "
            "PRIMARY KEY (id, chat_id)"
            ")"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE reactions ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "message_id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "emoji VARCHAR(50)"
            ")"
        )
    )
    conn.execute(
        sa.text("CREATE TABLE media (id VARCHAR(255) NOT NULL PRIMARY KEY, message_id BIGINT, chat_id BIGINT)")
    )
    conn.execute(
        sa.text(
            "CREATE TABLE message_versions ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "message_id BIGINT NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "change_hash VARCHAR(64)"
            ")"
        )
    )
    conn.execute(sa.text('CREATE TABLE metadata ("key" VARCHAR(255) NOT NULL PRIMARY KEY, value TEXT)'))


def _insert_message(conn, msg_id, raw_data, *, chat_id=-100, text=""):
    conn.execute(
        sa.text("INSERT INTO messages (id, chat_id, date, text, raw_data) VALUES (:id, :cid, :d, :t, :r)"),
        {"id": msg_id, "cid": chat_id, "d": "2026-07-19 00:00:00", "t": text, "r": raw_data},
    )


def _message_ids(conn):
    return {row[0] for row in conn.execute(sa.text("SELECT id FROM messages")).fetchall()}


# Phantom rows: every one of the 7 legacy names covered at least once, across
# spaced/compact JSON forms, with/without a service_type key, and high/low ids.
# id is the surrogate the caller inserts; chat_id defaults to -100.
_PHANTOM_ROWS = {
    # id: raw_data  (comment: name / form / service_type? / id band)
    5_000_000_001: '{"service_type": "service", "action_type": "user_joined"}',  # spaced / with / high
    5_000_000_002: '{"service_type":"service","action_type":"user_left"}',  # compact / with / high
    42: '{"action_type": "user_added"}',  # spaced / without / LOW
    5_000_000_003: '{"action_type":"user_kicked"}',  # compact / without / high
    7: '{"service_type": "service", "action_type": "photo_changed"}',  # spaced / with / LOW
    5_000_000_004: '{"action_type":"photo_removed"}',  # compact / without / high
    5_000_000_005: '{"service_type": "service", "action_type": "title_changed", "new_title": "Room"}',  # spaced+extra
}

# Rows that MUST be preserved.
_KEPT_ROWS = {
    1: (None, ""),  # normal message, NULL raw_data
    2: ('{"some_key": "some_value"}', "hi there"),  # normal message, unrelated raw_data
    3: ('{"service_type": "service", "action_type": "chat_add_user"}', ""),  # sweep-vocabulary service row
    100: ('{"forward_from_name": "user_joined"}', ""),  # TRAP: name only as a VALUE of another key
    101: ('{"caption": "photo_removed happened"}', "the photo_removed yesterday"),  # TRAP: name in text/other key
    102: ('{"action_type": "userZjoined"}', ""),  # near-miss: underscore is literal, not a wildcard
    103: ('{"action_type": "user joined"}', ""),  # near-miss: space where the underscore would be
    104: ("{broken", ""),  # malformed JSON: must be untouched with no exception
}


def _seed_all(conn):
    for msg_id, raw in _PHANTOM_ROWS.items():
        _insert_message(conn, msg_id, raw)
    for msg_id, (raw, text) in _KEPT_ROWS.items():
        _insert_message(conn, msg_id, raw, text=text)


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "019"
    assert migration.down_revision == "018"


def test_deletes_phantoms_and_preserves_everything_else():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        _seed_all(conn)

        _run(conn, migration.upgrade)

        surviving = _message_ids(conn)
        # Every phantom form (all 7 names, spaced + compact, with/without
        # service_type, high + low id) is gone.
        for phantom_id in _PHANTOM_ROWS:
            assert phantom_id not in surviving, f"phantom id {phantom_id} should be deleted"
        # Every legitimate / trap / malformed row survives untouched.
        assert surviving == set(_KEPT_ROWS), f"unexpected survivors: {surviving ^ set(_KEPT_ROWS)}"


def test_child_rows_follow_their_message():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        _seed_all(conn)

        # Children on a phantom (user_joined, id 5_000_000_001) — must be deleted.
        conn.execute(
            sa.text("INSERT INTO reactions (id, message_id, chat_id, emoji) VALUES (1, 5000000001, -100, '👍')")
        )
        conn.execute(sa.text("INSERT INTO media (id, message_id, chat_id) VALUES ('m-phantom', 5000000001, -100)"))
        conn.execute(
            sa.text(
                "INSERT INTO message_versions (id, message_id, chat_id, change_hash) VALUES (1, 5000000001, -100, 'h1')"
            )
        )
        # Children on a kept message (chat_add_user, id 3) — must be preserved.
        conn.execute(sa.text("INSERT INTO reactions (id, message_id, chat_id, emoji) VALUES (2, 3, -100, '🔥')"))
        conn.execute(sa.text("INSERT INTO media (id, message_id, chat_id) VALUES ('m-kept', 3, -100)"))
        conn.execute(
            sa.text("INSERT INTO message_versions (id, message_id, chat_id, change_hash) VALUES (2, 3, -100, 'h2')")
        )

        _run(conn, migration.upgrade)

        # phantom's children gone
        assert conn.execute(sa.text("SELECT COUNT(*) FROM reactions WHERE message_id=5000000001")).scalar() == 0
        assert conn.execute(sa.text("SELECT COUNT(*) FROM media WHERE message_id=5000000001")).scalar() == 0
        assert conn.execute(sa.text("SELECT COUNT(*) FROM message_versions WHERE message_id=5000000001")).scalar() == 0
        # kept message's children preserved
        assert conn.execute(sa.text("SELECT COUNT(*) FROM reactions WHERE message_id=3")).scalar() == 1
        assert conn.execute(sa.text("SELECT COUNT(*) FROM media WHERE message_id=3")).scalar() == 1
        assert conn.execute(sa.text("SELECT COUNT(*) FROM message_versions WHERE message_id=3")).scalar() == 1


def test_idempotent_rerun():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        _seed_all(conn)

        _run(conn, migration.upgrade)
        after_first = _message_ids(conn)

        _run(conn, migration.upgrade)  # second run must delete 0 more rows and not raise
        after_second = _message_ids(conn)

        assert after_first == after_second == set(_KEPT_ROWS)


def test_malformed_raw_data_is_untouched_and_raises_nothing():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        # Only a malformed row present — proves the predicate never parses JSON.
        _insert_message(conn, 500, "{broken")
        _insert_message(conn, 501, '{"action_type": "user_joined"')  # truncated but matches the LIKE

        _run(conn, migration.upgrade)  # must not raise

        surviving = _message_ids(conn)
        assert 500 in surviving  # malformed, no action_type match -> kept
        assert 501 not in surviving  # contains the phantom fragment -> deleted (parse-free)


def test_stats_cache_invalidated_other_metadata_preserved():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        _seed_all(conn)
        conn.execute(
            sa.text('INSERT INTO metadata ("key", value) VALUES (:k, :v)'),
            {"k": "cached_stats", "v": '{"messages": 999}'},
        )
        conn.execute(
            sa.text('INSERT INTO metadata ("key", value) VALUES (:k, :v)'),
            {"k": "stats_calculated_at", "v": "2026-07-19T00:00:00"},
        )
        conn.execute(
            sa.text('INSERT INTO metadata ("key", value) VALUES (:k, :v)'),
            {"k": "last_backup_time", "v": "2026-07-19T00:00:00"},
        )

        _run(conn, migration.upgrade)

        keys = {row[0] for row in conn.execute(sa.text('SELECT "key" FROM metadata')).fetchall()}
        assert "cached_stats" not in keys  # invalidated
        # The startup-recompute marker must go with the blob, or the viewer shows
        # zeros until the daily scheduled recompute (lifespan only runs the
        # immediate initial calculation when stats_calculated_at is absent).
        assert "stats_calculated_at" not in keys
        assert "last_backup_time" in keys  # unrelated metadata preserved

        # Idempotent: re-run with the cache already gone must not raise.
        _run(conn, migration.upgrade)


def test_upgrade_noop_when_metadata_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        # Only messages present — no metadata table and no child tables. Proves
        # the `if <table> in tables` guards keep the delete from raising.
        conn.execute(
            sa.text(
                "CREATE TABLE messages ("
                "id BIGINT NOT NULL, chat_id BIGINT NOT NULL, date DATETIME, text TEXT, raw_data TEXT, "
                "PRIMARY KEY (id, chat_id))"
            )
        )
        _insert_message(conn, 5000000001, '{"action_type": "user_joined"}')

        _run(conn, migration.upgrade)  # must not raise despite missing metadata/child tables
        assert _message_ids(conn) == set()


def test_upgrade_noop_when_messages_table_absent():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration.upgrade)  # no messages table -> no-op, no raise
        assert "messages" not in sa.inspect(conn).get_table_names()


def test_downgrade_is_noop():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_schema(conn)
        _seed_all(conn)
        seeded = _message_ids(conn)

        _run(conn, migration.downgrade)  # no-op: nothing restored, nothing deleted, no raise
        assert _message_ids(conn) == seeded
