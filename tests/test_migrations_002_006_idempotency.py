"""Tests for Alembic migrations 002-006 idempotency guards.

These migrations predate the inspector-guard convention introduced later in the
chain (007+). A database provisioned via ``Base.metadata.create_all()`` and then
stamped at a revision below the migration in question already has every object
the migration would otherwise (unconditionally) try to create - so each of these
migrations must detect existing tables/columns/indexes/FKs and no-op instead of
crash-looping. See CLAUDE.md's "Alembic Migrations - Critical Reminders" section.
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


def _fk_referred_tables(conn, table):
    return {fk.get("referred_table") for fk in sa.inspect(conn).get_foreign_keys(table)}


# ============================================================================
# Migration 002 - idx_messages_chat_date_desc
# ============================================================================

migration_002 = _load_migration("20260116_002_add_chat_date_index.py", "migration_002")


def _create_messages_table_002(conn):
    conn.execute(sa.text("CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, date DATETIME NOT NULL)"))


def test_002_revision_chain():
    assert migration_002.revision == "002"
    assert migration_002.down_revision == "001"


def test_002_upgrade_creates_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table_002(conn)

        _run(conn, migration_002.upgrade)
        assert "idx_messages_chat_date_desc" in _indexes(conn, "messages")

        _run(conn, migration_002.upgrade)  # re-run must be a no-op
        assert "idx_messages_chat_date_desc" in _indexes(conn, "messages")


def test_002_upgrade_noop_when_index_already_exists():
    """Simulates create_all(): the Message model's Index() already made this."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table_002(conn)
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_date_desc ON messages (chat_id, date DESC)"))

        _run(conn, migration_002.upgrade)  # must not raise
        assert "idx_messages_chat_date_desc" in _indexes(conn, "messages")


def test_002_downgrade_drops_index_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table_002(conn)
        _run(conn, migration_002.upgrade)

        _run(conn, migration_002.downgrade)
        assert "idx_messages_chat_date_desc" not in _indexes(conn, "messages")

        _run(conn, migration_002.downgrade)  # idempotent
        assert "idx_messages_chat_date_desc" not in _indexes(conn, "messages")


def test_002_upgrade_noop_when_messages_table_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_002.upgrade)  # no messages table -> no-op, no raise
        assert "messages" not in sa.inspect(conn).get_table_names()


# ============================================================================
# Migration 003 - push_subscriptions table + idx_push_sub_chat
# ============================================================================

migration_003 = _load_migration("20260117_003_add_push_subscriptions.py", "migration_003")


def test_003_revision_chain():
    assert migration_003.revision == "003"
    assert migration_003.down_revision == "002"


def test_003_upgrade_creates_table_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_003.upgrade)
        assert "push_subscriptions" in sa.inspect(conn).get_table_names()
        assert "idx_push_sub_chat" in _indexes(conn, "push_subscriptions")

        _run(conn, migration_003.upgrade)  # re-run must be a no-op
        assert "push_subscriptions" in sa.inspect(conn).get_table_names()


def test_003_upgrade_noop_when_table_already_exists():
    """Simulates create_all(): the PushSubscription model already made this."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE push_subscriptions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "endpoint TEXT NOT NULL UNIQUE, "
                "p256dh VARCHAR(255) NOT NULL, "
                "auth VARCHAR(255) NOT NULL, "
                "chat_id BIGINT, "
                "user_agent VARCHAR(500), "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "last_used_at DATETIME"
                ")"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_push_sub_chat ON push_subscriptions (chat_id)"))

        _run(conn, migration_003.upgrade)  # must not raise
        assert "idx_push_sub_chat" in _indexes(conn, "push_subscriptions")


def test_003_downgrade_drops_table_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_003.upgrade)

        _run(conn, migration_003.downgrade)
        assert "push_subscriptions" not in sa.inspect(conn).get_table_names()

        _run(conn, migration_003.downgrade)  # idempotent
        assert "push_subscriptions" not in sa.inspect(conn).get_table_names()


# ============================================================================
# Migration 004 - messages.is_pinned column + idx_messages_chat_pinned
# ============================================================================

migration_004 = _load_migration("20260125_004_add_is_pinned_column.py", "migration_004")


def _create_messages_table_004(conn):
    conn.execute(sa.text("CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, date DATETIME NOT NULL)"))


def test_004_revision_chain():
    assert migration_004.revision == "004"
    assert migration_004.down_revision == "003"


def test_004_upgrade_adds_column_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table_004(conn)

        _run(conn, migration_004.upgrade)
        assert "is_pinned" in _columns(conn, "messages")
        assert "idx_messages_chat_pinned" in _indexes(conn, "messages")

        _run(conn, migration_004.upgrade)  # re-run must be a no-op
        assert "is_pinned" in _columns(conn, "messages")


def test_004_upgrade_noop_when_column_and_index_already_exist():
    """Simulates create_all(): the Message model already declares is_pinned + index."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
                "date DATETIME NOT NULL, is_pinned INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_messages_chat_pinned ON messages (chat_id, is_pinned)"))

        _run(conn, migration_004.upgrade)  # must not raise
        assert "is_pinned" in _columns(conn, "messages")
        assert "idx_messages_chat_pinned" in _indexes(conn, "messages")


