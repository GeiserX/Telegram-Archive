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
from pathlib import Path

from PIL import Image

from .media_utils import legacy_folder_alternates

logger = logging.getLogger(__name__)

# Limit decompression to prevent pixel-bomb OOM attacks (~50 megapixels)
Image.MAX_IMAGE_PIXELS = 50_000_000

ALLOWED_SIZES: set[int] = {200, 400}
WEBP_QUALITY = 80
_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MB

_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}

# Limit concurrent thumbnail generations to cap peak memory (~15MB per decode)
_generation_semaphore = asyncio.Semaphore(8)

_DEFAULT_CACHE_DIR = "/tmp/telegram-archive-thumbs"


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


def _thumb_path(media_root: Path, size: int, folder: str, filename: str) -> Path:
    stem = Path(filename).stem
    return media_root / ".thumbs" / str(size) / folder / f"{stem}.webp"


def _generate_sync(source: Path, dest: Path, size: int) -> bool:
    """Blocking thumbnail generation -- meant for run_in_executor."""
    try:
        if source.stat().st_size > _MAX_SOURCE_BYTES:
            logger.warning("Source too large for thumbnail: %s (%d bytes)", source, source.stat().st_size)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as img:
            img.thumbnail((size, size), Image.LANCZOS)
            img.save(dest, "WEBP", quality=WEBP_QUALITY)
        return True
    except Exception as e:
        logger.warning("Thumbnail generation failed for %s: %s", source, e)
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

    if not _is_image(filename):
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

    async with _generation_semaphore:
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, _generate_sync, source, dest, size)
    return (dest, resolved_folder) if ok else None
