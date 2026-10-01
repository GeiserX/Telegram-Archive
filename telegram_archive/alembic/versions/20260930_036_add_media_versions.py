"""Keep a message's earlier media when an edit replaces the photo or file.

Telegram lets a sender replace a message's media on edit. The edit paths
compared only the text, so the old file stayed, the new media was never
downloaded, and the new caption sat beside the old picture.

``media.telegram_file_id`` holds Telegram's id of the photo or document a
media row stands for. A different id on a later read means the media was
replaced. Rows from before this migration keep NULL: the id is read from their
file name instead, and a row whose name carries none stays unknown.

``media_versions`` is append-only. Before a media row takes a new file, its
values are copied there with the id it had (transcripts point at it) and the
date that media became current, the same date ``message_versions`` gives the
text beside it. ``skip_reason`` and ``first_seen`` (the row's ``created_at``)
come along, so a filtered or oversize media keeps its reason and its first
capture time.

Idempotent: the entrypoint stamping ladder is frozen at 018, a create_all()
database already has the table and the column, and every step is guarded by
the inspector.

Revision ID: 036
Revises: 035
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media_versions"
INDEX_NAME = "ix_media_versions_message"
MEDIA_COLUMN = "telegram_file_id"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "media" in tables and MEDIA_COLUMN not in {c["name"] for c in inspector.get_columns("media")}:
        op.add_column("media", sa.Column(MEDIA_COLUMN, sa.String(length=32), nullable=True))

    if "messages" not in tables:
        return

    if TABLE_NAME not in tables:
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("media_id", sa.String(length=255), nullable=False),
            sa.Column("type", sa.String(length=50), nullable=True),
            sa.Column("telegram_file_id", sa.String(length=32), nullable=True),
            sa.Column("file_path", sa.Text(), nullable=True),
            sa.Column("file_name", sa.String(length=255), nullable=True),
            sa.Column("file_size", sa.BigInteger(), nullable=True),
            sa.Column("mime_type", sa.String(length=100), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("duration", sa.Integer(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("downloaded", sa.Integer(), nullable=False),
            sa.Column("download_date", sa.DateTime(), nullable=True),
            sa.Column("skip_reason", sa.String(length=16), nullable=True),
            sa.Column("first_seen", sa.DateTime(), nullable=True),
            sa.Column("date", sa.DateTime(), nullable=False),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["account_id", "message_id", "chat_id"],
                ["messages.account_id", "messages.id", "messages.chat_id"],
                name="fk_media_versions_message",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("account_id", "media_id", name="uq_media_versions_media_id"),
        )
        inspector = sa.inspect(conn)

    # A database that ran an earlier build of this revision has the table
    # without these two columns.
    version_columns = {c["name"] for c in inspector.get_columns(TABLE_NAME)}
    if "skip_reason" not in version_columns:
        op.add_column(TABLE_NAME, sa.Column("skip_reason", sa.String(length=16), nullable=True))
    if "first_seen" not in version_columns:
        op.add_column(TABLE_NAME, sa.Column("first_seen", sa.DateTime(), nullable=True))

    if INDEX_NAME not in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
        op.create_index(INDEX_NAME, TABLE_NAME, ["account_id", "chat_id", "message_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = inspector.get_table_names()
    if TABLE_NAME in tables:
        if INDEX_NAME in {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}:
            op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)
    if "media" in tables and MEDIA_COLUMN in {c["name"] for c in inspector.get_columns("media")}:
        op.drop_column("media", MEDIA_COLUMN)
