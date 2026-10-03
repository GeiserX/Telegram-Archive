"""Find media rows whose file is gone, and put the file back.

A media row with ``downloaded = 1`` names a path under the media folder:
``<media>/<chat folder>/<name>``. With DEDUPLICATE_MEDIA that chat entry is a
relative symlink into ``_shared/``, where the bytes live. A row can end up
naming nothing in two ways:

* **a broken link**: the chat entry is a link into ``_shared`` and the
  ``_shared`` entry it names is gone;
* **a missing file**: there is no chat entry at all. Migration 013 moved the
  path of every legacy row from the plain-id folder (``N``) to the marked one
  (``-100N`` or ``-N``) without moving any file, so a row can point at a folder
  that never held its file while the file sits in the other one.

``repair_media_row`` looks for a copy of the same media already on disk and
puts it back where the row points, never replacing anything that exists. A copy
is looked for, in order, under the same name elsewhere in ``_shared``, under
the same name in the chat's other id-form folders, and at the path of any other
row with the same content hash. When none is found the row is marked not
downloaded, so the normal download path fetches it from Telegram again.

A row is marked only when its file is provably gone: the media root is
visibly there (it exists and is not empty) and the row's folder exists under
it (``visible_media_root``, ``missing_under_root``). A media volume that is
not mounted, or a share that dropped, makes every path read as missing; on
that evidence no row changes.

A file can also be in place and still be cut short: a release from late 2025
stored downloads that stopped early as complete (Telethon ends a download at
the first short answer and never compares the total with the declared size).
``cut_short_state`` tells such a video or audio file apart without decoding
it: its size is a multiple of Telethon's smallest request size and its
top-level boxes run past the end of the file or hold no index (``moov``).
``check-media --repair`` marks it not downloaded, and the download replaces it.

A photo in place can also lack its size: releases before 7.32.0 stored no
width or height for photos from the full pass, and the viewer sizes a picture
from them. ``image_header_size`` reads the size from the file's header without
decoding it, and ``check-media --repair`` stores it on a row that has none.

Everything here is a few ``lstat`` calls per row and a hash of each candidate,
plus one open and a few small reads per MP4-family file or photo with no size.
Nothing walks the media tree.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import warnings
from dataclasses import dataclass, field
from typing import Any

from .message_utils import (
    METADATA_ONLY_MEDIA_TYPES,
    broken_shared_link_target,
    compute_file_hash,
    place_copy,
    resolve_shared_file_path,
    shared_link_target,
)
from .web.media_utils import CHANNEL_ID_OFFSET, resolve_stored_media_path

logger = logging.getLogger(__name__)

SHARED_DIR_NAME = "_shared"

# Row states, as repair_media_row and inspect_media_row report them.
PRESENT = "present"  # the path resolves to a file
RESTORED = "restored"  # a copy was put back
RESTORABLE = "restorable"  # a copy exists (inspection only)
REFETCH = "refetch"  # no copy anywhere: marked not downloaded
MISSING = "missing"  # no copy anywhere (inspection only)
KEPT = "kept"  # an entry exists but cannot be read from here: left alone
# A location, contact, poll or other metadata-only kind: the row is the whole
# record and has no file. Older releases still gave some of these rows a
# ``.bin`` path and a link; that path is a leftover, never a missing file.
PLACEHOLDER = "placeholder"
# The media root is missing, unreadable or empty here: nothing was looked at.
NOT_VISIBLE = "not_visible"

# A video or audio file whose download stopped early (``cut_short_state``).
TRUNCATED = "truncated"  # no index and a size a stopped download leaves: marked
SUSPICIOUS = "suspicious"  # no index, but any size: counted, never marked

# The ISO base media formats whose top-level boxes ``iso_bmff_incomplete``
# walks. The extension decides, not the media type: a .mp4 sent as a file is
# a document and has the same risk. Matroska and Ogg are not checked.
ISO_BMFF_EXTENSIONS = frozenset({".mp4", ".m4v", ".m4a", ".mov", ".3gp"})
# Telethon's smallest request size for a file of known size
# (``utils.get_appropriated_part_size``: 128 KiB up to 100 MB, then 256 and
# 512 KiB). A download that stopped at a short answer ends on a multiple of
# its request size, so every such cut is a multiple of this.
CUT_SHORT_GRAIN = 128 * 1024

# A name that starts with a Telegram file id (a 64-bit number) names one
# Telegram file wherever it appears. The fallback name for media without a file
# id, ``<message_id>_<type>.<ext>``, names a different file in every chat.
_TELEGRAM_FILE_NAME = re.compile(r"^\d{12,}")


def visible_media_root(media_root: str | None) -> str | None:
    """The real path of the media root when the archive's disk is visibly there, else None.

    A missing, unreadable or empty media root means the process runs where
    the media volume is not mounted (a host or PyPI install, a volume left
    out, a share that dropped, a root moved since capture). Every stored path
    would then read as missing, so no row may be changed on that evidence.
    """
    if not media_root:
        return None
    try:
        if not os.path.isdir(media_root):
            return None
        with os.scandir(media_root) as entries:
            if next(entries, None) is None:
                return None
    except OSError, TypeError, ValueError:
        return None
    return os.path.realpath(media_root)


def missing_under_root(path: str | None, media_root: str | None) -> bool:
    """True when ``path`` is missing (or a dangling link) inside a visible media root.

    ``media_root`` is what ``visible_media_root`` returned. Only a definite
    "no such file" counts: a permission error or a path component that is not
    a directory says nothing about the file. The parent directory must exist
    and lie under the media root, so a path from another mount, or under a
    directory that is gone, is never read as missing.
    """
    if not path or not media_root:
        return False
    try:
        # stat() follows links: FileNotFoundError for a missing file and for a dangling link.
        os.stat(path)
        return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    parent = os.path.dirname(path)
    try:
        if not os.path.isdir(parent):
            return False
        real_parent = os.path.realpath(parent)
        return os.path.commonpath([real_parent, media_root]) == media_root
    except OSError, ValueError:
        return False


def chat_folder_alternates(folder: str) -> list[str]:
    """The other id forms of a chat's media folder: ``N``, ``-N`` and ``-100N``.

    Channels and supergroups were stored under the plain id before v4.0.5 and
    under ``-100N`` after it, and a group re-keyed between ``-N`` and ``-100N``
    keeps files under both. A name that is not a number has no alternates.
    """
    try:
        value = int(folder)
    except ValueError:
        return []
    raw = abs(value)
    if raw > CHANNEL_ID_OFFSET:
        raw -= CHANNEL_ID_OFFSET
    if raw == 0:
        return []
    forms = (str(raw), str(-raw), str(-(CHANNEL_ID_OFFSET + raw)))
    return [form for form in forms if form != folder]


def iso_bmff_incomplete(path: str) -> bool | None:
    """Whether an MP4-family file ends before its own boxes do.

    Walks the top-level boxes with seek and read, never reading a payload and
    never decoding. True when a box's declared size (32-bit, or the 64-bit
    largesize when the size field is 1) runs past the end of the file, when
    the file ends inside a box header, or when the walk reaches the end with
    no ``moov`` box (the index a player needs). A size of 0 means "to the end
    of the file" and ends the walk; the ``moov`` rule still applies. False
    when every box fits and a ``moov`` was seen. None when the file cannot be
    read, is empty, does not start with ``ftyp`` or holds a malformed box:
    such a file is not judged.

    "Runs past the end" also catches a file with ``moov`` first (faststart)
    whose media data was cut.
    """
    try:
        with open(path, "rb") as f:
            end = os.fstat(f.fileno()).st_size
            offset = 0
            seen_moov = False
            while offset < end:
                f.seek(offset)
                header = f.read(8)
                if len(header) < 8:
                    return None if offset == 0 else True
                size, kind = struct.unpack(">I4s", header)
                if offset == 0 and kind != b"ftyp":
                    return None
                header_len = 8
                if size == 1:
                    large = f.read(8)
                    if len(large) < 8:
                        return True
                    size = struct.unpack(">Q", large)[0]
                    header_len = 16
                if kind == b"moov":
                    seen_moov = True
                if size == 0:
                    break
                if size < header_len:
                    return None
                if offset + size > end:
                    return True
                offset += size
            if end == 0:
                return None
            return not seen_moov
    except OSError:
        return None


# EXIF orientations 5 to 8 turn the picture a quarter: the stored width is the
# height it is drawn at.
_EXIF_ORIENTATION = 0x0112
_QUARTER_TURNS = frozenset({5, 6, 7, 8})


def image_header_size(path: str | None) -> tuple[int, int] | None:
    """The width and height the picture at ``path`` is drawn at, from its header.

    Nothing is decoded: Pillow reads the header when it opens the file, which
    it opens read-only. An EXIF orientation of 5 to 8 swaps the two, as a
    browser draws the picture turned. The orientation is read only where it
    sits in the header (a JPEG, or any file whose header carried EXIF): for a
    PNG without it, Pillow's ``getexif`` decodes the whole picture to look
    for EXIF after the pixels. None for a file Pillow cannot identify, one
    over Pillow's pixel limit, and any read error.
    """
    if not path:
        return None
    try:
        from PIL import Image

        with warnings.catch_warnings():
            # Pillow only warns between its pixel limit and twice it; over the
            # limit is unreadable here, as it is to the thumbnailer.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                in_header = image.format in ("JPEG", "MPO") or "exif" in image.info
                orientation = image.getexif().get(_EXIF_ORIENTATION) if in_header else None
    except Exception:
        return None
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        return None
    if orientation in _QUARTER_TURNS:
        width, height = height, width
    return width, height


def cut_short_state(path: str | None) -> str | None:
    """TRUNCATED, SUSPICIOUS or None for the file at ``path``.

    Only a path with an MP4-family extension (``ISO_BMFF_EXTENSIONS``) is
    looked at. TRUNCATED: ``iso_bmff_incomplete`` and a size that is a
    multiple of ``CUT_SHORT_GRAIN``, the shape a stopped Telethon download
    leaves. SUSPICIOUS: incomplete at any other size, which a stopped
    download does not explain, so it is counted and never marked. None for
    everything else, a file that is not judged included.
    """
    if not path or os.path.splitext(path)[1].lower() not in ISO_BMFF_EXTENSIONS:
        return None
    if iso_bmff_incomplete(path) is not True:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    return TRUNCATED if size % CUT_SHORT_GRAIN == 0 else SUSPICIOUS


@dataclass
class MediaInspection:
    """What one row's path holds, and where a copy of its file is."""

    state: str
    path: str | None = None
    # The path to create: the missing ``_shared`` entry of a broken link, or
    # the missing chat entry itself.
    restore_to: str | None = None
    broken_link: bool = False
    source: str | None = None
    candidates_checked: list[str] = field(default_factory=list)


