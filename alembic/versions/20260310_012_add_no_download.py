"""Add no_download column to viewer_accounts and viewer_tokens.

Controls whether viewers/tokens can download media files.
Default is 1 (restricted). Admin can toggle per account/token.

Revision ID: 012
Revises: 011
Create Date: 2026-03-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("viewer_accounts", sa.Column("no_download", sa.Integer(), server_default="1", nullable=True))
    op.add_column("viewer_tokens", sa.Column("no_download", sa.Integer(), server_default="1", nullable=True))


def downgrade() -> None:
    op.drop_column("viewer_tokens", "no_download")
    op.drop_column("viewer_accounts", "no_download")
