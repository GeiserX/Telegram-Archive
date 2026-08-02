"""Shared message processing utilities used by backup and listener modules."""

import asyncio
import errno
import hashlib
import logging
import mimetypes
import os
import re
import stat
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def utcnow_naive() -> datetime:
    """Return current UTC time without tzinfo, for naive DB datetime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def sender_display_name(sender: object | None) -> str | None:
    """Return a trimmed capture-time display name for a Telegram sender."""
    if sender is None:
        return None

    first_name = getattr(sender, "first_name", None)
    last_name = getattr(sender, "last_name", None)
    name_parts = [value.strip() for value in (first_name, last_name) if isinstance(value, str) and value.strip()]
    if name_parts:
        return " ".join(name_parts)

    for attribute in ("title", "username"):
        value = getattr(sender, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_sender_display_name(
    sender_name: str | None,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> str | None:
    """Resolve a stored message row's sender to a display name.

    Single source of truth for "who sent this row" on the read side, so the
    message list, the media gallery, the export stream and the reply-quote block
    can never drift apart. Precedence matches the viewer's getSenderName: the
    capture-time ``messages.sender_name`` snapshot first (it is what was true
    when the message arrived), then the user's current first/last name, then the
    username. Returns ``None`` when nothing is known — each caller picks its own
    placeholder ("Unknown", the numeric id, or nothing at all).
    """
    for value in (sender_name, f"{first_name or ''} {last_name or ''}", username):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def compute_directory_size(path: str) -> int:
    """Return total on-disk size (bytes) of regular files under `path`.

    Walks the tree without following symlinks, summing each regular file's
    lstat size. Symlinks (used by the dedup _shared store) are not followed,
    so shared blobs are counted exactly once. Missing path or per-entry errors
    are ignored (best-effort, never raises)."""
    if not path or not os.path.isdir(path):
        return 0

    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            total += st.st_size
    return total


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


# Reserve for the temp-download suffix ".{pid}.{task_id}.part": pid up to 7 digits,
# id(asyncio.current_task()) up to ~20 digits on 64-bit, plus 3 dots + "part". A fixed,
# generous constant keeps the truncated name DETERMINISTIC (independent of live pid/task id).
_MEDIA_PART_SUFFIX_RESERVE = 40

# Above this, an "extension" is almost certainly not one — treat the whole name as stem.
_MEDIA_MAX_EXT_BYTES = 16


def build_media_filename(file_id: str, original_name: str, name_max_bytes: int) -> str:
    """Build a length-safe media filename ``<file_id>_<original stem, truncated>.<ext>``.

    ``name_max_bytes`` is the usable per-component byte budget of the target filesystem
    (e.g. ~143 on Synology eCryptfs, 255 on ext4). The temp-download suffix is reserved
    internally so the ``.part`` file also fits (see ``download_and_shard_media``). The
    ``file_id`` prefix (uniqueness) is always preserved; only the decorative original-name
    stem is shortened. UTF-8 codepoint-safe (never splits a multibyte character) and
    deterministic (a pure function of its inputs, so retries/dedup recompute the same name).
    """
    safe_name = sanitize_media_filename(original_name)
    stem, ext = os.path.splitext(safe_name)
    if len(ext.encode("utf-8")) > _MEDIA_MAX_EXT_BYTES:
        stem, ext = safe_name, ""

    safe_id = str(file_id).replace("/", "_").replace("\\", "_")
    prefix = f"{safe_id}_"

    budget = name_max_bytes - _MEDIA_PART_SUFFIX_RESERVE - len(prefix.encode("utf-8")) - len(ext.encode("utf-8"))
    if budget <= 0:
        # Pathological tiny budget, only reachable via an absurdly small
        # MEDIA_MAX_FILENAME_BYTES (never at the 143/255 defaults, where budget is
        # comfortably positive). Fall back to a short deterministic hash of the
        # original name, keeping uniqueness (via file_id) and the extension. The
        # tiers check against the raw name_max_bytes: at a sub-reserve budget the
        # temp ``.part`` can't be made to fit anyway (a real file_id alone plus the
        # suffix already overflows), so we return the shortest useful name that fits
        # the component limit rather than uselessly dropping the extension.
        digest = hashlib.sha1(safe_name.encode("utf-8")).hexdigest()[:8]
        fallback = f"{safe_id}_{digest}{ext}"
        if len(fallback.encode("utf-8")) <= name_max_bytes:
            return fallback
        with_ext = f"{safe_id}{ext}"
        if len(with_ext.encode("utf-8")) <= name_max_bytes:
            return with_ext
        return safe_id

    # Truncate the stem to the byte budget without splitting a multibyte codepoint.
    safe_stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    if not safe_stem:
        digest = hashlib.sha1(safe_name.encode("utf-8")).hexdigest()[:8]
        return f"{safe_id}_{digest}{ext}"
    return f"{safe_id}_{safe_stem}{ext}"


# Per-media-type extension used only when mime_type is missing/unrecognized.
_FALLBACK_MEDIA_EXTENSIONS = {
    "photo": "jpg",
    "video": "mp4",
    "animation": "mp4",
    "voice": "ogg",
    "audio": "mp3",
    "sticker": "webp",
    "document": "bin",
}


def fallback_media_filename(
    telegram_file_id: str | None,
    media_type: str,
    mime_type: str | None,
    message_id: int | str | None = None,
) -> str:
    """Build a filename for media with no usable original name.

    Canonical fallback shared by the backup and listener ingest paths so both
    produce IDENTICAL names for the same media (previously they diverged: one
    used mime-type-derived extensions, the other a hardcoded per-type table with
    a different last-resort shape). The extension is derived from ``mime_type``
    via ``mimetypes.guess_extension`` (correcting the common ``jpe`` -> ``jpg``
    quirk), falling back to a per-``media_type`` default when the MIME type is
    missing or unrecognized. With a Telegram file ID, the name is
    ``<file_id>.<ext>``; without one, ``<message_id>_<media_type>.<ext>`` keeps
    retries deterministic.
    """
    extension = None
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            extension = guessed.lstrip(".")
            if extension == "jpe":
                extension = "jpg"

    if not extension:
        extension = _FALLBACK_MEDIA_EXTENSIONS.get(media_type, "bin")

    if telegram_file_id:
        safe_id = str(telegram_file_id).replace("/", "_").replace("\\", "_")
        return f"{safe_id}.{extension}"

    safe_message_id = message_id if message_id is not None else "unknown"
    return f"{safe_message_id}_{media_type}.{extension}"


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

    logger.debug("Content-hash dedup: matched existing file")
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
            logger.debug("Created symlink for deduplicated media")
        except OSError as e:
            # Type only: OSError embeds the offending path, and media paths
            # carry the chat-id folder.
            logger.warning(f"Symlink not supported, using direct path: {type(e).__name__}")
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
    logger.debug("Downloaded media to shared")

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
        # Type only, as in the sibling handler above: OSError stringifies with
        # the offending path, and a media path carries the chat-id folder.
        logger.warning(f"Symlink not supported, using direct path: {type(e).__name__}")
        import shutil

        if reused:
            shutil.copy2(shared_file_path, file_path)
        else:
            shutil.move(shared_file_path, file_path)

    return shared_file_path, content_hash


def _photo_size_bytes(size: object) -> int:
    """Byte count of a Telethon ``PhotoSize`` variant, 0 when it carries none.

    ``PhotoSize`` exposes a scalar ``size``; ``PhotoSizeProgressive`` exposes NO
    ``size`` at all, only ``sizes`` — the list of progressive byte offsets whose
    last/largest entry is the full rendition. Scoring the progressive variant as 0
    made it lose every comparison, so the largest rendition (the one Telegram
    actually delivers for big photos) was never selected and its ``w``/``h`` were
    read off a smaller thumbnail instead.
    """
    value = getattr(size, "size", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    progressive = getattr(size, "sizes", None)
    if isinstance(progressive, (list, tuple)):
        candidates = [v for v in progressive if isinstance(v, int) and not isinstance(v, bool)]
        if candidates:
            return max(candidates)
    return 0


def extract_media_attributes(media: object) -> dict[str, Any]:
    """Extract size/mime/dimension/duration metadata from a Telegram media object.

    THE single extractor for these five columns: both the scheduled sweep
    (``TelegramBackup._process_media``) and the realtime listener call it, so a
    sweep that re-upserts a message the listener already stored writes back the
    SAME values instead of nulling them (#263). Two divergent copies of this logic
    are what produced the bug: live-captured voice notes rendered without their
    duration while the same note captured by the sweep showed it.

    THE RETURNED KEY SET IS A CONTRACT: exactly ``file_size``, ``mime_type``,
    ``width``, ``height``, ``duration`` — every key always present, value ``None``
    when unknown. Both callers spread it into a ``media`` row and then OVERRIDE
    ``file_size`` with the on-disk byte count (the sweep re-assigns it after the
    spread, the listener mutates the dict before spreading); the other four are
    taken as returned. Adding or renaming a key here silently changes what those
    rows write, so ``TestExtractMediaAttributes`` pins the key set — extend both
    callers and that test together. ``file_size`` is Telegram's DECLARED size and
    is the value that survives only when no file was written.

    ``mime_type`` is read off ``media.document`` (the ``MessageMediaDocument``
    wrapper itself has no ``mime_type``; the sweep's old ``getattr(media, ...)``
    read therefore always yielded ``None``). Photo dimensions and ``file_size``
    come from the largest ``PhotoSize`` (the ``Photo`` object itself carries no
    ``w``/``h``), sized via ``_photo_size_bytes`` so progressive renditions count.

    ``duration`` is coerced to ``int``: Telethon reports video durations as a
    float while the ``media.duration`` column is INTEGER. Rounding (not
    truncation) is used because that is what PostgreSQL's implicit float->int
    assignment cast did to the raw floats the sweep has always passed, so newly
    written rows stay consistent with the historical ones.
    """
    attributes: dict[str, Any] = {
        "file_size": None,
        "mime_type": None,
        "width": None,
        "height": None,
        "duration": None,
    }

    document = getattr(media, "document", None)
    photo = getattr(media, "photo", None)

    if document is not None:
        attributes["file_size"] = getattr(document, "size", None)
        attributes["mime_type"] = getattr(document, "mime_type", None)
        for attr in getattr(document, "attributes", None) or ():
            if hasattr(attr, "w") and hasattr(attr, "h"):
                attributes["width"] = attr.w
                attributes["height"] = attr.h
            if hasattr(attr, "duration"):
                attributes["duration"] = attr.duration
    elif photo is not None:
        sizes = getattr(photo, "sizes", None) or ()
        largest = max(sizes, key=_photo_size_bytes, default=None)
        if largest is not None:
            attributes["file_size"] = _photo_size_bytes(largest) or None
            attributes["width"] = getattr(largest, "w", None)
            attributes["height"] = getattr(largest, "h", None)

    duration = attributes["duration"]
    if isinstance(duration, float):
        attributes["duration"] = round(duration)

    return attributes


# Storage-name prefixes added by the ingest paths, stripped back off for display
# and for the download filename. ``<file_id>_`` comes from ``build_media_filename``
# (Telegram download) and ``import_<chat>_<msg>_`` from the Telegram Desktop
# importer's media id. Anchored and applied at most once, so only the FIRST
# component can ever be lost: ``77_2026_report.pdf`` -> ``2026_report.pdf``.
# ``[0-9]`` rather than ``\d``: Python's ``\d`` also matches non-ASCII decimal
# digits, which the viewer's ``/^[0-9]+_/`` does not — and this function exists to
# produce exactly the name the viewer shows.
_MEDIA_STORAGE_PREFIX_RE = re.compile(r"^(?:import_-?[0-9]+_[0-9]+_|[0-9]+_)")


def media_display_filename(stored_name: str) -> str:
    """Turn a stored media basename back into the name the user recognises.

    Media is stored under a uniqueness-prefixed name (``<file_id>_holiday.jpg``),
    which is what ``Media.file_name`` holds too — it is the basename of
    ``Media.file_path``, not the original document name. The viewer already hides
    the prefix (``getMediaDisplayName`` in index.html); this is the server-side
    twin so a forced download saves ``holiday.jpg`` rather than the storage name.

    Takes only the basename, because the only caller (``serve_media``) only has a
    URL path — it never holds ``Media.id``. That is why the pattern carries an
    explicit ``import_<chat>_<msg>_`` alternation: the viewer strips that prefix
    using the media id it already has, and without the alternation an imported
    file would save as ``import_-1001_5_report.pdf`` while the gallery labels it
    ``report.pdf``. Every prefix any ingest path actually writes is covered —
    ``build_media_filename`` emits ``<file_id>_`` and the importer emits
    ``import_<chat>_<msg>_``.

    At most one prefix is removed, so ``77_2026_report.pdf`` keeps the second one
    and yields ``2026_report.pdf``. A name whose ORIGINAL first component is
    digits (``2026_report.pdf``) is indistinguishable from a prefixed one and does
    lose it (-> ``report.pdf``); the viewer's unconditional
    ``name.replace(/^[0-9]+_/, '')`` does the same, and matching the visible label
    is the point of this function. Returns ``stored_name`` unchanged when nothing
    matches or stripping would leave an empty name.
    """
    name = _MEDIA_STORAGE_PREFIX_RE.sub("", stored_name, count=1)
    return name or stored_name


def describe_exception(exc: BaseException) -> str:
    """Exception detail that cannot smuggle a filesystem path into the logs.

    ``OSError`` and its subclasses stringify with the offending filename —
    ``[Errno 66] Directory not empty: '/data/media/-1001234'`` — and a media
    path carries the chat-id folder, so for those only the type is safe. That is
    how a leak survived the #274 sweep: the message had stopped naming the id
    while the exception text still carried it.

    Everything else keeps its message, because that is where the diagnostic
    value lives: a Telethon ``FloodWaitError``'s wait, an RPC error's reason.
    Blanket-stripping every ``{e}`` would buy no privacy and cost real
    debuggability.
    """
    if isinstance(exc, OSError):
        return type(exc).__name__
    return f"{type(exc).__name__}: {exc}"


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


def service_action_type(action: object) -> str:
    """Normalize a Telethon ``MessageAction`` class name to a snake_case tag.

    THE shared ``raw_data.action_type`` vocabulary: since the #222 fix, both the
    backup backfill path AND the live listener's chat-action handler label
    service messages with these tags (the listener's old curated set —
    ``title_changed``, ``user_joined``, ... — was retired and its historical
    rows deleted by migration 019; nothing may ever emit those names again).

    Examples: ``MessageActionTopicCreate`` -> ``"topic_create"``,
    ``MessageActionTopicEdit`` -> ``"topic_edit"``,
    ``MessageActionChatEditTitle`` -> ``"chat_edit_title"``.

    Note: consecutive capitals (acronyms) are split letter-by-letter, e.g.
    ``MessageActionSetMessagesTTL`` -> ``"set_messages_t_t_l"``. None of the
    title-bearing actions we care about are affected; the tag is only a stable,
    deterministic identifier and is not parsed back, so this is cosmetic.
    """
    name = type(action).__name__.removeprefix("MessageAction")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def service_message_text(
    action: object,
    *,
    actor_name: str | None = None,
    affected_left: bool = False,
    affected_joined_self: bool = False,
) -> str | None:
    """Build human-readable text for a Telegram service ``MessageAction``.

    Shared by the real-time listener (``on_chat_action``) and the scheduled
    backup sweep (``_process_message``) so both render identical wording for the
    same service event. Keyed purely on the ``MessageAction`` subclass name; the
    ``raw_data.action_type`` tag (``service_action_type``) stays the storage
    identifier while this is the display string the viewer shows.

    Args:
        action: A Telethon ``MessageAction`` instance (``message.action`` or
            ``event.action_message.action``).
        actor_name: Display name of the SUBJECT of the sentence — for
            ``ChatAddUser``/``ChatDeleteUser`` that is the AFFECTED user
            (added/removed), not the admin who performed the action; for every
            other action the actor and subject coincide. Falsy values render as
            "Someone", matching the historical listener wording.
        affected_left: ``MessageActionChatDeleteUser`` only — ``True`` when the
            affected user removed themselves (left), ``False`` when a different
            user removed them.
        affected_joined_self: ``MessageActionChatAddUser`` only — ``True`` when
            the user added themselves (joined via the public username), which
            reads "joined the group" rather than "was added".

    Returns:
        The rendered text, or ``None`` for actions with no curated wording; the
        caller stores ``""`` for those, exactly as the sweep does today.
    """
    name = type(action).__name__
    who = actor_name or "Someone"
    title = getattr(action, "title", None)
    if title is not None and not isinstance(title, str):
        # Defensive: a title may arrive as TextWithEntities rather than a plain
        # str; fall back to its .text so the wording never breaks on drift.
        title = getattr(title, "text", None) or str(title)

    if name == "MessageActionChatJoinedByLink":
        return f"{who} joined the group via invite link"
    if name == "MessageActionChatJoinedByRequest":
        return f"{who} joined the group"
    if name == "MessageActionChatAddUser":
        if affected_joined_self:
            return f"{who} joined the group"
        return f"{who} was added to the group"
    if name == "MessageActionChatDeleteUser":
        if affected_left:
            return f"{who} left the group"
        return f"{who} was removed from the group"
    if name == "MessageActionChatEditTitle":
        return f'{who} changed the group name to "{title}"'
    if name == "MessageActionChatEditPhoto":
        return f"{who} changed the group photo"
    if name == "MessageActionChatDeletePhoto":
        return f"{who} removed the group photo"
    if name == "MessageActionChatCreate":
        return f'{who} created the group "{title}"'
    if name == "MessageActionChannelCreate":
        return f'{who} created the channel "{title}"'
    return None


def normalize_reaction_emoji(reaction: object) -> str | None:
    """Normalize a Telethon ``Reaction`` variant to a stable storage string.

    - ``ReactionEmoji`` -> its ``emoticon`` (e.g. ``"👍"``)
    - ``ReactionCustomEmoji`` -> ``f"custom_{document_id}"`` (the viewer renders a
      placeholder; resolving the sticker needs a separate API call, out of scope)
    - ``ReactionPaid`` (Telegram Stars) -> ``"paid"`` sentinel (no per-instance emoji)
    - ``ReactionEmpty`` / unknown -> ``None`` (ignored by the caller)

    Defensive by design: Telethon is archived (Feb 2026), so this tolerates
    attribute/shape drift rather than assuming exact constructors.
    """
    if reaction is None:
        return None
    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return emoticon
    document_id = getattr(reaction, "document_id", None)
    if document_id is not None:
        return f"custom_{document_id}"
    cls = type(reaction).__name__
    if "Paid" in cls:
        return "paid"
    if "Empty" in cls:
        return None
    return None


def extract_reactions(message_reactions: object) -> list[dict[str, object]] | None:
    """Extract the per-emoji aggregate from a Telethon ``MessageReactions``.

    Accepts ``message.reactions`` (scheduled backup) or an
    ``UpdateMessageReactions.reactions`` (live listener) — both are the same
    ``MessageReactions`` object carrying the FULL current snapshot in
    ``results`` (``list[ReactionCount]``).

    Returns:
    - ``[{"emoji", "count"}, ...]`` — the current aggregate (possibly ``[]`` for a
      message with no reactions; callers treat ``[]`` as an authoritative empty
      snapshot and reconcile removals down to zero).
    - ``None`` — extraction FAILED (unexpected shape). Callers MUST skip
      reconciliation on ``None`` rather than treat it as empty, so transient
      Telethon shape drift can never tombstone valid reactions.

    Aggregate-only by design (see ``DatabaseAdapter.reconcile_reactions``):
    ``results`` counts are authoritative; per-user identity from
    ``recent_reactions`` is an unreliable sliding-window preview and is not used.
    Never raises and never logs identifiers/content (PII).
    """
    if message_reactions is None:
        return []
    out: list[dict[str, object]] = []
    try:
        results = getattr(message_reactions, "results", None) or []
        for rc in results:
            emoji = normalize_reaction_emoji(getattr(rc, "reaction", None))
            if not emoji:
                continue
            count = int(getattr(rc, "count", 0) or 0)
            if count <= 0:
                continue
            out.append({"emoji": emoji, "count": count})
    except Exception as e:
        # Telethon is archived (Feb 2026); tolerate shape drift rather than break a
        # whole backup batch — but signal FAILURE (None) so callers skip reconcile
        # instead of tombstoning valid rows. No identifiers/content logged (PII).
        logger.debug("Reaction extraction failed, skipping reconcile: %s", type(e).__name__)
        return None
    return out