def _matches(candidate: str, file_name: str | None, content_hash: str | None, file_size: int | None) -> bool:
    """Whether ``candidate`` is a copy of the row's file.

    A known content hash decides alone. Without one, the name has to carry a
    Telegram file id and the size has to match when the row knows it.
    """
    try:
        if not os.path.isfile(candidate):
            return False
        if content_hash:
            return compute_file_hash(candidate) == content_hash
        if not file_name or not _TELEGRAM_FILE_NAME.match(os.path.basename(file_name)):
            return False
        if file_size and file_size > 0:
            return os.path.getsize(candidate) == file_size
        return os.path.getsize(candidate) > 0
    except OSError:
        return False


def _local_candidates(media_root: str, path: str, file_name: str, content_hash: str | None) -> list[str]:
    """Paths that may hold a copy, cheapest first. None of them walks a tree."""
    shared_dir = os.path.join(media_root, SHARED_DIR_NAME)
    candidates: list[str] = []
    if content_hash:
        found = resolve_shared_file_path(shared_dir, file_name, content_hash)
        if found:
            candidates.append(found)
    found = resolve_shared_file_path(shared_dir, file_name, None)
    if found:
        candidates.append(found)
    folder = os.path.basename(os.path.dirname(path))
    parent = os.path.dirname(os.path.dirname(path))
    for alternate in chat_folder_alternates(folder):
        candidates.append(os.path.join(parent, alternate, os.path.basename(path)))
        if os.path.basename(path) != file_name:
            candidates.append(os.path.join(parent, alternate, file_name))
    return list(dict.fromkeys(candidates))


