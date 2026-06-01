"""Shared message processing utilities used by backup and listener modules."""

import asyncio
import errno
import hashlib
import logging
import os

logger = logging.getLogger(__name__)


def sanitize_media_filename(name: str) -> str:
    """Strip path components from an attacker-controlled media filename.

    Telegram document ``file_name`` attributes are remote-controlled and may
    contain ``/``, ``\\``, or ``..`` segments. Left unchecked these survive into
    ``media.file_name`` and later into on-disk ``os.replace`` targets, allowing a
    write outside the media store (#175 repair pass made this reachable). Collapse
    to a bare basename and neutralise residual traversal/separators.
    """
    name = name.replace("\\", "/")
    name = os.path.basename(name)
    name = name.replace("\x00", "")
    if name in ("", ".", ".."):
        return "_"
    return name


def get_shared_file_path(shared_dir: str, file_name: str, content_hash: str | None) -> str:
    """Build the sharded path for a file in the shared store.

    Uses the first 2 hex characters of the content_hash as a subdirectory
    (256 buckets). Falls back to flat layout when no hash is available.
    """
    file_name = os.path.basename(file_name)
    if content_hash and len(content_hash) >= 2:
        bucket = content_hash[:2]
        return os.path.join(shared_dir, bucket, file_name)
    return os.path.join(shared_dir, file_name)


def resolve_shared_file_path(shared_dir: str, file_name: str, content_hash: str | None) -> str | None:
    """Find an existing file in the shared store, checking sharded then flat.

    Returns the path if found (via lexists, so symlinks count), else None.
    """
    file_name = os.path.basename(file_name)
    # Check sharded location first
    if content_hash and len(content_hash) >= 2:
        sharded = os.path.join(shared_dir, content_hash[:2], file_name)
        if os.path.lexists(sharded):
            return sharded
    else:
        # Hash unknown — scan shard buckets for the file
        try:
            for entry in os.scandir(shared_dir):
                if entry.is_dir() and len(entry.name) == 2:
                    candidate = os.path.join(entry.path, file_name)
                    if os.path.lexists(candidate):
                        return candidate
        except OSError:
            pass
    # Fallback: flat layout (pre-sharding installs)
    flat = os.path.join(shared_dir, file_name)
    if os.path.lexists(flat):
        return flat
    return None


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

    existing_hash = existing.get("content_hash", "")
    existing_shared = resolve_shared_file_path(shared_dir, existing["file_name"], existing_hash)
    if not existing_shared:
        return shared_file_path, content_hash, False

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


def finalize_atomic_download(actual_path: str | None, temporary_path: str, fallback_path: str) -> str | None:
    """Move a finished download to its intended filename.

    The temp path carries a unique ``.{pid}.{task}.part`` suffix so concurrent
    downloads never collide. Telethon's ``_get_proper_filename`` treats that
    trailing ``.part`` as the file extension and returns the temp path verbatim,
    so the produced file is always one of ``actual_path`` / ``temporary_path``.
    We rename it to the caller-provided ``fallback_path`` (the intended clean
    name, already carrying the correct extension), instead of deriving a name
    from the temp path — stripping only ``.part`` left names like
    ``video.mp4.7.140234567890`` on disk. See issue #175.
    """
    source = actual_path if (actual_path and os.path.exists(actual_path)) else None
    if source is None and os.path.exists(temporary_path):
        source = temporary_path
    if source is None:
        return None

    if source != fallback_path:
        os.replace(source, fallback_path)

    # Clean up a stale temp artifact if Telethon wrote the real file elsewhere.
    if temporary_path not in (fallback_path, source) and os.path.exists(temporary_path):
        try:
            os.remove(temporary_path)
        except OSError:
            pass

    return fallback_path if os.path.exists(fallback_path) else None


