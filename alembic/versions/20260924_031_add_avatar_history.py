"""Keep every profile photo each account saw for a chat.

``chats.avatar_photo_id`` (029) is one pointer per (account, chat), and the
backup overwrites it whenever the photo changes, so the archive forgot every
earlier photo id and could not tell "this account saw the photo removed" from
"never recorded". ``avatar_history`` is append-only: ``upsert_chat`` adds a row
whenever the recorded id changes, and a removal is a row with ``photo_id`` NULL.

The upgrade seeds one row per chat that already has a recorded photo and no
history yet, dated at the chat row's last update, so the photo seen before the
upgrade stays in the history once it changes. Idempotent: the entrypoint
stamping ladder is frozen at 018, a create_all() database already has the
table, and the seed skips any chat that already has a history row.

Revision ID: 031
Revises: 030
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "avatar_history"
INDEX_NAME = "ix_avatar_history_account_chat_seen"

SEED_SQL = sa.text(
    "INSERT INTO avatar_history (account_id, chat_id, photo_id, seen_at) "
    "SELECT c.account_id, c.id, c.avatar_photo_id, COALESCE(c.updated_at, CURRENT_TIMESTAMP) "
    "FROM chats c "
    "WHERE c.avatar_photo_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM avatar_history h WHERE h.account_id = c.account_id AND h.chat_id = c.id)"
)


def _can_seed(inspector: sa.Inspector) -> bool:
    """Whether chats exists and carries 029's column, which the seed reads."""
    if "chats" not in inspector.get_table_names():
        return False
    return "avatar_photo_id" in {c["name"] for c in inspector.get_columns("chats")}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("photo_id", sa.BigInteger(), nullable=True),
            sa.Column("seen_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(conn)

    if INDEX_NAME not in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
        op.create_index(INDEX_NAME, TABLE_NAME, ["account_id", "chat_id", "seen_at"])

    if _can_seed(inspector):
        conn.execute(SEED_SQL)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    if INDEX_NAME in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
