"""Tests for thumbnail generation (src/web/thumbnails.py)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.web.thumbnails import (
    _IMAGE_EXTENSIONS,
    _MAX_SOURCE_BYTES,
    ALLOWED_SIZES,
    WEBP_QUALITY,
    _generate_sync,
    _is_image,
    _thumb_path,
    ensure_thumbnail,
)


class TestIsImage(unittest.TestCase):
    """Test _is_image file extension detection."""

    def test_recognizes_jpg(self):
        """_is_image returns True for .jpg files."""
        self.assertTrue(_is_image("photo.jpg"))

    def test_recognizes_jpeg(self):
        """_is_image returns True for .jpeg files."""
        self.assertTrue(_is_image("photo.jpeg"))

    def test_recognizes_png(self):
        """_is_image returns True for .png files."""
        self.assertTrue(_is_image("image.png"))

    def test_recognizes_gif(self):
        """_is_image returns True for .gif files."""
        self.assertTrue(_is_image("anim.gif"))

    def test_recognizes_webp(self):
        """_is_image returns True for .webp files."""
        self.assertTrue(_is_image("thumb.webp"))

    def test_recognizes_bmp(self):
        """_is_image returns True for .bmp files."""
        self.assertTrue(_is_image("old.bmp"))

    def test_recognizes_tiff(self):
        """_is_image returns True for .tiff files."""
        self.assertTrue(_is_image("scan.tiff"))

    def test_rejects_mp4(self):
        """_is_image returns False for video files."""
        self.assertFalse(_is_image("video.mp4"))

    def test_rejects_txt(self):
        """_is_image returns False for text files."""
        self.assertFalse(_is_image("readme.txt"))

    def test_rejects_pdf(self):
        """_is_image returns False for pdf files."""
        self.assertFalse(_is_image("doc.pdf"))

    def test_case_insensitive(self):
        """_is_image is case-insensitive for extensions."""
        self.assertTrue(_is_image("PHOTO.JPG"))
        self.assertTrue(_is_image("Image.PNG"))

    def test_no_extension_returns_false(self):
        """_is_image returns False for files without extension."""
        self.assertFalse(_is_image("noext"))


class TestThumbPath(unittest.TestCase):
    """Test _thumb_path output format."""

    def test_returns_webp_in_thumbs_directory(self):
        """_thumb_path returns .webp file under .thumbs/{size}/{folder}/."""
        media = Path("/media")
        result = _thumb_path(media, 200, "chat123", "photo.jpg")
        self.assertEqual(result, Path("/media/.thumbs/200/chat123/photo.webp"))

    def test_preserves_folder_structure(self):
        """_thumb_path preserves the folder subpath."""
        media = Path("/data/media")
        result = _thumb_path(media, 400, "avatars/users", "avatar_123.png")
        self.assertEqual(result, Path("/data/media/.thumbs/400/avatars/users/avatar_123.webp"))

    def test_strips_original_extension(self):
        """_thumb_path uses stem of original filename, not full name."""
        media = Path("/m")
        result = _thumb_path(media, 200, "f", "image.with.dots.jpeg")
        # stem = "image.with.dots" (everything before last .)
        self.assertEqual(result.name, "image.with.dots.webp")


class TestConstants(unittest.TestCase):
    """Test module-level constants are sane."""

    def test_allowed_sizes_contains_200_and_400(self):
        """ALLOWED_SIZES contains exactly 200 and 400."""
        self.assertEqual(ALLOWED_SIZES, {200, 400})

    def test_webp_quality_is_reasonable(self):
        """WEBP_QUALITY is between 1 and 100."""
        self.assertGreaterEqual(WEBP_QUALITY, 1)
        self.assertLessEqual(WEBP_QUALITY, 100)

    def test_max_source_bytes_is_50mb(self):
        """_MAX_SOURCE_BYTES is 50 MB."""
        self.assertEqual(_MAX_SOURCE_BYTES, 50 * 1024 * 1024)

    def test_image_extensions_include_common_formats(self):
        """_IMAGE_EXTENSIONS includes jpg, png, gif, webp."""
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            self.assertIn(ext, _IMAGE_EXTENSIONS)


class TestGenerateSync(unittest.TestCase):
    """Test _generate_sync blocking thumbnail generation."""

    def test_returns_false_when_source_too_large(self):
        """_generate_sync returns False when source exceeds size limit."""
        with tempfile.NamedTemporaryFile(suffix=".jpg") as src:
            source = Path(src.name)
            dest = Path(tempfile.mktemp(suffix=".webp"))

            with patch.object(Path, "stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=_MAX_SOURCE_BYTES + 1)
                result = _generate_sync(source, dest, 200)

            self.assertFalse(result)

    def test_creates_destination_directory(self):
        """_generate_sync creates parent directories for destination."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a tiny valid image using Pillow
            from PIL import Image as PILImage

            source = Path(tmpdir) / "source.png"
            img = PILImage.new("RGB", (10, 10), "red")
            img.save(source)

            dest = Path(tmpdir) / "sub" / "dir" / "thumb.webp"
            result = _generate_sync(source, dest, 200)

            self.assertTrue(result)
            self.assertTrue(dest.exists())
            self.assertTrue(dest.parent.exists())

    def test_output_is_valid_webp(self):
        """_generate_sync produces a valid WebP file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from PIL import Image as PILImage

            source = Path(tmpdir) / "source.png"
            img = PILImage.new("RGB", (500, 500), "blue")
            img.save(source)

            dest = Path(tmpdir) / "thumb.webp"
            result = _generate_sync(source, dest, 200)

            self.assertTrue(result)
            with PILImage.open(dest) as thumb:
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(thumb.width, 200)
                self.assertLessEqual(thumb.height, 200)

    def test_returns_false_on_corrupt_source(self):
        """_generate_sync returns False when source is not a valid image."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "corrupt.jpg"
            source.write_text("not an image")
            dest = Path(tmpdir) / "thumb.webp"

            result = _generate_sync(source, dest, 200)
            self.assertFalse(result)