def inspect_media_row(row: dict[str, Any], media_root: str, extra_candidates: list[str] = ()) -> MediaInspection:
    """Classify one media row and find a copy of its file. Writes nothing.

    ``extra_candidates`` are more paths to try, such as the paths of other rows
    with the same content hash.
    """
    if row.get("type") in METADATA_ONLY_MEDIA_TYPES:
        return MediaInspection(state=PLACEHOLDER)
    path = resolve_stored_media_path(row.get("file_path"), media_root)
    if not path:
        return MediaInspection(state=MISSING)
    if os.path.isfile(path):
        return MediaInspection(state=PRESENT, path=path)

    shared_dir = os.path.join(media_root, SHARED_DIR_NAME)
    restore_to = None
    broken_link = False
    if os.path.lexists(path):
        restore_to = broken_shared_link_target(path, shared_dir)
        if restore_to is None:
            # A link out of the archive, a _shared entry that is itself a link
            # nobody here can follow, or something that is not a file: the
            # archive did not make it and does not touch it.
            return MediaInspection(state=KEPT, path=path)
        broken_link = True
    else:
        restore_to = path

    file_name = row.get("file_name") or os.path.basename(path)
    content_hash = row.get("content_hash") or None
    file_size = row.get("file_size")
    candidates = _local_candidates(media_root, path, file_name, content_hash) + list(extra_candidates)
    checked = []
    for candidate in dict.fromkeys(candidates):
        if not candidate or os.path.realpath(candidate) == os.path.realpath(restore_to):
            continue
        checked.append(candidate)
        if _matches(candidate, file_name, content_hash, file_size):
            return MediaInspection(
                state=RESTORABLE,
                path=path,
                restore_to=restore_to,
                broken_link=broken_link,
                source=candidate,
                candidates_checked=checked,
            )
    return MediaInspection(
        state=MISSING, path=path, restore_to=restore_to, broken_link=broken_link, candidates_checked=checked
    )


