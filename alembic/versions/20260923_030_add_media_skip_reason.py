"""Record why a media row will never download on its own.

A row sits at ``downloaded = 0`` with no file in three situations: a failed
download the retry drain will pick up, a file over ``MAX_MEDIA_SIZE_MB``, and
a file the ``DOWNLOAD_MEDIA_TYPES`` / ``DOWNLOAD_DOCUMENT_MIME_TYPES``
whitelist declined. Only the first ever changes state, but the viewer showed
"Will download on next backup" and counted all three as pending (#465). The
settings live in the backup, so the backup records the reason on the row and
the viewer reads it.

Nullable, no backfill here: the backup classifies existing rows on its next
run. Idempotent — the entrypoint stamping ladder is frozen at 018 and a
create_all() database already has the column.

Revision ID: 030
Revises: 029
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media"
COLUMN_NAME = "skip_reason"


def _has_column(inspector: sa.Inspector) -> bool | None:
    """Whether media has the column; None when there is no media table at all."""
    if TABLE_NAME not in inspector.get_table_names():
        return None
    return COLUMN_NAME in {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())) is False:
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.String(16), nullable=True))


def downgrade() -> None:
    if _has_column(sa.inspect(op.get_bind())):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
