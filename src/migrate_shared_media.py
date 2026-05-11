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

    Returns the number of files migrated.
    """
    shared_dir = os.path.join(media_path, "_shared")
    if not os.path.isdir(shared_dir):
        return 0

    marker = os.path.join(shared_dir, SHARD_MARKER)
    if os.path.exists(marker):
        return 0

    # Only process regular files directly in _shared/.
    # Symlinks are excluded: moving a symlink to a subdirectory breaks its
    # relative target (e.g. git-annex pointers like ../../.git/annex/objects/...).
    # They remain in flat _shared/ and are found via the fallback in
    # resolve_shared_file_path().
    # .part files are excluded: they are in-progress downloads.
    flat_files = []
    try:
        for e in os.scandir(shared_dir):
            if e.is_file(follow_symlinks=False) and not e.name.startswith(".") and not e.name.endswith(".part"):
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
    for entry in flat_files:
        src_path = entry.path

        content_hash = _compute_hash(src_path)
        if not content_hash:
            continue

        bucket = content_hash[:2]
        dest_dir = os.path.join(shared_dir, bucket)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, entry.name)

        if os.path.lexists(dest_path):
            # Already exists in shard — remove flat duplicate
            if os.path.isfile(dest_path):
                os.remove(src_path)
            continue

        # Relink symlinks BEFORE moving: if crash occurs between relink and move,
        # symlinks are dangling but file is still in flat_files on restart → full retry.
        _relink_chat_symlinks(media_path, shared_dir, entry.name, dest_path, chat_dirs)

        os.replace(src_path, dest_path)
        migrated += 1

    _write_marker(marker)
    logger.info(f"Migration complete: {migrated} files moved to sharded layout")
    return migrated


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
        logger.error(f"Failed to write migration marker: {e}")