def restore_from_copy(inspection: MediaInspection, media_root: str) -> bool:
    """Put the copy an inspection found where the row expects it.

    A broken link gets its ``_shared`` entry back and is itself left as it is.
    A missing chat entry becomes a symlink when the copy is in ``_shared`` (the
    shape the backup writes), and otherwise a hardlink or copy of the file.
    Returns False when the target appeared meanwhile or cannot be written.
    """
    if inspection.state != RESTORABLE or not inspection.source or not inspection.restore_to:
        return False
    target = inspection.restore_to
    shared_dir = os.path.realpath(os.path.join(media_root, SHARED_DIR_NAME))
    source_real = os.path.realpath(inspection.source)
    try:
        if not inspection.broken_link and source_real.startswith(shared_dir + os.sep):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.symlink(os.path.relpath(inspection.source, os.path.dirname(target)), target)
        else:
            place_copy(inspection.source, target)
    except FileExistsError:
        return os.path.isfile(inspection.path or target)
    except OSError as e:
        # Type only: the path carries the chat-id folder.
        logger.warning(f"Could not restore a media file from its copy: {type(e).__name__}")
        return False
    return True


async def _hash_twin_paths(db, row: dict[str, Any], media_root: str) -> list[str]:
    """Paths of other rows, any account, that hold the same content hash."""
    content_hash = row.get("content_hash")
    if not content_hash or not hasattr(db, "get_media_paths_by_content_hash"):
        return []
    try:
        stored = await db.get_media_paths_by_content_hash(content_hash)
    except Exception as e:
        logger.debug(f"Could not look up copies by content hash: {type(e).__name__}")
        return []
    paths = []
    for value in stored:
        resolved = resolve_stored_media_path(value, media_root)
        if resolved:
            paths.append(resolved)
    return paths


async def inspect_media_row_with_db(db, row: dict[str, Any], media_root: str) -> MediaInspection:
    """``inspect_media_row``, also trying the paths of rows with the same hash."""
    inspection = inspect_media_row(row, media_root)
    if inspection.state != MISSING or not inspection.restore_to or not row.get("content_hash"):
        return inspection
    twins = await _hash_twin_paths(db, row, media_root)
    if not twins:
        return inspection
    return inspect_media_row(row, media_root, extra_candidates=twins)


