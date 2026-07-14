"""Add media.download_attempts (failed-download retry cap).

Revision ID: 016
Revises: 015
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "media"
COLUMN_NAME = "download_attempts"


def _column_exists(inspector: sa.Inspector) -> bool:
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Idempotent: create_all()-provisioned databases may already have the column.
    if TABLE_NAME in inspector.get_table_names() and not _column_exists(inspector):
        op.add_column(
            TABLE_NAME,
            sa.Column("download_attempts", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if TABLE_NAME in inspector.get_table_names() and _column_exists(inspector):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
