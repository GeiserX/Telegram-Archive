"""Shared utilities for legacy media path resolution.

Centralizes the Telegram marked-ID convention so it's defined once
and used consistently across serve_media, thumbnails, and ACL checks.
"""

CHANNEL_ID_OFFSET: int = 1_000_000_000_000


def legacy_folder_alternates(folder: str) -> list[str]:
    """Return alternate folder names for legacy positive/negative ID paths.

    Forward (positive folder → possible negative marked IDs on disk):
        "1234567890" → ["-1234567890", "-1001234567890"]

    Reverse (negative folder → possible old positive folder on disk):
        "-1234567890"    → ["1234567890"]           (basic group)
        "-1001234567890" → ["1234567890"]           (channel)
    """
    try:
        if not folder.startswith("-"):
            folder_int = int(folder)
            if folder_int <= 0:
                return []
            return [f"-{folder}", str(-(CHANNEL_ID_OFFSET + folder_int))]
        folder_int = int(folder)
    except ValueError:
        return []
    raw = -folder_int
    if raw > CHANNEL_ID_OFFSET:
        return [str(raw - CHANNEL_ID_OFFSET)]
    return [str(raw)]


def legacy_marked_chat_ids(positive_id: int) -> list[int]:
    """Return possible marked chat_ids for a legacy positive folder ID.

    Used by ACL checks to determine if a user has access to a chat
    referenced by its old positive folder name.
    """
    return [-positive_id, -(CHANNEL_ID_OFFSET + positive_id)]


def derive_stale_folder(chat_id: int) -> str | None:
    """Derive the old positive folder name from a marked chat_id.

    Basic groups: chat_id = -X  →  old folder = "X"
    Channels:     chat_id = -(10^12 + X)  →  old folder = "X"
    Users:        chat_id > 0  →  no mismatch possible, return None
    """
    if chat_id >= 0:
        return None
    raw = -chat_id
    if raw > CHANNEL_ID_OFFSET:
        return str(raw - CHANNEL_ID_OFFSET)
    return str(raw)