async def repair_media_row(db, row: dict[str, Any], media_root: str, *, account_id: int, refetch: bool = True) -> str:
    """Make one row's path hold its file again, or hand it back to the download.

    Returns PRESENT, RESTORED, REFETCH, MISSING, KEPT, PLACEHOLDER or
    NOT_VISIBLE. A metadata-only row is never repaired or fetched: it has no
    file to fetch.
    REFETCH marks the row not downloaded, which the pending-download retry of
    the next backup picks up. That download fills a broken link's ``_shared``
    entry under the name the link holds, so the link itself is never rewritten.
    A row is marked only when its file is provably gone (``missing_under_root``):
    otherwise, and with ``refetch`` off, a file with no copy is MISSING and the
    row is left as it is. With the media root not visible here, nothing is
    looked at and the answer is NOT_VISIBLE.
    """
    root = visible_media_root(media_root)
    if root is None:
        return NOT_VISIBLE
    inspection = await inspect_media_row_with_db(db, row, media_root)
    if inspection.state in (PRESENT, KEPT, PLACEHOLDER):
        return inspection.state
    if inspection.state == RESTORABLE and restore_from_copy(inspection, media_root):
        return RESTORED
    if not refetch or row.get("id") is None or not missing_under_root(inspection.path, root):
        return MISSING
    try:
        await db.mark_media_for_redownload(row["id"], account_id=row.get("account_id") or account_id, keep_path=True)
    except Exception as e:
        logger.warning(f"Could not mark media for re-download: {type(e).__name__}")
        return MISSING
    return REFETCH


def file_in_place(row: dict[str, Any], media_root: str) -> bool:
    """Whether a row marked not downloaded has its own file at its path after all.

    For a row an outage marked: the share dropped, the file read as missing,
    the row was marked and its download attempts ran out, and the file is
    back. A known content hash decides alone; without one the size has to be
    within the 1% VERIFY_MEDIA allows when the row knows it, and the file must
    not be empty. A metadata-only row and a row skipped on purpose
    (``skip_reason``) never count, and neither does a file cut short
    (``cut_short_state``): its hash and size match the row because the row
    was written from the short file, and counting it would mark it
    downloaded again before the new download replaces it.
    """
    if row.get("type") in METADATA_ONLY_MEDIA_TYPES or row.get("skip_reason"):
        return False
    path = resolve_stored_media_path(row.get("file_path"), media_root)
    try:
        if not path or not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if size <= 0:
            return False
        if cut_short_state(path) == TRUNCATED:
            return False
        if row.get("content_hash"):
            return compute_file_hash(path) == row["content_hash"]
    except OSError:
        return False
    expected = row.get("file_size")
    if expected and expected > 0:
        return abs(size - expected) <= expected * 0.01
    return True


def _empty_report() -> dict[str, int]:
    return {
        "checked": 0,
        "present": 0,
        "broken_links": 0,
        "missing_files": 0,
        "restorable": 0,
        "refetch": 0,
        "refetch_failed": 0,
        "restored": 0,
        "restore_failed": 0,
        "kept": 0,
        "placeholders": 0,
        "not_provable": 0,
        "recoverable": 0,
        "recovered": 0,
        "recover_failed": 0,
        "media_root_not_visible": 0,
        "truncated": 0,
        "suspicious": 0,
        "dimensions_missing": 0,
        "dimensions_unreadable": 0,
        "dimensions_filled": 0,
        "dimensions_fill_failed": 0,
    }


