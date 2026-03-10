"""Add viewer_tokens table for share-token authentication.

Admins can create share tokens that grant scoped access to specific chats
without requiring username/password. Tokens are hashed (PBKDF2-SHA256).

Revision ID: 010
Revises: 009
Create Date: 2026-03-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "viewer_tokens" not in inspector.get_table_names():
        op.create_table(
            "viewer_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(255), nullable=True),
            sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
            sa.Column("token_salt", sa.String(64), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=False),
            sa.Column("allowed_chat_ids", sa.Text(), nullable=False),
            sa.Column("is_revoked", sa.Integer(), server_default="0"),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("use_count", sa.Integer(), server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_viewer_tokens_created_by", "viewer_tokens", ["created_by"])
        op.create_index("idx_viewer_tokens_is_revoked", "viewer_tokens", ["is_revoked"])
    else:
        # Table exists (from create_all), ensure indexes exist
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("viewer_tokens")}
        if "idx_viewer_tokens_created_by" not in existing_indexes:
            op.create_index("idx_viewer_tokens_created_by", "viewer_tokens", ["created_by"])
        if "idx_viewer_tokens_is_revoked" not in existing_indexes:
            op.create_index("idx_viewer_tokens_is_revoked", "viewer_tokens", ["is_revoked"])


def downgrade() -> None:
    op.drop_index("idx_viewer_tokens_is_revoked", table_name="viewer_tokens")
    op.drop_index("idx_viewer_tokens_created_by", table_name="viewer_tokens")
    op.drop_table("viewer_tokens")
