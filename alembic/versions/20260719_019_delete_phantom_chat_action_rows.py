"""Delete historical phantom chat-action rows written by the pre-fix listener (#222).

The old listener wrote junk "service message" rows into ``messages`` with
fabricated wall-clock IDs and a broken classifier that stamped ``raw_data`` with
seven curated event names (``photo_changed``, ``photo_removed``,
``title_changed``, ``user_joined``, ``user_left``, ``user_added``,
``user_kicked``). The listener fix switched to real Telegram IDs and the shared
``service_action_type()`` sweep vocabulary, so those seven names are no longer
emitted. This migration deletes every historical row carrying one of them.

Safety invariant: this DELETE is safe iff the listener never again emits the 7
legacy ``action_type`` names (it now uses the ``service_action_type()``
vocabulary, which is provably disjoint from these names) and the ``action_type``
IN-list filter below is never broadened. The seven names are the *entire*
identity of a phantom row: forensics across the full lifetime of both production
databases proved they never appear on a legitimate row, so there is no id or
``service_type`` condition here — real-world phantoms exist with low ids and
without a ``service_type`` key, so either boundary would be unsafe.

``raw_data`` is a TEXT column holding a JSON string that has never been parsed by
a migration and can contain malformed JSON (the adapter defends against that at
read time). The match is therefore parse-free ``LIKE`` on the raw text: a
``->>`` / ``::jsonb`` / ``json_extract`` predicate would be a type error on the
PostgreSQL TEXT column and would crash on malformed rows. Two serialization eras
are covered per name — the spaced form ``json.dumps`` emits today
(``"action_type": "user_joined"``) and a compact form
(``"action_type":"user_joined"``). Underscores in the names are escaped
(``!_`` with ``ESCAPE '!'``) so ``LIKE`` matches them literally instead of as its
single-character wildcard, pinning the match to the exact seven strings. ``LIKE``
is case-sensitive on PostgreSQL and case-insensitive for ASCII on SQLite; that is
harmless here because the JSON is always lower-case exactly as listed.

Revision ID: 019
Revises: 018
Create Date: 2026-07-19
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# The 7 legacy curated event names the broken classifier fabricated. This IN-list
# is the entire identity of a phantom row — do NOT broaden it (see module docstring).
_LEGACY_ACTION_TYPES = (
    "photo_changed",
    "photo_removed",
    "title_changed",
    "user_joined",
    "user_left",
    "user_added",
    "user_kicked",
)

# Child tables whose rows reference (message_id, chat_id). reactions has no
# ON DELETE CASCADE, and on SQLite PRAGMA foreign_keys may be OFF so the media /
# message_versions cascades cannot be relied on either — so all three are deleted
# explicitly, on both engines, before the messages rows.
_CHILD_TABLES = ("reactions", "media", "message_versions")


def _phantom_predicate() -> str:
    """Build the parse-free WHERE predicate identifying phantom rows.

    References ``messages.raw_data``; valid verbatim both as a ``messages`` filter
    and inside ``SELECT id, chat_id FROM messages WHERE ...`` for the child deletes.
    """
    clauses: list[str] = []
    for name in _LEGACY_ACTION_TYPES:
        escaped = name.replace("_", "!_")  # !_ + ESCAPE '!' => literal underscore
        # spaced form (json.dumps default): "action_type": "name"
        clauses.append(f"""raw_data LIKE '%"action_type": "{escaped}"%' ESCAPE '!'""")
        # compact form (older era): "action_type":"name"
        clauses.append(f"""raw_data LIKE '%"action_type":"{escaped}"%' ESCAPE '!'""")
    return "raw_data IS NOT NULL AND (" + " OR ".join(clauses) + ")"


_PHANTOM_PREDICATE = _phantom_predicate()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    tables = set(inspector.get_table_names())
    # Fresh database mid-bootstrap (create_all has not populated messages yet):
    # nothing to clean. Naturally idempotent otherwise — a re-run deletes 0 rows.
    if "messages" not in tables:
        return

    subquery = f"SELECT id, chat_id FROM messages WHERE {_PHANTOM_PREDICATE}"

    # Delete dependent rows first. Observed dependent-row count on phantoms in
    # production is zero, but a single stray child row would make the messages
    # DELETE raise and crash-loop the container at startup, so this is explicit.
    child_counts = {name: 0 for name in _CHILD_TABLES}
    for child in _CHILD_TABLES:
        if child in tables:
            result = conn.execute(sa.text(f"DELETE FROM {child} WHERE (message_id, chat_id) IN ({subquery})"))
            child_counts[child] = result.rowcount

    result = conn.execute(sa.text(f"DELETE FROM messages WHERE {_PHANTOM_PREDICATE}"))
    deleted = result.rowcount

    # Invalidate the cached statistics blob so dashboard message / per-chat counts
    # stop including the deleted phantoms. It is the only persisted aggregate that
    # counts messages: the "cached_stats" key in the metadata key-value table
    # (written by adapter.calculate_and_store_statistics). "stats_calculated_at"
    # must go WITH it: the viewer's lifespan hook only runs the immediate initial
    # recompute when that marker is absent (src/web/main.py), so dropping the blob
    # alone would leave the dashboard showing zeros for up to a day until the
    # scheduled recompute. Deleting both makes the next viewer start recompute
    # fresh stats right away. Per-chat stats (get_chat_stats) are computed on
    # the fly behind a 60-second in-memory web-layer cache that expires on its
    # own, so they need no action here.
    # Idempotent: a re-run deletes 0 rows.
    if "metadata" in tables:
        conn.execute(sa.text("DELETE FROM metadata WHERE \"key\" IN ('cached_stats', 'stats_calculated_at')"))

    # Counts only — never chat IDs, message text, or user IDs (PII).
    logger.info(
        "Migration 019: deleted %d phantom chat-action message rows "
        "(children removed: %d reactions, %d media, %d message_versions)",
        deleted,
        child_counts["reactions"],
        child_counts["media"],
        child_counts["message_versions"],
    )


def downgrade() -> None:
    """No-op: intentionally irreversible.

    The deleted rows were fabricated junk (phantom chat-action rows with
    wall-clock IDs) that never corresponded to a real Telegram message, so there
    is no source of truth to restore them from. Re-applying ``upgrade`` is safe
    and idempotent.
    """
    pass
