"""Add username and allowed_chat_ids to push_subscriptions for per-user filtering.

Revision ID: 008
Revises: 007
Create Date: 2026-02-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("push_subscriptions", sa.Column("username", sa.String(255), nullable=True))
    op.add_column("push_subscriptions", sa.Column("allowed_chat_ids", sa.Text(), nullable=True))
    op.create_index("idx_push_sub_username", "push_subscriptions", ["username"])


def downgrade() -> None:
    op.drop_index("idx_push_sub_username", table_name="push_subscriptions")
    op.drop_column("push_subscriptions", "allowed_chat_ids")
    op.drop_column("push_subscriptions", "username")
