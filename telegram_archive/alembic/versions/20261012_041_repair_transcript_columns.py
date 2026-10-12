"""Add the ``media_transcripts`` columns an earlier build of 032 never made.

Migration 032 was changed after a build of it had shipped: a database that
applied the earlier build has ``media_transcripts`` without ``job_stored_at``,
``copied_from_id`` and ``diarize``. Alembic does not run a revision again, so
those databases never gained them, and every transcription drain failed with
``column media_transcripts.copied_from_id does not exist``.

Adds whichever of the three is missing. A database that has them, or that made
the table with the current 032, is left untouched. The downgrade removes
nothing: the columns belong to 032.

Revision ID: 041
Revises: 040
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "media_transcripts"

# (name, type) of the columns 032 now creates and an earlier build of it did not.
COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("job_stored_at", sa.DateTime()),
    ("copied_from_id", sa.Integer()),
    ("diarize", sa.Boolean()),
)


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if TABLE_NAME not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    for name, column_type in COLUMNS:
        if name not in present:
            op.add_column(TABLE_NAME, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    # Nothing to undo: the columns are 032's, and dropping them here would
    # leave 032 without columns it creates.
    pass