def test_004_downgrade_drops_column_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_messages_table_004(conn)
        _run(conn, migration_004.upgrade)

        _run(conn, migration_004.downgrade)
        assert "is_pinned" not in _columns(conn, "messages")

        _run(conn, migration_004.downgrade)  # idempotent
        assert "is_pinned" not in _columns(conn, "messages")


def test_004_upgrade_noop_when_messages_table_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_004.upgrade)  # no messages table -> no-op, no raise
        assert "messages" not in sa.inspect(conn).get_table_names()


# ============================================================================
# Migration 005 - v6 schema normalization (the big one)
# ============================================================================

migration_005 = _load_migration("20260128_005_v6_schema_normalization.py", "migration_005")


def _create_legacy_pre005_schema(conn):
    """Schema shape as of migrations 001-004 applied (pre-normalization)."""
    conn.execute(sa.text("CREATE TABLE chats (id BIGINT NOT NULL PRIMARY KEY, username VARCHAR(255))"))
    conn.execute(sa.text("CREATE TABLE users (id BIGINT NOT NULL PRIMARY KEY, username VARCHAR(255))"))
    conn.execute(
        sa.text("""
        CREATE TABLE messages (
            id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            sender_id BIGINT,
            date DATETIME NOT NULL,
            text TEXT,
            reply_to_msg_id BIGINT,
            reply_to_text TEXT,
            forward_from_id BIGINT,
            edit_date DATETIME,
            media_type VARCHAR(50),
            media_id VARCHAR(255),
            media_path VARCHAR(500),
            raw_data TEXT,
            created_at DATETIME,
            is_outgoing INTEGER DEFAULT 0,
            is_pinned INTEGER DEFAULT 0 NOT NULL,
            PRIMARY KEY (id, chat_id),
            FOREIGN KEY(chat_id) REFERENCES chats (id)
        )
    """)
    )
    conn.execute(
        sa.text("""
        CREATE TABLE media (
            id VARCHAR(255) NOT NULL PRIMARY KEY,
            message_id BIGINT,
            chat_id BIGINT,
            type VARCHAR(50),
            file_path VARCHAR(500),
            file_name VARCHAR(255),
            file_size BIGINT,
            mime_type VARCHAR(100),
            width INTEGER,
            height INTEGER,
            duration INTEGER,
            downloaded INTEGER DEFAULT 0,
            download_date DATETIME,
            created_at DATETIME
        )
    """)
    )
    conn.execute(
        sa.text("""
        CREATE TABLE reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            emoji VARCHAR(50) NOT NULL,
            user_id BIGINT,
            count INTEGER DEFAULT 1,
            created_at DATETIME
        )
    """)
    )


def test_005_revision_chain():
    assert migration_005.revision == "005"
    assert migration_005.down_revision == "004"


