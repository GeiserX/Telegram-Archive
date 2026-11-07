"""Shared utilities for legacy media path resolution.

Centralizes the Telegram marked-ID convention so it's defined once
and used consistently across serve_media, thumbnails, and ACL checks.
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

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


# A drive-qualified Windows path: "C:/x", "C:\\x" or the drive-relative "C:x".
WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def media_relative_path(file_path: str | None, media_root: Path | None) -> str | None:
    """Normalize a media row's file_path to a media-root-relative path, or None.

    Rows hold whatever the writer stored. Modern archives store a relative path;
    older ones stored an absolute path, with whichever separator the machine that
    wrote it uses. Three shapes have to end up as the same root-relative tail:

    - a relative path, from either platform;
    - an absolute path under the CURRENT media root;
    - an absolute path under a DIFFERENT root, which is an archive that has been
      moved to another directory or drive (#438). The media directory is always
      named after the root's own last component (config builds it as
      ``<BACKUP_PATH>/media``), so whatever follows that component is the tail
      the current root expects, and a moved archive keeps working without
      rewriting every row in the database.

    Deliberately lexical: no filesystem call. This runs for every media row of
    every message list, and the media root is a network or FUSE mount on most
    installs, where one ``Path.resolve()`` costs ~260 us against ~0.07 us for a
    string compare — 19 ms on a 50-row page, on an endpoint the viewer polls
    every three seconds. Nothing here is a security bound: the bound is applied
    where the bytes are read, by resolve_media_file and ensure_thumbnail, which
    both resolve the joined path and re-check it against the root.
    """
    if not file_path or not media_root:
        return None

    # Whichever separator wrote the row. A stored file NAME never contains a
    # backslash — sanitize_media_filename collapses them before the name is
    # written — so a backslash here is always a directory separator.
    path = file_path.replace("\\", "/")
    root = media_root.as_posix()

    # "/x", "C:/x" and "C:x" are all absolute enough to need the root stripped.
    if path.startswith("/") or WINDOWS_DRIVE_PREFIX.match(path):
        if path.startswith(root + "/"):
            path = path[len(root) + 1 :]
        else:
            anchor = "/" + media_root.name + "/"
            cut = path.rfind(anchor)
            if cut < 0:
                return None
            path = path[cut + len(anchor) :]

    if not path or path.startswith("/") or ".." in path.split("/"):
        return None
    return path


def resolve_media_file(relative_path: str, media_root: Path) -> Path | None:
    """Resolve a root-relative media path to a real file inside the media root.

    Same containment contract as the pre-8.0 serve path: resolve(strict=True),
    the legacy positive/negative folder fallback for pre-v4.0.5 archives, and
    the is_relative_to bound that keeps a symlink from escaping the root.
    Returns the resolved Path or None.
    """
    candidate = media_root / relative_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError, ValueError:
        # Legacy fallback: pre-v4.0.5 paths used positive IDs, disk uses negative marked IDs.
        # Try alternate folder names: X→-X (basic group), X→-100X (channel/supergroup)
        parts = relative_path.split("/", 1)
        resolved = None
        if len(parts) == 2:
            folder, rest = parts
            for alt in legacy_folder_alternates(folder):
                try:
                    resolved = (media_root / alt / rest).resolve(strict=True)
                    logger.debug("Legacy fallback: served media via alternate folder resolution")
                    break
                except OSError, ValueError, RuntimeError:
                    continue
        if resolved is None:
            return None
    if not resolved.is_relative_to(media_root):
        return None
    if not resolved.is_file():
        return None
    return resolved
