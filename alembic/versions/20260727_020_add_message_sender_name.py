"""Add immutable per-message sender name snapshots (#240).

Revision ID: 020
Revises: 019
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "messages"
COLUMN_NAME = "sender_name"


def _column_exists(inspector: sa.Inspector) -> bool:
    return COLUMN_NAME in {column["name"] for column in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    if not _column_exists(inspector):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Text(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    if _column_exists(inspector):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