def test_005_upgrade_migrates_legacy_data_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_legacy_pre005_schema(conn)

        conn.execute(sa.text("INSERT INTO chats (id, username) VALUES (1, 'c')"))
        conn.execute(sa.text("INSERT INTO users (id, username) VALUES (10, 'u')"))
        # Message with media_id but no corresponding media row yet (Step 1 must create it).
        conn.execute(
            sa.text(
                "INSERT INTO messages (id, chat_id, date, media_type, media_id, media_path) "
                "VALUES (100, 1, '2026-01-01 00:00:00', 'photo', 'file_abc', '/data/file_abc.jpg')"
            )
        )
        # Orphan media row (no matching message) - Step 4 must delete it.
        conn.execute(
            sa.text("INSERT INTO media (id, message_id, chat_id, type) VALUES ('orphan_media', 999, 1, 'photo')")
        )
        # Orphan reaction (user doesn't exist) - Step 4 must null out user_id.
        conn.execute(
            sa.text("INSERT INTO reactions (message_id, chat_id, emoji, user_id) VALUES (100, 1, '\U0001f44d', 12345)")
        )

        _run(conn, migration_005.upgrade)

        # Step 1/3: media columns removed from messages, data migrated to media table.
        assert "media_type" not in _columns(conn, "messages")
        migrated = conn.execute(sa.text("SELECT chat_id, type FROM media WHERE id = 'file_abc'")).fetchone()
        assert migrated == (1, "photo")

        # Step 4: orphan media deleted, orphan reaction's user_id nulled.
        assert conn.execute(sa.text("SELECT 1 FROM media WHERE id = 'orphan_media'")).fetchone() is None
        reaction_user = conn.execute(sa.text("SELECT user_id FROM reactions WHERE message_id = 100")).scalar()
        assert reaction_user is None

        # Step 5: FK from media to messages now exists.
        assert "messages" in _fk_referred_tables(conn, "media")

        # Step 7: new indexes present.
        assert "idx_messages_reply_to" in _indexes(conn, "messages")
        assert "idx_media_downloaded" in _indexes(conn, "media")
        assert "idx_media_type" in _indexes(conn, "media")
        assert "idx_reactions_user" in _indexes(conn, "reactions")
        assert "idx_chats_username" in _indexes(conn, "chats")
        assert "idx_users_username" in _indexes(conn, "users")

        # Re-run must be a no-op (already normalized by the first run).
        _run(conn, migration_005.upgrade)
        assert "media_type" not in _columns(conn, "messages")
        assert "messages" in _fk_referred_tables(conn, "media")


def test_005_upgrade_noop_against_already_normalized_schema():
    """Simulates create_all(): DB stamped below 005 but built from current models.py,
    so messages never had media_type/media_id/media_path and media already has its FK.
    """
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE chats (id BIGINT NOT NULL PRIMARY KEY, username VARCHAR(255))"))
        conn.execute(sa.text("CREATE INDEX idx_chats_username ON chats (username)"))
        conn.execute(sa.text("CREATE TABLE users (id BIGINT NOT NULL PRIMARY KEY, username VARCHAR(255))"))
        conn.execute(sa.text("CREATE INDEX idx_users_username ON users (username)"))
        conn.execute(
            sa.text("""
            CREATE TABLE messages (
                id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                date DATETIME NOT NULL,
                reply_to_msg_id BIGINT,
                is_pinned INTEGER DEFAULT 0 NOT NULL,
                PRIMARY KEY (id, chat_id)
            )
        """)
        )
        conn.execute(sa.text("CREATE INDEX idx_messages_reply_to ON messages (chat_id, reply_to_msg_id)"))
        conn.execute(
            sa.text("""
            CREATE TABLE media (
                id VARCHAR(255) NOT NULL PRIMARY KEY,
                message_id BIGINT,
                chat_id BIGINT,
                type VARCHAR(50),
                downloaded INTEGER DEFAULT 0 NOT NULL,
                FOREIGN KEY(message_id, chat_id) REFERENCES messages (id, chat_id) ON DELETE CASCADE
            )
        """)
        )
        conn.execute(sa.text("CREATE INDEX idx_media_downloaded ON media (chat_id, downloaded)"))
        conn.execute(sa.text("CREATE INDEX idx_media_type ON media (type)"))
        conn.execute(
            sa.text("""
            CREATE TABLE reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                FOREIGN KEY(user_id) REFERENCES users (id)
            )
        """)
        )
        conn.execute(sa.text("CREATE INDEX idx_reactions_user ON reactions (user_id)"))

        _run(conn, migration_005.upgrade)  # must not raise
        assert "media_type" not in _columns(conn, "messages")
        assert "messages" in _fk_referred_tables(conn, "media")

        _run(conn, migration_005.upgrade)  # idempotent
        assert "media_type" not in _columns(conn, "messages")


def test_005_downgrade_restores_legacy_columns_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_legacy_pre005_schema(conn)
        conn.execute(sa.text("INSERT INTO chats (id, username) VALUES (1, 'c')"))
        conn.execute(
            sa.text(
                "INSERT INTO messages (id, chat_id, date, media_type, media_id, media_path) "
                "VALUES (100, 1, '2026-01-01 00:00:00', 'photo', 'file_abc', '/data/file_abc.jpg')"
            )
        )
        _run(conn, migration_005.upgrade)

        _run(conn, migration_005.downgrade)
        assert "media_type" in _columns(conn, "messages")
        assert "messages" not in _fk_referred_tables(conn, "media")
        restored = conn.execute(sa.text("SELECT media_type, media_id FROM messages WHERE id = 100")).fetchone()
        assert restored == ("photo", "file_abc")

        _run(conn, migration_005.downgrade)  # idempotent
        assert "media_type" in _columns(conn, "messages")


