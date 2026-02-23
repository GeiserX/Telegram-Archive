"""Add viewer_accounts and viewer_audit_log tables for multi-user access control.

Revision ID: 007
Revises: 006
Create Date: 2026-02-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "viewer_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("salt", sa.String(64), nullable=False),
        sa.Column("allowed_chat_ids", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("idx_viewer_accounts_username", "viewer_accounts", ["username"])

    op.create_table(
        "viewer_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("viewer_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_viewer_id", "viewer_audit_log", ["viewer_id"])
    op.create_index("idx_audit_timestamp", "viewer_audit_log", ["timestamp"])


def downgrade() -> None:
    op.drop_index("idx_audit_timestamp", table_name="viewer_audit_log")
    op.drop_index("idx_audit_viewer_id", table_name="viewer_audit_log")
    op.drop_table("viewer_audit_log")
    op.drop_index("idx_viewer_accounts_username", table_name="viewer_accounts")
    op.drop_table("viewer_accounts")
