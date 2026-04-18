"""Shared message processing utilities used by backup and listener modules."""


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
