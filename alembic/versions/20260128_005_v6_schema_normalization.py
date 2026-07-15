"""v6.0.0 Schema normalization - remove media duplication, add FKs.

This migration:
1. Migrates any missing media data from messages to media table
2. Removes media_type, media_id, media_path from messages (normalized to media table)
3. Adds foreign key constraints for data integrity
4. Adds performance indexes

BREAKING CHANGE: Applications must now use the media table for media metadata.

Revision ID: 005
Revises: 004
Create Date: 2026-01-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(inspector: sa.Inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table: str, column: str) -> bool:
    if not _table_exists(inspector, table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def _index_exists(inspector: sa.Inspector, table: str, index: str) -> bool:
    if not _table_exists(inspector, table):
        return False
    return index in {ix["name"] for ix in inspector.get_indexes(table)}


def _fk_to_table_exists(inspector: sa.Inspector, table: str, referred_table: str) -> bool:
    """True if `table` has any FK referencing `referred_table`.

    Name-agnostic on purpose: a create_all()-provisioned DB names FKs
    differently (or auto-generates a name) than the explicit names this
    migration uses, so matching by referred table is what actually detects
    "already normalized" rather than "already named exactly like this".
    """
    if not _table_exists(inspector, table):
        return False
    return any(fk.get("referred_table") == referred_table for fk in inspector.get_foreign_keys(table))


def _fk_name_exists(inspector: sa.Inspector, table: str, name: str) -> bool:
    if not _table_exists(inspector, table):
        return False
    return any(fk.get("name") == name for fk in inspector.get_foreign_keys(table))


def upgrade() -> None:
    """Normalize schema: move media data to media table, add FKs and indexes."""

    # Get connection and dialect
    conn = op.get_bind()
    dialect = conn.dialect.name
    inspector = sa.inspect(conn)

    # Idempotency anchor: the CURRENT models.py no longer declares
    # media_type/media_id/media_path on Message (they were normalized away in
    # v6.0.0), so a create_all()-provisioned database never has them. Their
    # presence is therefore exactly the pre-normalization legacy shape this
    # migration expects to fix; their absence means either an already-normalized
    # fresh schema or a prior run of this migration - steps 1-3 are no-ops either way.
    legacy_media_cols_present = _column_exists(inspector, "messages", "media_type")

    # =========================================================================
    # STEP 1: Data Migration - Ensure all media data exists in media table
    # =========================================================================

    if legacy_media_cols_present and _table_exists(inspector, "media"):
        # Insert missing media records from messages table
        # This handles cases where messages have media_id but no corresponding media record
        # Use ON CONFLICT DO NOTHING for PostgreSQL to handle duplicate keys gracefully
        # Note: downloaded column is Integer (0/1), not Boolean
        if dialect == "postgresql":
            conn.execute(
                text("""
                INSERT INTO media (id, message_id, chat_id, type, file_path, downloaded, created_at)
                SELECT
                    m.media_id,
                    m.id,
                    m.chat_id,
                    m.media_type,
                    m.media_path,
                    CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN 1 ELSE 0 END,
                    m.created_at
                FROM messages m
                WHERE m.media_id IS NOT NULL
                  AND m.media_id != ''
                ON CONFLICT (id) DO NOTHING
            """)
            )
        else:
            # SQLite: Use INSERT OR IGNORE
            conn.execute(
                text("""
                INSERT OR IGNORE INTO media (id, message_id, chat_id, type, file_path, downloaded, created_at)
                SELECT
                    m.media_id,
                    m.id,
                    m.chat_id,
                    m.media_type,
                    m.media_path,
                    CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN 1 ELSE 0 END,
                    m.created_at
                FROM messages m
                WHERE m.media_id IS NOT NULL
                  AND m.media_id != ''
            """)
            )

        # Update existing media records that might be missing message_id/chat_id
        # (Naturally idempotent: an UPDATE ... WHERE message_id IS NULL only ever
        # touches rows that still need it, so re-running is a no-op.)
        conn.execute(
            text("""
            UPDATE media
            SET message_id = (
                SELECT m.id FROM messages m WHERE m.media_id = media.id LIMIT 1
            ),
            chat_id = (
                SELECT m.chat_id FROM messages m WHERE m.media_id = media.id LIMIT 1
            )
            WHERE message_id IS NULL
              AND EXISTS (SELECT 1 FROM messages m WHERE m.media_id = media.id)
        """)
        )

    # =========================================================================
    # STEP 2: Create backup table for rollback (stores dropped columns)
    # =========================================================================

    if legacy_media_cols_present:
        op.execute(
            text("""
            CREATE TABLE IF NOT EXISTS _messages_media_backup AS
            SELECT id, chat_id, media_type, media_id, media_path
            FROM messages
            WHERE media_id IS NOT NULL AND media_id != ''
        """)
        )

    # =========================================================================
    # STEP 3: Drop the media columns from messages table
    # =========================================================================

    if legacy_media_cols_present:
        # SQLite doesn't support DROP COLUMN directly in older versions
        # We need to handle this differently based on dialect
        if dialect == "sqlite":
            # SQLite: Recreate table without the columns
            # First, create new table structure
            # NOTE: sender_id FK is NOT enforced because sender_id can be channel/group IDs
            # that aren't in the users table. The relationship is maintained at ORM level.
            op.execute(
                text("""
                CREATE TABLE messages_new (
                    id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    sender_id INTEGER,
                    date DATETIME NOT NULL,
                    text TEXT,
                    reply_to_msg_id INTEGER,
                    reply_to_text TEXT,
                    forward_from_id INTEGER,
                    edit_date DATETIME,
                    raw_data TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_outgoing INTEGER DEFAULT 0 NOT NULL,
                    is_pinned INTEGER DEFAULT 0 NOT NULL,
                    PRIMARY KEY (id, chat_id),
                    FOREIGN KEY(chat_id) REFERENCES chats (id)
                )
            """)
            )

            # Copy data
            op.execute(
                text("""
                INSERT INTO messages_new (
                    id, chat_id, sender_id, date, text, reply_to_msg_id,
                    reply_to_text, forward_from_id, edit_date, raw_data,
                    created_at, is_outgoing, is_pinned
                )
                SELECT
                    id, chat_id, sender_id, date, text, reply_to_msg_id,
                    reply_to_text, forward_from_id, edit_date, raw_data,
                    created_at, is_outgoing, is_pinned
                FROM messages
            """)
            )

            # Drop old table and rename new
            op.execute(text("DROP TABLE messages"))
            op.execute(text("ALTER TABLE messages_new RENAME TO messages"))

            # Recreate indexes (table is brand new, so these are always absent)
            op.create_index("idx_messages_chat_id", "messages", ["chat_id"])
            op.create_index("idx_messages_date", "messages", ["date"])
            op.create_index("idx_messages_sender_id", "messages", ["sender_id"])
            op.create_index("idx_messages_chat_date_desc", "messages", ["chat_id", sa.text("date DESC")])
            op.create_index("idx_messages_chat_pinned", "messages", ["chat_id", "is_pinned"])
        else:
            # PostgreSQL: Direct column drops
            # NOTE: sender_id FK is NOT added because sender_id can be channel/group IDs
            # that aren't in the users table. The relationship is maintained at ORM level.
            if _column_exists(inspector, "messages", "media_type"):
                op.drop_column("messages", "media_type")
            if _column_exists(inspector, "messages", "media_id"):
                op.drop_column("messages", "media_id")
            if _column_exists(inspector, "messages", "media_path"):
                op.drop_column("messages", "media_path")

    # Refresh after any table rebuild above.
    inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 4: Clean up orphan data before adding FK constraints
    # =========================================================================

    # Delete orphan media records (where message doesn't exist).
    # Naturally idempotent: re-running finds no orphans left to delete.
    if _table_exists(inspector, "media") and _table_exists(inspector, "messages"):
        conn.execute(
            text("""
            DELETE FROM media
            WHERE message_id IS NOT NULL
              AND chat_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM messages
                  WHERE messages.id = media.message_id
                    AND messages.chat_id = media.chat_id
              )
        """)
        )

    # Set user_id to NULL for orphan reactions (where user doesn't exist).
    # This preserves the reaction counts while removing invalid FK references.
    # Naturally idempotent: re-running finds no orphans left to null out.
    if _table_exists(inspector, "reactions") and _table_exists(inspector, "users"):
        conn.execute(
            text("""
            UPDATE reactions
            SET user_id = NULL
            WHERE user_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users WHERE users.id = reactions.user_id)
        """)
        )

    # =========================================================================
    # STEP 5: Add FK constraint for media -> messages
    # =========================================================================

    if (
        _table_exists(inspector, "media")
        and _table_exists(inspector, "messages")
        and not _fk_to_table_exists(inspector, "media", "messages")
    ):
        if dialect == "sqlite":
            # SQLite: Recreate media table with FK
            op.execute(
                text("""
                CREATE TABLE media_new (
                    id VARCHAR(255) NOT NULL PRIMARY KEY,
                    message_id INTEGER,
                    chat_id INTEGER,
                    type VARCHAR(50),
                    file_path TEXT,
                    file_name VARCHAR(255),
                    file_size INTEGER,
                    mime_type VARCHAR(100),
                    width INTEGER,
                    height INTEGER,
                    duration INTEGER,
                    downloaded INTEGER DEFAULT 0 NOT NULL,
                    download_date DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(message_id, chat_id) REFERENCES messages (id, chat_id) ON DELETE CASCADE
                )
            """)
            )

            op.execute(
                text("""
                INSERT INTO media_new
                SELECT * FROM media
            """)
            )

            op.execute(text("DROP TABLE media"))
            op.execute(text("ALTER TABLE media_new RENAME TO media"))

            # Recreate media indexes (table is brand new, so this is always absent)
            op.create_index("idx_media_message", "media", ["message_id", "chat_id"])
        else:
            # PostgreSQL: Add FK directly
            op.create_foreign_key(
                "fk_media_message",
                "media",
                "messages",
                ["message_id", "chat_id"],
                ["id", "chat_id"],
                ondelete="CASCADE",
            )

    inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 6: Add FK for reactions.user_id -> users.id
    # =========================================================================

    if (
        dialect != "sqlite"
        and _table_exists(inspector, "reactions")
        and _table_exists(inspector, "users")
        and not _fk_to_table_exists(inspector, "reactions", "users")
    ):
        op.create_foreign_key("fk_reactions_user", "reactions", "users", ["user_id"], ["id"], ondelete="SET NULL")

    # =========================================================================
    # STEP 7: Add new performance indexes
    # =========================================================================

    inspector = sa.inspect(conn)

    # Index for reply lookups
    if not _index_exists(inspector, "messages", "idx_messages_reply_to"):
        op.create_index("idx_messages_reply_to", "messages", ["chat_id", "reply_to_msg_id"])

    # Index for finding undownloaded media
    if not _index_exists(inspector, "media", "idx_media_downloaded"):
        op.create_index("idx_media_downloaded", "media", ["chat_id", "downloaded"])

    # Index for filtering by media type
    if not _index_exists(inspector, "media", "idx_media_type"):
        op.create_index("idx_media_type", "media", ["type"])

    # Index for user reaction queries
    if not _index_exists(inspector, "reactions", "idx_reactions_user"):
        op.create_index("idx_reactions_user", "reactions", ["user_id"])

    # Index for chat username lookups
    if not _index_exists(inspector, "chats", "idx_chats_username"):
        op.create_index("idx_chats_username", "chats", ["username"])

    # Index for user username lookups
    if not _index_exists(inspector, "users", "idx_users_username"):
        op.create_index("idx_users_username", "users", ["username"])


def downgrade() -> None:
    """Restore media columns to messages table from backup."""

    conn = op.get_bind()
    dialect = conn.dialect.name
    inspector = sa.inspect(conn)

    # =========================================================================
    # STEP 1: Drop new indexes
    # =========================================================================

    for table, index in (
        ("users", "idx_users_username"),
        ("chats", "idx_chats_username"),
        ("reactions", "idx_reactions_user"),
        ("media", "idx_media_type"),
        ("media", "idx_media_downloaded"),
        ("messages", "idx_messages_reply_to"),
    ):
        if _index_exists(inspector, table, index):
            op.drop_index(index, table_name=table)

    # =========================================================================
    # STEP 2: Drop foreign keys (PostgreSQL only)
    # =========================================================================

    if dialect != "sqlite":
        inspector = sa.inspect(conn)
        # NOTE: fk_messages_sender was never created (sender_id can be channel/group IDs).
        # Name-based (not referred-table-based) on purpose: only undo the specific
        # constraints *this migration* created under these names.
        if _fk_name_exists(inspector, "reactions", "fk_reactions_user"):
            op.drop_constraint("fk_reactions_user", "reactions", type_="foreignkey")
        if _fk_name_exists(inspector, "media", "fk_media_message"):
            op.drop_constraint("fk_media_message", "media", type_="foreignkey")

    # =========================================================================
    # STEP 3: Restore media columns to messages
    # =========================================================================

    inspector = sa.inspect(conn)
    legacy_media_cols_present = _column_exists(inspector, "messages", "media_type")

    if not legacy_media_cols_present and _table_exists(inspector, "messages"):
        if dialect == "sqlite":
            # SQLite: Recreate messages table with media columns
            op.execute(
                text("""
                CREATE TABLE messages_new (
                    id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    sender_id INTEGER,
                    date DATETIME NOT NULL,
                    text TEXT,
                    reply_to_msg_id INTEGER,
                    reply_to_text TEXT,
                    forward_from_id INTEGER,
                    edit_date DATETIME,
                    media_type VARCHAR(50),
                    media_id VARCHAR(255),
                    media_path VARCHAR(500),
                    raw_data TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_outgoing INTEGER DEFAULT 0 NOT NULL,
                    is_pinned INTEGER DEFAULT 0 NOT NULL,
                    PRIMARY KEY (id, chat_id),
                    FOREIGN KEY(chat_id) REFERENCES chats (id)
                )
            """)
            )

            op.execute(
                text("""
                INSERT INTO messages_new (
                    id, chat_id, sender_id, date, text, reply_to_msg_id,
                    reply_to_text, forward_from_id, edit_date, raw_data,
                    created_at, is_outgoing, is_pinned
                )
                SELECT
                    id, chat_id, sender_id, date, text, reply_to_msg_id,
                    reply_to_text, forward_from_id, edit_date, raw_data,
                    created_at, is_outgoing, is_pinned
                FROM messages
            """)
            )

            op.execute(text("DROP TABLE messages"))
            op.execute(text("ALTER TABLE messages_new RENAME TO messages"))

            # Recreate indexes (table is brand new, so these are always absent)
            op.create_index("idx_messages_chat_id", "messages", ["chat_id"])
            op.create_index("idx_messages_date", "messages", ["date"])
            op.create_index("idx_messages_sender_id", "messages", ["sender_id"])
            op.create_index("idx_messages_chat_date_desc", "messages", ["chat_id", sa.text("date DESC")])
            op.create_index("idx_messages_chat_pinned", "messages", ["chat_id", "is_pinned"])
        else:
            # PostgreSQL: Add columns back
            if not _column_exists(inspector, "messages", "media_type"):
                op.add_column("messages", sa.Column("media_type", sa.String(50)))
            if not _column_exists(inspector, "messages", "media_id"):
                op.add_column("messages", sa.Column("media_id", sa.String(255)))
            if not _column_exists(inspector, "messages", "media_path"):
                op.add_column("messages", sa.Column("media_path", sa.String(500)))

    inspector = sa.inspect(conn)

    # SQLite media table: drop the FK by recreating without it, only if the FK is
    # actually there (i.e. STEP 5 of upgrade() actually ran against this DB).
    if dialect == "sqlite" and _fk_to_table_exists(inspector, "media", "messages"):
        # Recreate media table without FK
        op.execute(
            text("""
            CREATE TABLE media_new (
                id VARCHAR(255) NOT NULL PRIMARY KEY,
                message_id INTEGER,
                chat_id INTEGER,
                type VARCHAR(50),
                file_path VARCHAR(500),
                file_name VARCHAR(255),
                file_size INTEGER,
                mime_type VARCHAR(100),
                width INTEGER,
                height INTEGER,
                duration INTEGER,
                downloaded INTEGER DEFAULT 0 NOT NULL,
                download_date DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        )

        op.execute(text("INSERT INTO media_new SELECT * FROM media"))
        op.execute(text("DROP TABLE media"))
        op.execute(text("ALTER TABLE media_new RENAME TO media"))
        op.create_index("idx_media_message", "media", ["message_id", "chat_id"])

    # =========================================================================
    # STEP 4: Restore data from backup table
    # =========================================================================

    inspector = sa.inspect(conn)
    if _table_exists(inspector, "_messages_media_backup"):
        conn.execute(
            text("""
            UPDATE messages
            SET
                media_type = (SELECT media_type FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id),
                media_id = (SELECT media_id FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id),
                media_path = (SELECT media_path FROM _messages_media_backup b WHERE b.id = messages.id AND b.chat_id = messages.chat_id)
            WHERE EXISTS (
                SELECT 1 FROM _messages_media_backup b
                WHERE b.id = messages.id AND b.chat_id = messages.chat_id
            )
        """)
        )

    # =========================================================================
    # STEP 5: Drop backup table
    # =========================================================================

    op.execute(text("DROP TABLE IF EXISTS _messages_media_backup"))
