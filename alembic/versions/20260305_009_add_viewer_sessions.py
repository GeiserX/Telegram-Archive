"""Add persistent viewer sessions table.

Sessions were previously stored only in-memory, causing all users to be
logged out on every container restart. This migration adds DB-backed
session storage that survives restarts.

Revision ID: 009
Revises: 008
Create Date: 2026-03-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "viewer_sessions" not in inspector.get_table_names():
        op.create_table(
            "viewer_sessions",
            sa.Column("token", sa.String(64), nullable=False),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("allowed_chat_ids", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("last_accessed", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("token"),
        )
        op.create_index("idx_viewer_sessions_username", "viewer_sessions", ["username"])
        op.create_index("idx_viewer_sessions_created_at", "viewer_sessions", ["created_at"])
    else:
        # Table exists (from create_all), ensure indexes exist
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("viewer_sessions")}
        if "idx_viewer_sessions_username" not in existing_indexes:
            op.create_index("idx_viewer_sessions_username", "viewer_sessions", ["username"])
        if "idx_viewer_sessions_created_at" not in existing_indexes:
            op.create_index("idx_viewer_sessions_created_at", "viewer_sessions", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_viewer_sessions_created_at", table_name="viewer_sessions")
    op.drop_index("idx_viewer_sessions_username", table_name="viewer_sessions")
    op.drop_table("viewer_sessions")
