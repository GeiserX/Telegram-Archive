"""On-demand thumbnail generation with disk caching.

Generates WebP thumbnails at whitelisted sizes, stored under
{cache_dir}/{size}/{folder}/{stem}.webp.
Pillow runs in a thread executor to avoid blocking the async event loop.

The cache directory is separate from the media root so thumbnails work
even when the media volume is mounted read-only.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

from ..message_utils import describe_exception
from .media_utils import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, legacy_folder_alternates

logger = logging.getLogger(__name__)

# Limit decompression to prevent pixel-bomb OOM attacks (~50 megapixels)
Image.MAX_IMAGE_PIXELS = 50_000_000

ALLOWED_SIZES: set[int] = {200, 400}
WEBP_QUALITY = 80
_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MB
# Peak decode memory is set by pixel count, not by compressed size, so the byte
# gate above cannot bound it: a 12000x8000 PNG of flat colour is under 300 KB on
# disk and still costs ~390 MB to decode (Pillow holds ~4 bytes per pixel while
# resizing). Image.MAX_IMAGE_PIXELS is no help either -- Pillow only raises above
# TWICE that value, so everything up to 100 MP proceeds after a warning nobody
# reads. 25 MP caps what a single decode may cost, and the Semaphore(8) below
# caps how many decodes run at once, so peak thumbnail memory stays a small
# multiple of one capped decode instead of growing with request count -- and
# the cap still covers every Telegram-compressed photo and ordinary camera
# image. The image lane compares this against the size Pillow will actually
# decode (after draft(), see _generate_sync); the video lane passes it to
# ffmpeg as -max_pixels so the decoder itself refuses an oversized frame.
_MAX_SOURCE_PIXELS = 25_000_000

_IMAGE_EXTENSIONS: set[str] = {f".{ext}" for ext in IMAGE_EXTENSIONS}
_VIDEO_EXTENSIONS: set[str] = {f".{ext}" for ext in VIDEO_EXTENSIONS}

# Limit concurrent thumbnail generations to cap peak memory (see _MAX_SOURCE_PIXELS)
_generation_semaphore = asyncio.Semaphore(8)
# Video thumbnails are heavier (ffmpeg subprocess) — lower concurrency limit
_video_semaphore = asyncio.Semaphore(2)

_DEFAULT_CACHE_DIR = "/tmp/telegram-archive-thumbs"

# Remember recent generation failures. Nothing is written when generation fails,
# so without this an undecodable video re-runs ffmpeg (up to two 15s subprocess
# attempts, holding one of only two video slots) on every single request, for
# every viewer, forever. Time-bounded rather than permanent so a truncated
# download that later completes -- or a media volume that was briefly away --
# recovers on its own.
_FAILURE_TTL_SECONDS = 300.0
_MAX_FAILURE_ENTRIES = 1024
_recent_failures: dict[tuple[int, str], float] = {}

# Collapse duplicate concurrent generations: a cold gallery grid (or a reload
# during first render) fires many requests for the same missing thumbnail, and
# each one would decode the same source again — the semaphore caps how many run
# at once, not how many run. The first request generates; every concurrent
# duplicate waits on the same per-destination lock and then finds the finished
# file (or the cached failure). Entries are removed as soon as the generation
# settles, so the dict stays bounded by in-flight distinct thumbnails.
_inflight_locks: dict[str, asyncio.Lock] = {}


def _failure_cached(key: tuple[int, str]) -> bool:
    """True when this (size, source) failed recently enough to skip retrying."""
    expires_at = _recent_failures.get(key)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        _recent_failures.pop(key, None)
        return False
    return True


def _record_failure(key: tuple[int, str]) -> None:
    """Remember a failed generation for _FAILURE_TTL_SECONDS."""
    now = time.monotonic()
    if len(_recent_failures) >= _MAX_FAILURE_ENTRIES:
        for stale in [k for k, expires_at in _recent_failures.items() if expires_at <= now]:
            del _recent_failures[stale]
        if len(_recent_failures) >= _MAX_FAILURE_ENTRIES:
            # Dropping entries only costs a regeneration, never correctness.
            _recent_failures.clear()
    _recent_failures[key] = now + _FAILURE_TTL_SECONDS


def _save_webp_atomic(img: Image.Image, dest: Path) -> None:
    """Write the WebP through a temp file in dest's directory, then os.replace().

    Pillow streams straight into whatever path it is given, so writing to the
    cache path directly makes a half-written file visible to any concurrent
    request -- dest.exists() is the only completeness check there is, and the
    truncated result is then served with a 24h Cache-Control. os.replace() is
    atomic, so the cache path only ever appears fully written.
    """
    # Short fixed prefix: media names are already near the filesystem's
    # per-component byte budget, so embedding one here could overflow it.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".thumb-", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        img.save(tmp_path, "WEBP", quality=WEBP_QUALITY)
        os.replace(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def resolve_cache_dir(media_root: Path | None) -> Path:
    """Determine the thumbnail cache directory.

    Priority: THUMBNAIL_CACHE_DIR env > {media_root}/.thumbs (if writable) > /tmp fallback.
    """
    env_dir = os.environ.get("THUMBNAIL_CACHE_DIR")
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if media_root:
        candidate = media_root / ".thumbs"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Verify actual write access (dir may exist on a read-only mount)
            probe = candidate / ".write_test"
            probe.touch()
            probe.unlink()
            return candidate
        except OSError:
            pass

    p = Path(_DEFAULT_CACHE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTENSIONS


def _is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in _VIDEO_EXTENSIONS


_FFMPEG_AVAILABLE: bool | None = None


def _check_ffmpeg() -> bool:
    global _FFMPEG_AVAILABLE
    if _FFMPEG_AVAILABLE is None:
        _FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
    return _FFMPEG_AVAILABLE


def _generate_video_sync(source: Path, dest: Path, size: int) -> bool:
    """Extract a frame from video and create thumbnail — blocking."""
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES * 4:
            return False
        if not _check_ffmpeg():
            logger.debug("ffmpeg not available for video thumbnails")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            # Try at 1s first; fall back to first frame for very short videos
            for seek_time in ("00:00:01", "00:00:00"):
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        # The lane is chosen by the sender-controlled filename
                        # extension, and the byte gate above cannot see pixel
                        # cost, so a 96 MP still renamed .mp4 would otherwise
                        # drive ffmpeg's decoder to allocate a ~675 MB frame.
                        # -max_pixels makes the decoder refuse any frame over
                        # the cap (exit non-zero at ~16 MB, handled below),
                        # which bounds the child for both crafted stills and
                        # genuinely oversized video streams; ordinary videos
                        # decode normally.
                        "-max_pixels",
                        str(_MAX_SOURCE_PIXELS),
                        "-ss",
                        seek_time,
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                        tmp_path,
                    ],
                    capture_output=True,
                    timeout=15,
                )
                if result.returncode == 0 and Path(tmp_path).stat().st_size > 0:
                    break
            else:
                return False
            with Image.open(tmp_path) as img:
                _save_webp_atomic(img, dest)
            return True
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Video thumbnail generation failed: %s", describe_exception(e))
        return False


def _thumb_path(media_root: Path, size: int, folder: str, filename: str) -> Path:
    stem = Path(filename).stem
    return media_root / ".thumbs" / str(size) / folder / f"{stem}.webp"


def _generate_sync(source: Path, dest: Path, size: int) -> bool:
    """Blocking thumbnail generation -- meant for run_in_executor."""
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES:
            logger.warning("Source too large for thumbnail (%d bytes)", source.stat().st_size)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            # Image.open() parses only the header, so nothing is decoded yet.
            # draft() next: formats that support it (JPEG) decode thumbnails at
            # a reduced scale -- size * 2 asks for the same reduction that
            # thumbnail()'s reducing_gap would -- so a 96 MP JPEG really costs
            # ~1.5 MP and must not be refused for pixels it never decodes.
            # Formats without draft support (PNG, BMP) keep their full size
            # here, and those are exactly the decode bombs the gate is for.
            img.draft(None, (size * 2, size * 2))
            pixels = img.size[0] * img.size[1]
            if pixels > _MAX_SOURCE_PIXELS:
                logger.warning("Source too large for thumbnail (%d pixels)", pixels)
                return False
            img.thumbnail((size, size), Image.LANCZOS)
            _save_webp_atomic(img, dest)
        return True
    except Exception as e:
        logger.warning("Thumbnail generation failed: %s", describe_exception(e))
        return False


async def ensure_thumbnail(
    media_root: Path, size: int, folder: str, filename: str, *, cache_dir: Path | None = None
) -> tuple[Path, str] | None:
    """Return (thumb_path, resolved_folder) or None.

    resolved_folder is the actual folder the source was found in (may differ
    from the requested folder due to legacy ID fallback). Callers use this
    for ACL enforcement on the resolved path.

    When cache_dir is provided, thumbnails are written there instead of
    under {media_root}/.thumbs/ — this supports read-only media volumes.
    """
    if size not in ALLOWED_SIZES:
        return None

    is_img = _is_image(filename)
    is_vid = _is_video(filename)
    if not is_img and not is_vid:
        return None

    # Path traversal protection: resolve and verify containment
    media_root_resolved = media_root.resolve()

    source = (media_root / folder / filename).resolve()
    if not source.is_relative_to(media_root_resolved):
        return None

    if cache_dir:
        stem = Path(filename).stem
        dest = (cache_dir / str(size) / folder / f"{stem}.webp").resolve()
        if not dest.is_relative_to(cache_dir.resolve()):
            return None
    else:
        dest = _thumb_path(media_root, size, folder, filename).resolve()
        thumbs_root = (media_root / ".thumbs").resolve()
        if not dest.is_relative_to(thumbs_root):
            return None

    resolved_folder = folder

    if dest.exists():
        return dest, resolved_folder

    if not source.exists():
        alt_folders = legacy_folder_alternates(folder)
        found = False
        for alt in alt_folders:
            try:
                alt_source = (media_root / alt / filename).resolve()
                if alt_source.is_relative_to(media_root_resolved) and alt_source.exists():
                    logger.debug("Thumbnail legacy fallback resolved via alternate folder")
                    source = alt_source
                    resolved_folder = alt
                    found = True
                    break
            except OSError, RuntimeError:
                continue
        if not found:
            return None

    failure_key = (size, str(source))
    if _failure_cached(failure_key):
        return None

    dest_key = str(dest)
    lock = _inflight_locks.setdefault(dest_key, asyncio.Lock())
    try:
        async with lock:
            # A duplicate that waited here finds the owner's result: the file
            # for a success, the failure cache for a failure. Either way the
            # wait replaces a second decode of the same source.
            if dest.exists():
                return dest, resolved_folder
            if _failure_cached(failure_key):
                return None
            sem = _video_semaphore if is_vid else _generation_semaphore
            async with sem:
                loop = asyncio.get_running_loop()
                gen_fn = _generate_video_sync if is_vid else _generate_sync
                ok = await loop.run_in_executor(None, gen_fn, source, dest, size)
    finally:
        _inflight_locks.pop(dest_key, None)
    if not ok:
        _record_failure(failure_key)
        return None
    return dest, resolved_folder
