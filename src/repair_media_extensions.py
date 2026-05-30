"""Repair media files corrupted by the pre-7.11.3 download finalize bug (#175).

Before 7.11.3, downloads went to a temp path ``{file_name}.{pid}.{task_id}.part``.
Telethon treats the trailing ``.part`` as the extension and returns that path
verbatim, and the old finalize step stripped only ``.part`` — leaving files
named like ``1234_video.mp4.7.140234567890`` on disk.

The corruption appears two ways:

* No-dedup installs: the chat-folder file itself carries the corrupt name and
  ``media.file_path`` points at it.
* Dedup installs (default): the chat-folder entry is a symlink with the clean
  name pointing at ``_shared/<hh>/<clean>.<pid>.<task>`` (corrupt blob name);
  ``media.file_path`` already stores the clean chat-folder path.

This pass is anchored to the DB's clean ``file_name`` so detection has zero
false positives: a path is only treated as corrupt when its basename equals
``file_name`` plus a ``.<int>.<int>`` tail. It never deletes anything (per
project safety rules) and is crash-safe and idempotent via a marker file.
"""

import logging
import os
import re

from .message_utils import compute_file_hash

logger = logging.getLogger(__name__)

REPAIR_MARKER = ".repaired-175"

# Secondary guard: pid is a small int, id()-derived task_id is large.
_CORRUPT_TAIL = re.compile(r"\.\d{1,7}\.\d{4,}$")


def _is_corrupt_basename(basename: str, clean_name: str) -> bool:
    """True when ``basename`` is ``clean_name`` plus a ``.<pid>.<task>`` tail.

    Anchored to the known-good clean name to avoid misclassifying legitimate
    filenames that merely end in digits.
    """
    if basename == clean_name:
        return False
    prefix = clean_name + "."
    if not basename.startswith(prefix):
        return False
    tail = basename[len(clean_name) :]  # includes leading dot, e.g. ".7.1402..."
    return bool(_CORRUPT_TAIL.fullmatch(tail))


def _repair_direct_file(corrupt_path: str, clean_path: str) -> bool:
    """No-dedup case: rename the corrupt on-disk file to its clean name.

    Returns True when ``clean_path`` ends up holding the intended content.
    """
    if os.path.lexists(clean_path):
        # A clean file already exists. Keep it; only adopt it when content matches.
        if os.path.isfile(clean_path) and os.path.isfile(corrupt_path):
            if compute_file_hash(clean_path) == compute_file_hash(corrupt_path):
                return True  # redundant corrupt copy; leave it untouched (no delete)
        return False  # genuine distinct file or unreadable — do not clobber
    os.replace(corrupt_path, clean_path)
    return True


def _repair_symlink_blob(link_path: str, shared_dir: str) -> bool:
    """Dedup case: rename the corrupt shared blob and retarget the chat symlink.

    The symlink basename is already clean; only its target blob name is corrupt.
    Renames the blob FIRST (creating the clean truth), then retargets the link,
    so a crash in between leaves a dangling link that re-resolves on the next
    run (the clean blob is found and the link is simply re-pointed).
    """
    link_dir = os.path.dirname(link_path)
    clean_name = os.path.basename(link_path)
    target = os.readlink(link_path)
    blob_path = os.path.normpath(os.path.join(link_dir, target))
    blob_dir = os.path.dirname(blob_path)
    blob_name = os.path.basename(blob_path)

    if not _is_corrupt_basename(blob_name, clean_name):
        return False

    clean_blob = os.path.join(blob_dir, clean_name)

    if os.path.lexists(clean_blob):
        if os.path.isfile(clean_blob) and os.path.isfile(blob_path):
            if compute_file_hash(clean_blob) != compute_file_hash(blob_path):
                return False  # distinct content under the clean name — skip
        # Clean blob already present (matching or our own prior run): just relink.
    elif os.path.isfile(blob_path):
        os.replace(blob_path, clean_blob)
    else:
        return False  # corrupt blob missing and no clean blob — nothing to do

    new_rel = os.path.relpath(clean_blob, link_dir)
    os.unlink(link_path)
    os.symlink(new_rel, link_path)
    return True


async def repair_media_extensions(media_path: str, db: object) -> int:
    """Repair files corrupted by #175. Returns the number of records repaired.

    Idempotent: a marker under ``_shared/`` short-circuits subsequent runs.
    Never deletes files; orphan ``.part`` artifacts are only counted/logged.
    """
    if not media_path or not os.path.isdir(media_path):
        return 0

    shared_dir = os.path.join(media_path, "_shared")
    marker = os.path.join(shared_dir, REPAIR_MARKER)
    if os.path.exists(marker):
        return 0

    try:
        records = await db.get_media_for_verification()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Media repair skipped — could not read media records: {e}")
        return 0

    repaired = 0
    skipped = 0
    for record in records:
        file_path = record.get("file_path")
        clean_name = record.get("file_name")
        media_id = record.get("id")
        if not file_path or not clean_name or media_id is None:
            continue

        try:
            if os.path.islink(file_path):
                # Dedup case: link name is clean, blob name may be corrupt.
                if os.path.basename(file_path) != clean_name:
                    continue
                if _repair_symlink_blob(file_path, shared_dir):
                    repaired += 1
                continue

            # No-dedup case: the recorded path itself may be corrupt.
            if _is_corrupt_basename(os.path.basename(file_path), clean_name):
                clean_path = os.path.join(os.path.dirname(file_path), clean_name)
                if _repair_direct_file(file_path, clean_path):
                    await db.update_media_file_path(media_id, clean_path)
                    repaired += 1
                else:
                    skipped += 1
        except OSError:
            skipped += 1

    orphan_parts = _count_orphan_parts(shared_dir)

    if repaired or skipped or orphan_parts:
        logger.info(
            "Media extension repair: %d repaired, %d skipped, %d orphan .part files left in place",
            repaired,
            skipped,
            orphan_parts,
        )

    _write_marker(marker)
    return repaired


def _count_orphan_parts(shared_dir: str) -> int:
    """Count leftover ``.part`` artifacts without deleting them."""
    count = 0
    for _root, _dirs, files in os.walk(shared_dir):
        count += sum(1 for f in files if f.endswith(".part"))
    return count


def _write_marker(marker_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, "w") as f:
            f.write("media extension repair (#175) complete\n")
    except OSError as e:
        logger.error(f"Failed to write repair marker: {e}")