# ============================================================================
# Migration 006 - forum topics, chat folders, archived chats
# ============================================================================

migration_006 = _load_migration("20260206_006_add_topics_folders_archived.py", "migration_006")


def _create_pre006_schema(conn):
    conn.execute(sa.text("CREATE TABLE chats (id BIGINT NOT NULL PRIMARY KEY)"))
    conn.execute(
        sa.text(
            "CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
            "date DATETIME NOT NULL, PRIMARY KEY (id, chat_id))"
        )
    )


def test_006_revision_chain():
    assert migration_006.revision == "006"
    assert migration_006.down_revision == "005"


def test_006_upgrade_creates_everything_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre006_schema(conn)

        _run(conn, migration_006.upgrade)
        assert "is_forum" in _columns(conn, "chats")
        assert "is_archived" in _columns(conn, "chats")
        assert "reply_to_top_id" in _columns(conn, "messages")
        assert "idx_messages_topic" in _indexes(conn, "messages")
        table_names = sa.inspect(conn).get_table_names()
        assert "forum_topics" in table_names
        assert "chat_folders" in table_names
        assert "chat_folder_members" in table_names
        assert "idx_forum_topics_chat" in _indexes(conn, "forum_topics")
        assert "idx_folder_members_chat" in _indexes(conn, "chat_folder_members")
        assert "idx_folder_members_folder" in _indexes(conn, "chat_folder_members")

        _run(conn, migration_006.upgrade)  # re-run must be a no-op
        assert "is_forum" in _columns(conn, "chats")


def test_006_upgrade_noop_when_everything_already_exists():
    """Simulates create_all(): all v6.2.0 columns/tables/indexes already declared."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE chats (id BIGINT NOT NULL PRIMARY KEY, "
                "is_forum INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE messages (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
                "date DATETIME NOT NULL, reply_to_top_id BIGINT, PRIMARY KEY (id, chat_id))"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_messages_topic ON messages (chat_id, reply_to_top_id)"))
        conn.execute(
            sa.text(
                "CREATE TABLE forum_topics (id BIGINT NOT NULL, chat_id BIGINT NOT NULL, "
                "title VARCHAR(500) NOT NULL, PRIMARY KEY (id, chat_id))"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_forum_topics_chat ON forum_topics (chat_id)"))
        conn.execute(
            sa.text("CREATE TABLE chat_folders (id INTEGER NOT NULL PRIMARY KEY, title VARCHAR(255) NOT NULL)")
        )
        conn.execute(
            sa.text(
                "CREATE TABLE chat_folder_members (folder_id INTEGER NOT NULL, chat_id BIGINT NOT NULL, "
                "PRIMARY KEY (folder_id, chat_id))"
            )
        )
        conn.execute(sa.text("CREATE INDEX idx_folder_members_chat ON chat_folder_members (chat_id)"))
        conn.execute(sa.text("CREATE INDEX idx_folder_members_folder ON chat_folder_members (folder_id)"))

        _run(conn, migration_006.upgrade)  # must not raise
        assert "is_forum" in _columns(conn, "chats")


def test_006_downgrade_removes_everything_and_is_idempotent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _create_pre006_schema(conn)
        _run(conn, migration_006.upgrade)

        _run(conn, migration_006.downgrade)
        assert "is_forum" not in _columns(conn, "chats")
        assert "is_archived" not in _columns(conn, "chats")
        assert "reply_to_top_id" not in _columns(conn, "messages")
        table_names = sa.inspect(conn).get_table_names()
        assert "forum_topics" not in table_names
        assert "chat_folders" not in table_names
        assert "chat_folder_members" not in table_names

        _run(conn, migration_006.downgrade)  # idempotent
        assert "is_forum" not in _columns(conn, "chats")


def test_006_upgrade_noop_when_chats_and_messages_absent():
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        _run(conn, migration_006.upgrade)  # no chats/messages tables -> no-op on those steps
        table_names = sa.inspect(conn).get_table_names()
        assert "chats" not in table_names
        assert "messages" not in table_names
        # forum_topics/chat_folders/chat_folder_members are still created independently
        assert "forum_topics" in table_names
        assert "chat_folders" in table_names
        assert "chat_folder_members" in table_names
