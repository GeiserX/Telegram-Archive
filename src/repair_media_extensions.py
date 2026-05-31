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

The marker is written only when no row was *deferred* — i.e. only when no
transient failure (filesystem error, DB write error) left repairable work
undone. Permanent no-ops (a genuinely distinct file already under the clean
name) do not block the marker, since retrying them can never succeed.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Protocol

from .message_utils import compute_file_hash

logger = logging.getLogger(__name__)

REPAIR_MARKER = ".repaired-175"

# Secondary guard: the task_id (id()-derived) is always large; the pid may be
# any positive int. Anchoring to the clean name is what guarantees zero false
# positives, so the pid bound is intentionally permissive.
_CORRUPT_TAIL = re.compile(r"\.\d+\.\d{4,}$")


class _MediaRepairDB(Protocol):
    """The narrow DB surface the repair pass depends on."""

    def iter_media_paths_for_repair(self, batch_size: int = ...) -> AsyncIterator[list[dict]]: ...

    async def update_media_file_path(self, media_id: object, file_path: str) -> None: ...


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

    Returns True when ``clean_path`` ends up holding the intended content,
    which signals the caller to repoint ``media.file_path`` at it. Raises
    ``OSError`` on a transient filesystem failure so the driver can defer it.
    """
    if os.path.lexists(clean_path):
        # A clean file already exists.
        if not os.path.lexists(corrupt_path):
            # The user already renamed it by hand (the #175 reporter's own
            # workaround) — adopt the clean file so the DB row is corrected.
            return True
        if os.path.isfile(clean_path) and os.path.isfile(corrupt_path):
            if compute_file_hash(clean_path) == compute_file_hash(corrupt_path):
                return True  # redundant corrupt copy; leave it untouched (no delete)
        return False  # genuine distinct file or unreadable — do not clobber
    if not os.path.lexists(corrupt_path):
        return False  # nothing on disk under either name
    os.replace(corrupt_path, clean_path)
    return True


def _repair_symlink_blob(link_path: str, shared_dir: str) -> bool:
    """Dedup case: rename the corrupt shared blob and retarget the chat symlink.

    The symlink basename is already clean; only its target blob name is corrupt.
    Renames the blob FIRST (creating the clean truth), then retargets the link,
    so a crash in between leaves a dangling link that re-resolves on the next
    run (the clean blob is found and the link is simply re-pointed). The same
    re-resolution heals sibling symlinks (other chats sharing the dedup'd blob)
    whose targets still carry the old corrupt name after the blob was renamed.

    Raises ``OSError`` on a transient filesystem failure so the driver defers it.
    """
    link_dir = os.path.dirname(link_path)
    clean_name = os.path.basename(link_path)
    target = os.readlink(link_path)
    blob_path = os.path.normpath(os.path.join(link_dir, target))
    blob_dir = os.path.dirname(blob_path)
    blob_name = os.path.basename(blob_path)

    # Only ever rename blobs that live inside our own _shared store; never touch
    # externally managed targets (e.g. git-annex) the symlink may point at.
    # realpath (not normpath) so a symlinked path component can't smuggle the
    # target outside _shared past the containment check.
    shared_root = os.path.realpath(shared_dir)
    real_blob_dir = os.path.realpath(blob_dir)
    if real_blob_dir != shared_root and not real_blob_dir.startswith(shared_root + os.sep):
        return False

    if not _is_corrupt_basename(blob_name, clean_name):
        return False

    clean_blob = os.path.join(blob_dir, clean_name)

    if os.path.lexists(clean_blob):
        if os.path.isfile(clean_blob) and os.path.isfile(blob_path):
            if compute_file_hash(clean_blob) != compute_file_hash(blob_path):
                return False  # distinct content under the clean name — skip
        # Clean blob already present (matching content, our own prior run, or a
        # sibling symlink that already renamed it): just relink.
    elif os.path.isfile(blob_path):
        os.replace(blob_path, clean_blob)
    else:
        return False  # corrupt blob missing and no clean blob — nothing to do

    new_rel = os.path.relpath(clean_blob, link_dir)
    os.unlink(link_path)
    os.symlink(new_rel, link_path)
    return True


def _repair_records_sync(records: list[dict], shared_dir: str) -> tuple[int, int, list[tuple]]:
    """Do the blocking filesystem repair work off the event loop.

    Returns ``(repaired, deferred, pending_db_updates)`` where
    ``pending_db_updates`` is a list of ``(media_id, clean_path)`` rows whose
    ``media.file_path`` must be repointed by the async caller. ``deferred``
    counts rows that hit a transient filesystem error and should be retried on
    a later run (these block the idempotency marker).
    """
    repaired = 0
    deferred = 0
    pending_db_updates: list[tuple] = []

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
                    pending_db_updates.append((media_id, clean_path))
                    repaired += 1
        except OSError:
            deferred += 1

    return repaired, deferred, pending_db_updates


async def repair_media_extensions(media_path: str, db: _MediaRepairDB) -> int:
    """Repair files corrupted by #175. Returns the number of records repaired.

    Idempotent: a marker under ``_shared/`` short-circuits subsequent runs, but
    is written only when nothing was deferred, so transient failures are retried
    on the next run instead of being permanently suppressed. Never deletes files.
    """
    if not media_path or not os.path.isdir(media_path):
        return 0

    shared_dir = os.path.join(media_path, "_shared")
    marker = os.path.join(shared_dir, REPAIR_MARKER)
    if os.path.exists(marker):
        return 0

    repaired = 0
    deferred = 0

    # Stream the media table in keyset-paginated batches. Materializing the whole
    # table OOM-killed the 256m backup container on large archives (#175 v7.11.3
    # crash loop), so we hold only one batch in memory at a time.
    try:
        batches = db.iter_media_paths_for_repair()
        async for records in batches:
            # Filesystem walks, hashing, and renames are blocking; keep them off
            # the event loop so the concurrently-running listener is not starved.
            batch_repaired, batch_deferred, pending_db_updates = await asyncio.to_thread(
                _repair_records_sync, records, shared_dir
            )
            repaired += batch_repaired
            deferred += batch_deferred

            # Repoint media.file_path for the no-dedup rows we renamed. A failure
            # here leaves the file renamed but the row stale; defer so the marker
            # is withheld and the next run's adoption branch repoints it.
            for media_id, clean_path in pending_db_updates:
                try:
                    await db.update_media_file_path(media_id, clean_path)
                except Exception as e:
                    logger.warning("Media repair: DB repoint failed for one record (%s)", type(e).__name__)
                    deferred += 1
                    repaired -= 1
    except Exception as e:
        logger.warning("Media repair aborted — could not read media records (%s)", type(e).__name__)
        return repaired

    if repaired or deferred:
        logger.info(
            "Media extension repair: %d repaired, %d deferred for retry",
            repaired,
            deferred,
        )

    # Only seal the pass when no repairable work was left behind by a transient
    # failure. Permanent no-ops (distinct content under the clean name) do not
    # set ``deferred`` and therefore never block the marker.
    if deferred == 0:
        _write_marker(marker)
    return repaired


def _write_marker(marker_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(marker_path), exist_ok=True)
        with open(marker_path, "w") as f:
            f.write("media extension repair (#175) complete\n")
    except OSError as e:
        logger.error("Failed to write repair marker (%s)", type(e).__name__)
