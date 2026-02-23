"""On-demand thumbnail generation with disk caching.

Generates WebP thumbnails at whitelisted sizes, stored under
{media_root}/.thumbs/{size}/{folder}/{stem}.webp.
Pillow runs in a thread executor to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

ALLOWED_SIZES: set[int] = {200, 400}
WEBP_QUALITY = 80

# Image extensions we can generate thumbnails for
_IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in _IMAGE_EXTENSIONS


def _thumb_path(media_root: Path, size: int, folder: str, filename: str) -> Path:
    stem = Path(filename).stem
    return media_root / ".thumbs" / str(size) / folder / f"{stem}.webp"


def _generate_sync(source: Path, dest: Path, size: int) -> bool:
    """Blocking thumbnail generation — meant for run_in_executor."""
    try:
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

    Returns None when:
    - size is not in ALLOWED_SIZES
    - source file is not an image
    - source file does not exist
    - generation fails
    """
    if size not in ALLOWED_SIZES:
        return None

    if not _is_image(filename):
        return None

    dest = _thumb_path(media_root, size, folder, filename)
    if dest.exists():
        return dest

    source = media_root / folder / filename
    if not source.exists():
        return None

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _generate_sync, source, dest, size)
    return dest if ok else None