async def check_media(db, media_root: str, *, repair: bool = False, chat_id: int | None = None) -> dict[str, int]:
    """Check every downloaded media row of every account, and repair with ``repair``.

    The ``check-media`` command. Rows are read in batches, each costing a few
    ``lstat`` calls; the media tree is never walked. Without ``repair`` nothing
    is written. Counts only: never ids, paths or names.

    With the media root not visible here (missing, unreadable or empty),
    nothing is checked or changed and ``media_root_not_visible`` is 1. A row
    whose file is missing but whose folder is not under the media root is
    counted in ``not_provable`` and never marked. A row marked not downloaded
    whose own file is at its path (``file_in_place``) is counted in
    ``recoverable``, and ``repair`` marks it downloaded again.

    A file in place that a stopped download cut short (``cut_short_state``)
    is counted in ``truncated`` instead of ``present``, and ``repair`` marks
    it not downloaded, keeping its path, so the next backup downloads it
    again and replaces it. Its bytes stay on disk until then. A file with no
    index at a size a stopped download does not leave is counted in
    ``suspicious`` and never marked.

    A photo in place with neither width nor height is counted in
    ``dimensions_missing`` when its header gives a size, and ``repair`` stores
    it (``dimensions_filled``). One whose header cannot be read is counted in
    ``dimensions_unreadable`` and left as it is. A stored size is never
    overwritten.
    """
    from .db.models import DEFAULT_ACCOUNT_ID

    report = _empty_report()
    root = visible_media_root(media_root)
    if root is None:
        report["media_root_not_visible"] = 1
        return report
    try:
        account_ids = list(await db.get_account_ids()) or [DEFAULT_ACCOUNT_ID]
    except Exception:
        account_ids = [DEFAULT_ACCOUNT_ID]
    for account_id in account_ids:
        async for batch in db.iter_media_for_verification(account_id=account_id):
            for row in batch:
                if chat_id is not None and row.get("chat_id") != chat_id:
                    continue
                if not row.get("downloaded"):
                    if file_in_place(row, media_root):
                        report["recoverable"] += 1
                        if repair:
                            await _mark_recovered(db, row, account_id, report)
                    continue
                report["checked"] += 1
                inspection = await inspect_media_row_with_db(db, row, media_root)
                if inspection.state == PRESENT:
                    cut = cut_short_state(inspection.path)
                    if cut == SUSPICIOUS:
                        report["suspicious"] += 1
                    elif cut == TRUNCATED:
                        report["truncated"] += 1
                        if repair:
                            await _mark_for_refetch(db, row, account_id, report)
                    else:
                        report["present"] += 1
                    await _check_photo_size(db, row, inspection.path, account_id, report, repair=repair)
                    continue
                if inspection.state == KEPT:
                    report["kept"] += 1
                    continue
                if inspection.state == PLACEHOLDER:
                    report["placeholders"] += 1
                    continue
                report["broken_links" if inspection.broken_link else "missing_files"] += 1
                if inspection.state == RESTORABLE:
                    report["restorable"] += 1
                    if repair:
                        if restore_from_copy(inspection, media_root):
                            report["restored"] += 1
                        else:
                            report["restore_failed"] += 1
                    continue
                if not missing_under_root(inspection.path, root):
                    # The row's folder is not under the media root here: the
                    # file is not provably gone, so the row is never marked.
                    report["not_provable"] += 1
                    continue
                if not repair:
                    report["refetch"] += 1
                    continue
                await _mark_for_refetch(db, row, account_id, report)
    return report


async def _check_photo_size(
    db, row: dict[str, Any], path: str | None, account_id: int, report: dict[str, int], *, repair: bool
) -> None:
    """Count a photo with no stored size, and with ``repair`` store the size its header gives."""
    if row.get("type") != "photo" or row.get("width") is not None or row.get("height") is not None:
        return
    size = image_header_size(path)
    if size is None:
        report["dimensions_unreadable"] += 1
        return
    report["dimensions_missing"] += 1
    if not repair:
        return
    try:
        changed = await db.fill_media_dimensions(row["id"], account_id=account_id, width=size[0], height=size[1])
    except Exception as e:
        logger.warning(f"Could not store a photo size: {type(e).__name__}")
        report["dimensions_fill_failed"] += 1
        return
    if changed:
        report["dimensions_filled"] += 1


async def _mark_for_refetch(db, row: dict[str, Any], account_id: int, report: dict[str, int]) -> None:
    """Mark one row not downloaded, keeping its path, and count the outcome."""
    try:
        await db.mark_media_for_redownload(row["id"], account_id=account_id, keep_path=True)
    except Exception as e:
        # One row's failure is counted and the check goes on.
        logger.warning(f"Could not mark media for re-download: {type(e).__name__}")
        report["refetch_failed"] += 1
    else:
        report["refetch"] += 1


async def _mark_recovered(db, row: dict[str, Any], account_id: int, report: dict[str, int]) -> None:
    try:
        changed = await db.mark_media_downloaded(row["id"], account_id=account_id)
    except Exception as e:
        logger.warning(f"Could not mark media downloaded: {type(e).__name__}")
        report["recover_failed"] += 1
        return
    if changed:
        report["recovered"] += 1


