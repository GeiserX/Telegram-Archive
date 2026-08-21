"""Tests for _pre_generate_thumbnail in telegram_backup.py."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image as PILImage

from src.telegram_backup import _pre_generate_thumbnail


class TestPreGenerateThumbnail(unittest.TestCase):
    """Test pre-generation of thumbnails during backup."""

    def _make_image(self, path: Path, size=(300, 300)):
        path.parent.mkdir(parents=True, exist_ok=True)
        img = PILImage.new("RGB", size, "red")
        img.save(path)

    def test_generates_thumbnail_for_valid_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "photo.jpg"
            self._make_image(source)

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "photo.webp"
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as thumb:
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(thumb.width, 200)
                self.assertLessEqual(thumb.height, 200)

    def test_skips_non_image_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "document.pdf"
            source.parent.mkdir(parents=True)
            source.write_text("not an image")

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "document.webp"
            self.assertFalse(dest.exists())

    def test_skips_nonexistent_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            _pre_generate_thumbnail(str(media_root / "missing.jpg"), str(media_root))
            # Should not raise, just return silently

    def test_skips_file_exceeding_size_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "huge.jpg"
            self._make_image(source)

            from unittest.mock import MagicMock, patch

            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value = MagicMock(st_size=51 * 1024 * 1024)
                _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "huge.webp"
            self.assertFalse(dest.exists())

    def test_skips_source_outside_media_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir) / "media"
            media_root.mkdir()
            outside = Path(tmpdir) / "outside" / "photo.jpg"
            self._make_image(outside)

            _pre_generate_thumbnail(str(outside), str(media_root))

            # No thumbnail generated anywhere under media_root
            thumbs_dir = media_root / ".thumbs"
            self.assertFalse(thumbs_dir.exists())

    def test_skips_if_thumbnail_already_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "photo.png"
            self._make_image(source)

            dest = media_root / ".thumbs" / "200" / "chat1" / "photo.webp"
            dest.parent.mkdir(parents=True)
            dest.write_text("existing")

            _pre_generate_thumbnail(str(source), str(media_root))

            # Should not overwrite
            self.assertEqual(dest.read_text(), "existing")

    def test_handles_nested_folder_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "deep" / "nested" / "dir" / "img.png"
            self._make_image(source)

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "deep" / "nested" / "dir" / "img.webp"
            self.assertTrue(dest.exists())

    def test_handles_corrupt_image_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "corrupt.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"not a valid image file content")

            # Should not raise
            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "corrupt.webp"
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()


class TestPreGenerateVideoThumbnail(unittest.TestCase):
    """Video thumbnails pre-generate at capture time, not first view (#9t6.6.11)."""

    def test_video_extension_routes_to_video_generator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fake-video-bytes")

            with patch("src.web.thumbnails._generate_video_sync") as gen:
                _pre_generate_thumbnail(str(source), str(media_root))

            gen.assert_called_once()
            called_source, called_dest, called_size = gen.call_args.args
            self.assertEqual(called_source, source)
            self.assertEqual(called_dest, media_root / ".thumbs" / "200" / "chat1" / "clip.webp")
            self.assertEqual(called_size, 200)

    def test_video_skipped_when_thumbnail_already_cached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "clip.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fake-video-bytes")
            dest = media_root / ".thumbs" / "200" / "chat1" / "clip.webp"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"existing")

            with patch("src.web.thumbnails._generate_video_sync") as gen:
                _pre_generate_thumbnail(str(source), str(media_root))

            gen.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_video_end_to_end(self):
        """A real 2s clip produces a real WebP through the actual ffmpeg lane."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "real.mp4"
            source.parent.mkdir(parents=True)
            build = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10", str(source)],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(build.returncode, 0, build.stderr.decode()[-300:])

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "real.webp"
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as thumb:
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(thumb.width, 200)
