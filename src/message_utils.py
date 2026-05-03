"""Shared message processing utilities used by backup and listener modules."""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)


async def deduplicate_shared_file(
    db: object,
    shared_file_path: str,
    shared_dir: str,
) -> tuple[str, str | None, bool]:
    """Check if newly downloaded content already exists in the shared store.

    Computes a SHA-256 hash, queries the DB for a match, and if found,
    removes the duplicate file and returns the path to the existing one.

    Returns (resolved_path, content_hash, reused_existing). The third
    element is True when the path points to a pre-existing canonical blob
    that must NOT be moved/deleted by the caller.
    """
    content_hash = compute_file_hash(shared_file_path)
    if not content_hash:
        return shared_file_path, content_hash, False

    existing = await db.find_media_by_content_hash(content_hash)
    if not existing or not existing.get("file_name"):
        return shared_file_path, content_hash, False

    existing_shared = os.path.join(shared_dir, existing["file_name"])

    # Path traversal guard: resolved path must stay within shared_dir
    real_shared_dir = os.path.realpath(shared_dir)
    real_existing = os.path.realpath(existing_shared)
    if not (real_existing == real_shared_dir or real_existing.startswith(real_shared_dir + os.sep)):
        return shared_file_path, content_hash, False

    if not os.path.exists(existing_shared) or existing_shared == shared_file_path:
        return shared_file_path, content_hash, False

    # TOCTOU-safe removal: another process may have already cleaned up
    try:
        os.remove(shared_file_path)
    except FileNotFoundError:
        pass

    logger.debug(f"Content-hash dedup: {os.path.basename(shared_file_path)} matches existing {existing['file_name']}")
    return existing_shared, content_hash, True


def compute_file_hash(filepath: str, chunk_size: int = 65536) -> str | None:
    """Compute SHA-256 hex digest of a file, following symlinks."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def extract_topic_id(message: object) -> int | None:
    """Extract forum topic ID from a Telegram message's reply_to metadata.

    Forum messages carry the topic ID in reply_to.reply_to_top_id.
    When that field is absent (e.g. topic-creating service messages),
    reply_to.reply_to_msg_id is used as a fallback.

    Returns None for non-forum messages or messages without reply_to.
    """
    if not message.reply_to or not getattr(message.reply_to, "forum_topic", False):
        return None
    topic_id = getattr(message.reply_to, "reply_to_top_id", None)
    if topic_id is None:
        topic_id = getattr(message.reply_to, "reply_to_msg_id", None)
    return topic_id