async def download_and_shard_media(
    db,
    download_coro,
    shared_dir: str,
    chat_media_dir: str,
    file_name: str,
    file_path: str,
    logger: logging.Logger,
) -> tuple[str | None, str | None]:
    """Download media to sharded shared store, create symlink in chat dir.

    Args:
        db: Database adapter (for deduplicate_shared_file)
        download_coro: Async callable that takes a tmp_path and returns actual path
        shared_dir: Path to _shared/ directory
        chat_media_dir: Chat's media directory (for relative symlinks)
        file_name: Media filename
        file_path: Full path where chat-dir symlink should be created
        logger: Logger instance

    Returns:
        (shared_file_path, content_hash) or (None, None) on failure
    """
    # Resolve existing file in shared store (sharded or flat fallback)
    shared_file_path = resolve_shared_file_path(shared_dir, file_name, None)

    if os.path.lexists(file_path):
        # Chat symlink already exists — resolve hash if possible
        content_hash = None
        if shared_file_path and os.path.exists(shared_file_path):
            content_hash = compute_file_hash(shared_file_path)
        return shared_file_path, content_hash

    if shared_file_path:
        # File exists in shared — create symlink. Hash only when target resolves.
        content_hash = compute_file_hash(shared_file_path) if os.path.exists(shared_file_path) else None
        try:
            rel_path = os.path.relpath(shared_file_path, chat_media_dir)
            try:
                os.symlink(rel_path, file_path)
            except FileExistsError:
                pass
            except OSError as e:
                if e.errno == errno.EEXIST:
                    if os.path.lexists(file_path):
                        os.unlink(file_path)
                    os.symlink(rel_path, file_path)
                else:
                    raise
            logger.debug(f"Created symlink for deduplicated media: {file_name}")
        except OSError as e:
            logger.warning(f"Symlink not supported, using direct path: {e}")
            import shutil

            shutil.copy2(shared_file_path, file_path)
        return shared_file_path, content_hash

    # First time seeing this file — download to unique .part then shard
    task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
    tmp_shared_file_path = os.path.join(shared_dir, f"{file_name}.{os.getpid()}.{task_id}.part")
    if os.path.exists(tmp_shared_file_path):
        os.remove(tmp_shared_file_path)

    actual_path = await download_coro(tmp_shared_file_path)
    tmp_shared_file_path = finalize_atomic_download(
        actual_path if isinstance(actual_path, str) else None,
        tmp_shared_file_path,
        os.path.join(shared_dir, file_name),
    )
    if not tmp_shared_file_path or not os.path.exists(tmp_shared_file_path):
        logger.warning("Media download did not produce a file")
        return None, None
    logger.debug(f"Downloaded media to shared: {file_name}")

    # Content-hash dedup: check if identical content already exists
    tmp_shared_file_path, content_hash, reused = await deduplicate_shared_file(db, tmp_shared_file_path, shared_dir)

    # Move to sharded location if we own this file (not reused)
    if not reused and content_hash:
        actual_name = os.path.basename(tmp_shared_file_path)
        final_shared = get_shared_file_path(shared_dir, actual_name, content_hash)
        os.makedirs(os.path.dirname(final_shared), exist_ok=True)
        if tmp_shared_file_path != final_shared:
            os.replace(tmp_shared_file_path, final_shared)
        shared_file_path = final_shared
    else:
        shared_file_path = tmp_shared_file_path

    # Create symlink in chat directory (hardened for concurrent tasks)
    try:
        rel_path = os.path.relpath(shared_file_path, chat_media_dir)
        try:
            os.symlink(rel_path, file_path)
        except FileExistsError:
            # Another concurrent task already created this symlink — benign
            pass
        except OSError as e:
            if e.errno == errno.EEXIST:
                # Retry after removing stale entry
                if os.path.lexists(file_path):
                    os.unlink(file_path)
                os.symlink(rel_path, file_path)
            else:
                raise
    except OSError as e:
        logger.warning(f"Symlink not supported, using direct path: {e}")
        import shutil

        if reused:
            shutil.copy2(shared_file_path, file_path)
        else:
            shutil.move(shared_file_path, file_path)

    return shared_file_path, content_hash


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
