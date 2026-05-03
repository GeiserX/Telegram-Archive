"""Add content_hash column to media table for content-based deduplication.

Enables SHA-256 content hashing so identical files sent via different
Telegram methods (streaming vs document) are deduplicated correctly.

Revision ID: 011
Revises: 010
Create Date: 2026-05-03

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

    existing_cols = {c["name"] for c in inspector.get_columns("media")}
    if "content_hash" not in existing_cols:
        op.add_column("media", sa.Column("content_hash", sa.String(64), nullable=True))

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("media")}
    if "idx_media_content_hash" not in existing_indexes:
        op.create_index("idx_media_content_hash", "media", ["content_hash"])


def downgrade() -> None:
    op.drop_index("idx_media_content_hash", table_name="media")
    op.drop_column("media", "content_hash")
