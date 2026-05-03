"""Shared message processing utilities used by backup and listener modules."""

import hashlib


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
