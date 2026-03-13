"""Add viewer tokens, app settings, session fields, and no_download columns (v7.2.0).

Creates:
1. viewer_tokens table for share-token authentication
2. app_settings table for cross-container configuration
3. no_download column on viewer_accounts
4. no_download + source_token_id columns on viewer_sessions

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
    existing_tables = set(inspector.get_table_names())

    # -- viewer_tokens table --
    if "viewer_tokens" not in existing_tables:
        op.create_table(
            "viewer_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(255), nullable=True),
            sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
            sa.Column("token_salt", sa.String(64), nullable=False),
            sa.Column("created_by", sa.String(255), nullable=False),
            sa.Column("allowed_chat_ids", sa.Text(), nullable=False),
            sa.Column("is_revoked", sa.Integer(), server_default="0"),
            sa.Column("no_download", sa.Integer(), server_default="0"),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("use_count", sa.Integer(), server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_viewer_tokens_created_by", "viewer_tokens", ["created_by"])
        op.create_index("idx_viewer_tokens_is_revoked", "viewer_tokens", ["is_revoked"])
    else:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("viewer_tokens")}
        if "idx_viewer_tokens_created_by" not in existing_indexes:
            op.create_index("idx_viewer_tokens_created_by", "viewer_tokens", ["created_by"])
        if "idx_viewer_tokens_is_revoked" not in existing_indexes:
            op.create_index("idx_viewer_tokens_is_revoked", "viewer_tokens", ["is_revoked"])

    # -- app_settings table --
    if "app_settings" not in existing_tables:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(255), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("key"),
        )

    # -- no_download column on viewer_accounts --
    existing_va_cols = {c["name"] for c in inspector.get_columns("viewer_accounts")}
    if "no_download" not in existing_va_cols:
        op.add_column("viewer_accounts", sa.Column("no_download", sa.Integer(), server_default="0", nullable=True))

    # -- no_download and source_token_id columns on viewer_sessions --
    if "viewer_sessions" in existing_tables:
        existing_vs_cols = {c["name"] for c in inspector.get_columns("viewer_sessions")}
        if "no_download" not in existing_vs_cols:
            op.add_column("viewer_sessions", sa.Column("no_download", sa.Integer(), server_default="0", nullable=True))
        if "source_token_id" not in existing_vs_cols:
            op.add_column("viewer_sessions", sa.Column("source_token_id", sa.Integer(), nullable=True))
        existing_vs_indexes = {idx["name"] for idx in inspector.get_indexes("viewer_sessions")}
        if "idx_viewer_sessions_source_token" not in existing_vs_indexes:
            op.create_index("idx_viewer_sessions_source_token", "viewer_sessions", ["source_token_id"])


def downgrade() -> None:
    op.drop_index("idx_viewer_sessions_source_token", table_name="viewer_sessions")
    op.drop_column("viewer_sessions", "source_token_id")
    op.drop_column("viewer_sessions", "no_download")
    op.drop_column("viewer_accounts", "no_download")
    op.drop_table("app_settings")
    op.drop_index("idx_viewer_tokens_is_revoked", table_name="viewer_tokens")
    op.drop_index("idx_viewer_tokens_created_by", table_name="viewer_tokens")
    op.drop_table("viewer_tokens")
