"""Add app_settings table for cross-container configuration.

Key-value store shared between backup and viewer containers.
Used for backup schedule override, active viewer tracking, and backup status.

Revision ID: 011
Revises: 010
Create Date: 2026-03-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "app_settings" not in inspector.get_table_names():
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("key"),
        )


def downgrade() -> None:
    op.drop_table("app_settings")
