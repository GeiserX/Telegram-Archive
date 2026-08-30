"""Shared utilities for legacy media path resolution.

Centralizes the Telegram marked-ID convention so it's defined once
and used consistently across serve_media, thumbnails, and ACL checks.
"""

import os

CHANNEL_ID_OFFSET: int = 1_000_000_000_000

IMAGE_EXTENSIONS: set[str] = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff"}
VIDEO_EXTENSIONS: set[str] = {"mp4", "mkv", "avi", "mov", "webm", "m4v", "3gp"}
THUMBNAIL_EXTENSIONS: set[str] = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


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

    Used by migration 013 and tests (not imported at web runtime).
    """
    if chat_id >= 0:
        return None
    raw = -chat_id
    if raw > CHANNEL_ID_OFFSET:
        return str(raw - CHANNEL_ID_OFFSET)
    return str(raw)


def resolve_stored_media_path(file_path: str | None, media_root: str) -> str | None:
    """Absolute on-disk path for a stored ``Media.file_path``, or None.

    Two shapes exist in the wild and both are legitimate:

    * **absolute** — what the API sweep and the realtime listener write, since
      ``config.media_path`` is an ``os.path.abspath`` (config.py:449, :631).
    * **media-root-relative** (``"{chat_id}/{name}"``) — what the Telegram
      Desktop importer writes (telegram_import.py:1131), because the viewer
      serves media by root-relative path.

    The capture layer only ever knew the first shape, so it stat()ed the stored
    value directly and every imported row resolved against the process CWD
    (``/app``) instead of the media root: the file was judged missing and
    re-downloaded, or skipped by a delete that still dropped its row (#310).

    An absolute value is returned unchanged — byte-identical to the behaviour
    that shipped before this helper, so nothing about swept archives moves. A
    relative value is joined to the media root and must stay inside it; a value
    that escapes the root yields None rather than a path outside the archive,
    because callers delete and replace what this returns.
    """
    if not file_path:
        return None
    if os.path.isabs(file_path):
        return file_path
    root = os.path.abspath(media_root)
    candidate = os.path.abspath(os.path.join(root, file_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate
