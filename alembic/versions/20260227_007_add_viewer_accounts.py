"""Add viewer accounts and audit log for multi-user access control.

This migration:
1. Creates viewer_accounts table for DB-backed viewer users
2. Creates viewer_audit_log table for access tracking

Revision ID: 007
Revises: 006
Create Date: 2026-02-27

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
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("salt", sa.String(64), nullable=False),
        sa.Column("allowed_chat_ids", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "viewer_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_log_username", "viewer_audit_log", ["username"])
    op.create_index("idx_audit_log_created", "viewer_audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_log_created", table_name="viewer_audit_log")
    op.drop_index("idx_audit_log_username", table_name="viewer_audit_log")
    op.drop_table("viewer_audit_log")
    op.drop_table("viewer_accounts")