def format_media_check(report: dict[str, int], *, repair: bool) -> list[str]:
    """The report as plain lines for a terminal."""
    if report.get("media_root_not_visible"):
        return [
            "Media check: the media folder is not visible here (missing, unreadable or empty).",
            "  Nothing was checked or changed. Mount the media volume, or set MEDIA_PATH, and run it again.",
        ]
    if repair:
        head = "Media check and repair:"
    else:
        head = "Media check (dry run, nothing changed; run with --repair to fix):"
    lines = [
        head,
        f"  Rows checked:              {report['checked']}",
        f"  Files in place:            {report['present']}",
        f"  Broken links:              {report['broken_links']}  (a link into _shared whose file is gone)",
        f"  Missing files:             {report['missing_files']}  (nothing at the row's path)",
    ]
    if repair:
        lines += [
            f"  Restored from a copy:      {report['restored']}",
            f"  Marked to download again:  {report['refetch']}  (the next backup fetches them from Telegram)",
        ]
        if report["restore_failed"]:
            lines.append(f"  Could not restore:         {report['restore_failed']}  (see the log)")
        if report["refetch_failed"]:
            lines.append(f"  Could not mark:            {report['refetch_failed']}  (see the log)")
    else:
        lines += [
            f"  Copy found on disk:        {report['restorable']}  (--repair puts it back)",
            f"  No copy on disk:           {report['refetch']}  (--repair marks them to download again)",
        ]
    if report.get("truncated"):
        lines.append(
            f"  Cut short:                 {report['truncated']}  "
            "(a video or audio file whose download stopped early; --repair marks them to download again)"
        )
    if report.get("suspicious"):
        lines.append(
            f"  Possibly damaged:          {report['suspicious']}  "
            "(no index, but the size does not match a download that stopped early; not marked)"
        )
    if report.get("not_provable"):
        lines.append(
            f"  Not marked:                {report['not_provable']}  "
            "(the row's folder is not under the media folder here, so the file is not provably gone)"
        )
    if report.get("recoverable"):
        if repair:
            lines.append(
                f"  Marked downloaded again:   {report.get('recovered', 0)}  "
                "(marked not downloaded earlier, and the file is at its path)"
            )
            if report.get("recover_failed"):
                lines.append(f"  Could not mark downloaded: {report['recover_failed']}  (see the log)")
        else:
            lines.append(
                f"  File back at its path:     {report['recoverable']}  "
                "(marked not downloaded earlier; --repair marks them downloaded again)"
            )
    if report.get("dimensions_missing"):
        if repair:
            lines.append(
                f"  Photo sizes filled:        {report.get('dimensions_filled', 0)}  (read from the file header)"
            )
            if report.get("dimensions_fill_failed"):
                lines.append(f"  Could not store a size:    {report['dimensions_fill_failed']}  (see the log)")
        else:
            lines.append(
                f"  Photos without size:       {report['dimensions_missing']}  (--repair reads it from the file header)"
            )
    if report.get("dimensions_unreadable"):
        lines.append(f"  Size not readable:         {report['dimensions_unreadable']}  (left as it is)")
    if report["kept"]:
        lines.append(f"  Left alone:                {report['kept']}  (an entry this process cannot read)")
    if report["placeholders"]:
        lines.append(
            f"  Placeholders:              {report['placeholders']}  "
            "(locations, contacts and polls have no file; an old .bin path on them is left as it is)"
        )
    return lines


__all__ = [
    "KEPT",
    "MISSING",
    "NOT_VISIBLE",
    "PLACEHOLDER",
    "PRESENT",
    "REFETCH",
    "RESTORABLE",
    "RESTORED",
    "SUSPICIOUS",
    "TRUNCATED",
    "MediaInspection",
    "chat_folder_alternates",
    "check_media",
    "cut_short_state",
    "file_in_place",
    "format_media_check",
    "image_header_size",
    "inspect_media_row",
    "inspect_media_row_with_db",
    "iso_bmff_incomplete",
    "missing_under_root",
    "repair_media_row",
    "restore_from_copy",
    "shared_link_target",
    "visible_media_root",
]
