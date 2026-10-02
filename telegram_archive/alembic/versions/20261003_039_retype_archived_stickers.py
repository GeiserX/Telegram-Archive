"""File the stickers older backups filed under another type as stickers.

Before 9.2.0 the classifier returned at the first Video attribute, and Telegram
sends a video sticker as [Video, Filename('sticker.webm'), Sticker]. Every video
sticker was stored as ``video``. Rows written by older versions also hold animated
stickers as ``document``. New captures are classified right; this repairs the
rows already kept, so the gallery, replies, exports and the media filter see
them as stickers too.

Data only. It changes ``media.type`` and nothing else: no file, no byte, no id.
``media.id`` is opaque by the adapter's own contract (``reconcile_media_row``
re-types a row the same way whenever a backup reads its message again), and
the viewer finds a row by its chat, message and type columns.

Each rule names the file Telegram itself names a sticker, so an ordinary video
or document is never touched:

* a ``video`` row whose file name ends ``_sticker.webm``, with a webm or unknown
  mime type, at most 512 pixels on each side and at most 3 seconds long (the
  limits Telegram sets for a video sticker);
* a ``document`` row whose file name ends ``_AnimatedSticker.tgs``.

Idempotent: a second run finds no row left to change. The downgrade does
nothing: which rows held the old label is not recorded, and the old label was
wrong.

Revision ID: 039
Revises: 038
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# The chain may be renumbered at merge time: these two lines are the only place.
revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# '!' escapes the LIKE wildcard '_' so the underscore before the name is literal.
# A backslash would need different quoting on the two backends.
VIDEO_STICKERS = sa.text(
    "UPDATE media SET type = 'sticker' "
    "WHERE type = 'video' "
    "AND file_name LIKE '%!_sticker.webm' ESCAPE '!' "
    "AND (mime_type = 'video/webm' OR mime_type IS NULL) "
    "AND COALESCE(width, 0) <= 512 "
    "AND COALESCE(height, 0) <= 512 "
    "AND COALESCE(duration, 0) <= 3"
)
ANIMATED_STICKERS = sa.text(
    "UPDATE media SET type = 'sticker' WHERE type = 'document' AND file_name LIKE '%!_AnimatedSticker.tgs' ESCAPE '!'"
)
REQUIRED_COLUMNS = {"type", "file_name", "mime_type", "width", "height", "duration"}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "media" not in inspector.get_table_names():
        return
    if not REQUIRED_COLUMNS.issubset(c["name"] for c in inspector.get_columns("media")):
        return
    conn.execute(VIDEO_STICKERS)
    conn.execute(ANIMATED_STICKERS)


def downgrade() -> None:
    # Nothing to undo: the old label is not recorded and was wrong.
    pass
