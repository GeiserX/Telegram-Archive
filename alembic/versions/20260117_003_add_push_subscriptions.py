"""Add push_subscriptions table for Web Push notifications.

Revision ID: 003
Revises: 002
Create Date: 2026-01-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "push_subscriptions"
INDEX_NAME = "idx_push_sub_chat"


def _index_exists(inspector: sa.Inspector) -> bool:
    return INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    """Create push_subscriptions table."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Idempotent: create_all()-provisioned databases may already have this table.
    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column("p256dh", sa.String(255), nullable=False),
            sa.Column("auth", sa.String(255), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("user_agent", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("endpoint"),
        )
        inspector = sa.inspect(conn)

    if not _index_exists(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, ["chat_id"])


def downgrade() -> None:
    """Remove push_subscriptions table."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME not in inspector.get_table_names():
        return

    if _index_exists(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
