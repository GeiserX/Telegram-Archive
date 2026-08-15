"""Auto-migrate flat _shared/ layout to sharded (hash-prefix) layout.

On startup, scans _shared/ for files directly in the root (not in a
2-char hex subdirectory). For each file, computes SHA-256, moves it to
_shared/<hash[:2]>/<filename>, and updates any chat-dir symlinks that
pointed at the old flat location.

Idempotent: files already in shard buckets are skipped.
"""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

SHARD_MARKER = ".sharded"


def _compute_hash(filepath: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def migrate_shared_media(media_path: str) -> int:
    """Migrate flat _shared/ files into hash-prefix sharded subdirectories.

    Returns the number of files migrated. Per-entry filesystem errors are
    contained and counted as deferred — this runs before the scheduler and the
    listener start, so one unreadable file must never keep the archiver from
    running. The idempotency marker is withheld while anything is deferred, so
    the leftovers are retried on the next start instead of being abandoned.
    """
    shared_dir = os.path.join(media_path, "_shared")
    if not os.path.isdir(shared_dir):
        return 0

    marker = os.path.join(shared_dir, SHARD_MARKER)
    if os.path.exists(marker):
        return 0

    flat_files = []
    try:
        for e in os.scandir(shared_dir):
            if (
                (e.is_file(follow_symlinks=False) or e.is_symlink())
                and not e.name.startswith(".")
                and not e.name.endswith(".part")
            ):
                flat_files.append(e)
    except OSError:
        return 0

    if not flat_files:
        # No flat files — mark as migrated
        _write_marker(marker)
        return 0

    logger.info(f"Migrating {len(flat_files)} files from flat _shared/ to sharded layout...")

    # Compute chat directories once (not per file)
    try:
        chat_dirs = [e.path for e in os.scandir(media_path) if e.is_dir() and not e.name.startswith("_")]
    except OSError:
        chat_dirs = []

    migrated = 0
    deferred = 0
    for entry in flat_files:
        src_path = entry.path

        try:
            content_hash = _compute_hash(src_path)
            if not content_hash:
                # Unreadable — e.g. a symlink whose target is not mounted yet.
                # Leave it flat and retry on a later start.
                deferred += 1
                continue

            bucket = content_hash[:2]
            dest_dir = os.path.join(shared_dir, bucket)
            os.makedirs(dest_dir, exist_ok=True)
            dest_path = os.path.join(dest_dir, entry.name)

            if os.path.lexists(dest_path):
                # Already exists in shard — remove flat duplicate
                if os.path.isfile(dest_path):
                    os.remove(src_path)
                else:
                    # Destination is not usable content (e.g. a dangling link);
                    # the flat copy stays, so this entry is still outstanding.
                    deferred += 1
                continue

            # Relink symlinks BEFORE moving: if crash occurs between relink and move,
            # symlinks are dangling but file is still in flat_files on restart → full retry.
            _relink_chat_symlinks(media_path, shared_dir, entry.name, dest_path, chat_dirs)

            _relocate_into_bucket(shared_dir, src_path, dest_path, entry.is_symlink())
            migrated += 1
        except OSError:
            # One bad file must not abort the migration, and must not propagate:
            # the caller exits the process on any exception, before the scheduler
            # and listener are started.
            deferred += 1

    if deferred:
        # Count only: a media path carries the chat-id folder.
        logger.warning(f"Sharding migration deferred {deferred} entries; will retry on next start")
    else:
        _write_marker(marker)
    logger.info(f"Migration complete: {migrated} files moved to sharded layout")
    return migrated


def _relocate_into_bucket(shared_dir: str, src_path: str, dest_path: str, is_symlink: bool) -> None:
    """Move one flat _shared/ entry into its shard bucket.

    A symlink cannot be moved verbatim: a RELATIVE target is resolved against the
    link's own directory, so relocating the link one level deeper re-points it one
    directory too high and it dangles for good. Recreate it with the target
    rewritten against the bucket directory instead. Absolute targets move as-is.
    """
    if is_symlink:
        target = os.readlink(src_path)
        if not os.path.isabs(target):
            target = os.path.relpath(os.path.join(shared_dir, target), os.path.dirname(dest_path))
        os.symlink(target, dest_path)
        os.unlink(src_path)
        return

    os.replace(src_path, dest_path)


def _relink_chat_symlinks(
    media_path: str, shared_dir: str, file_name: str, new_target: str, chat_dirs: list[str]
) -> None:
    """Find and update chat-dir symlinks that pointed at the old flat shared path."""
    old_rel_suffix = os.path.join("_shared", file_name)

    for chat_dir in chat_dirs:
        link_path = os.path.join(chat_dir, file_name)
        if not os.path.islink(link_path):
            continue

        target = os.readlink(link_path)
        # Check if this symlink points to the old flat location
        if target.endswith(old_rel_suffix) or (
            os.path.basename(os.path.dirname(target)) == "_shared" and os.path.basename(target) == file_name
        ):
            new_rel = os.path.relpath(new_target, chat_dir)
            os.unlink(link_path)
            os.symlink(new_rel, link_path)


def _write_marker(marker_path: str) -> None:
    try:
        with open(marker_path, "w") as f:
            f.write("sharding migration complete\n")
    except OSError as e:
        # Type only: OSError names the marker path, which sits under the
        # media root alongside the chat-id folders.
        logger.error(f"Failed to write migration marker: {type(e).__name__}")
