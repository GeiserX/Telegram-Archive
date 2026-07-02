"""Add message versions table.

Revision ID: 015
Revises: 014
Create Date: 2026-06-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "message_versions"
IDX_MESSAGE_DATE = "idx_message_versions_message_date"
IDX_MESSAGE_CAPTURED = "idx_message_versions_message_captured"


def _table_exists(inspector: sa.Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def _index_exists(inspector: sa.Inspector, name: str) -> bool:
    return name in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _table_exists(inspector):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("text", sa.Text(), nullable=True),
            sa.Column("date", sa.DateTime(), nullable=False),
            sa.Column("change_hash", sa.String(length=64), nullable=False),
            sa.Column("captured_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(
                ["message_id", "chat_id"],
                ["messages.id", "messages.chat_id"],
                name="fk_message_versions_message",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("change_hash", name="uq_message_versions_change_hash"),
        )
        inspector = sa.inspect(conn)

    if not _index_exists(inspector, IDX_MESSAGE_DATE):
        op.create_index(IDX_MESSAGE_DATE, TABLE_NAME, ["chat_id", "message_id", "date"])
        inspector = sa.inspect(conn)
    if not _index_exists(inspector, IDX_MESSAGE_CAPTURED):
        op.create_index(IDX_MESSAGE_CAPTURED, TABLE_NAME, ["chat_id", "message_id", "captured_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not _table_exists(inspector):
        return

    indexes = {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    if IDX_MESSAGE_CAPTURED in indexes:
        op.drop_index(IDX_MESSAGE_CAPTURED, table_name=TABLE_NAME)
    if IDX_MESSAGE_DATE in indexes:
        op.drop_index(IDX_MESSAGE_DATE, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
