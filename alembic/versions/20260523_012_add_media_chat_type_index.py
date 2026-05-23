"""Add composite index on media(chat_id, type) for gallery queries.

Revision ID: 012
Revises: 011
Create Date: 2026-05-23
"""

import sqlalchemy as sa

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("media")}
    if "idx_media_chat_type" not in existing_indexes:
        op.create_index("idx_media_chat_type", "media", ["chat_id", "type"])


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("media")}
    if "idx_media_chat_type" in existing_indexes:
        op.drop_index("idx_media_chat_type", table_name="media")
