"""Add soft-delete markers to messages.

Revision ID: 014
Revises: 013
Create Date: 2026-06-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c["name"] for c in inspector.get_columns("messages")}

    if "is_deleted" not in existing_cols:
        op.add_column("messages", sa.Column("is_deleted", sa.Integer(), nullable=False, server_default="0"))
    if "deleted_at" not in existing_cols:
        op.add_column("messages", sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_cols = {c["name"] for c in inspector.get_columns("messages")}

    if "deleted_at" in existing_cols:
        op.drop_column("messages", "deleted_at")
    if "is_deleted" in existing_cols:
        op.drop_column("messages", "is_deleted")
