"""On-demand thumbnail generation with disk caching.

Generates WebP thumbnails at whitelisted sizes, stored under
{media_root}/.thumbs/{size}/{folder}/{stem}.webp.
Pillow runs in a thread executor to avoid blocking the async event loop.
"""

import asyncio
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# Limit decompression to prevent pixel-bomb OOM attacks (~50 megapixels)
Image.MAX_IMAGE_PIXELS = 50_000_000

ALLOWED_SIZES: set[int] = {200, 400}
WEBP_QUALITY = 80
_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MB

_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


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


async def ensure_thumbnail(media_root: Path, size: int, folder: str, filename: str) -> Path | None:
    """Return the path to a cached thumbnail, generating it if needed.

    Returns None when the request is invalid or generation fails.
    Includes path traversal protection.
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

    dest = _thumb_path(media_root, size, folder, filename).resolve()
    thumbs_root = (media_root / ".thumbs").resolve()
    if not dest.is_relative_to(thumbs_root):
        return None

    if dest.exists():
        return dest

    if not source.exists():
        return None

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _generate_sync, source, dest, size)
    return dest if ok else None