class TestEnsureThumbnail(unittest.IsolatedAsyncioTestCase):
    """Test ensure_thumbnail async entry point."""

    async def test_rejects_disallowed_size(self):
        """ensure_thumbnail returns None for sizes not in ALLOWED_SIZES."""
        result = await ensure_thumbnail(Path("/tmp"), 999, "folder", "img.jpg")
        self.assertIsNone(result)

    async def test_rejects_non_image_file(self):
        """ensure_thumbnail returns None for non-image files."""
        result = await ensure_thumbnail(Path("/tmp"), 200, "folder", "video.mp4")
        self.assertIsNone(result)

    async def test_rejects_path_traversal_in_source(self):
        """ensure_thumbnail returns None when source escapes media_root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir) / "media"
            media_root.mkdir()
            result = await ensure_thumbnail(media_root, 200, "../..", "etc_passwd.jpg")
            self.assertIsNone(result)

    async def test_returns_cached_thumbnail_if_exists(self):
        """ensure_thumbnail returns existing thumbnail without regenerating."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            # Create source
            folder = "chat1"
            (media_root / folder).mkdir()
            source = media_root / folder / "img.jpg"
            source.write_text("placeholder")

            # Pre-create the thumbnail
            thumb = _thumb_path(media_root, 200, folder, "img.jpg")
            thumb.parent.mkdir(parents=True, exist_ok=True)
            thumb.write_text("cached")

            result = await ensure_thumbnail(media_root, 200, folder, "img.jpg")
            self.assertIsNotNone(result)
            self.assertEqual(result, thumb.resolve())

    async def test_returns_none_when_source_does_not_exist(self):
        """ensure_thumbnail returns None when source file is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            (media_root / "chat1").mkdir()
            result = await ensure_thumbnail(media_root, 200, "chat1", "missing.jpg")
            self.assertIsNone(result)

    async def test_generates_thumbnail_for_valid_source(self):
        """ensure_thumbnail generates a new thumbnail for a valid image source."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from PIL import Image as PILImage

            media_root = Path(tmpdir)
            folder = "chat1"
            (media_root / folder).mkdir()
            source = media_root / folder / "photo.png"
            img = PILImage.new("RGB", (300, 300), "green")
            img.save(source)

            result = await ensure_thumbnail(media_root, 200, folder, "photo.png")
            self.assertIsNotNone(result)
            self.assertTrue(result.exists())
            self.assertEqual(result.suffix, ".webp")


if __name__ == "__main__":
    unittest.main()
