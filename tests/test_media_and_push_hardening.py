"""Regression tests for thumbnail and push-fanout hardening.

Covers four audit findings:
- S5  push fan-out ran blocking HTTP with no timeout, sequentially, on the loop
- S23 thumbnail generation was gated on file bytes, not decoded pixels
- S26 thumbnails were written non-atomically into the live cache path
- S28 failed thumbnail generation was never cached, so it re-ran per request

And two residuals found against the S23 gate:
- the video lane trusted the sender-chosen filename, so a 96 MP still image
  renamed .mp4 bypassed the pixel gate and cost ffmpeg a full-size frame buffer
- the gate read header pixels, refusing large JPEGs that draft() decodes
  cheaply -- valid 96 MP JPEG documents 404ed for no memory benefit

The push half depends on py_vapid/pywebpush, which may be missing locally; a
module-level guard skips those tests gracefully (they run on CI).
"""

import asyncio
import os
import resource
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image as PILImage

import src.web.thumbnails as thumbs
from src.web.thumbnails import _MAX_SOURCE_PIXELS, _generate_sync, _generate_video_sync, ensure_thumbnail

try:
    from src.web.push import PushNotificationManager

    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False
    PushNotificationManager = None  # type: ignore[assignment, misc]


def _skip_unless_push(cls_or_fn):
    """Skip test class/method when push module could not be imported."""
    return unittest.skipUnless(_PUSH_AVAILABLE, "src.web.push import failed (missing py_vapid/pywebpush)")(cls_or_fn)


def _make_enabled_manager():
    """Helper: a PushNotificationManager with push enabled and _vapid stubbed."""
    db = MagicMock()
    cfg = MagicMock()
    cfg.push_notifications = "full"
    cfg.vapid_contact = "mailto:test@example.com"
    mgr = PushNotificationManager(db, cfg)
    mgr._vapid = MagicMock()
    mgr._vapid.sign.return_value = {"Authorization": "vapid t=token"}
    return mgr


def _pixel_bomb_png(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR claims huge dimensions.

    This is the attack shape: a few dozen bytes on disk, ~4 bytes per claimed
    pixel once decoded. Pillow reads the size from the header, so a gate that
    runs before decoding sees the real cost.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


def _write_png(path: Path, size: tuple[int, int], colour: str = "red") -> None:
    img = PILImage.new("RGB", size, colour)
    img.save(path, "PNG")


_flat_png_cache: dict[tuple[int, int], bytes] = {}


def _flat_png(width: int, height: int) -> bytes:
    """A real, fully decodable flat-black PNG, built without a pixel buffer.

    Unlike _pixel_bomb_png above, this file decodes cleanly end to end, so a
    lane that ignores the header and just decodes (ffmpeg on a renamed file)
    pays the full pixel cost -- ~280 KB on disk, 96 MP once decoded at the
    default test size. Rows are streamed through one zlib compressor so the
    test process never holds the raw pixels either.
    """
    cached = _flat_png_cache.get((width, height))
    if cached is not None:
        return cached

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    comp = zlib.compressobj(9)
    row = b"\x00" + b"\x00" * (width * 3)  # filter byte 0 + RGB zeros
    idat = bytearray()
    for _ in range(height):
        idat += comp.compress(row)
    idat += comp.flush()
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", bytes(idat)) + chunk(b"IEND", b"")
    _flat_png_cache[(width, height)] = data
    return data


def _maxrss_bytes(raw: int) -> int:
    """ru_maxrss is bytes on macOS and kibibytes on Linux."""
    return raw if sys.platform == "darwin" else raw * 1024


_REAL_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ============================================================================
# S23 - gate on decoded pixels, not on compressed bytes
# ============================================================================


class TestPixelBudget(unittest.TestCase):
    """_generate_sync must refuse oversized images before decoding them."""

    def test_pixel_bomb_is_refused_without_decoding_a_pixel(self):
        """A 96 MP PNG of 66 bytes passes the byte gate and must still be refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "bomb.png"
            source.write_bytes(_pixel_bomb_png(12000, 8000))
            dest = Path(tmpdir) / "out.webp"

            # Sanity: the existing byte gate cannot see this coming.
            self.assertLess(source.stat().st_size, thumbs._MAX_SOURCE_BYTES)

            decodes = []
            real_load = PILImage.Image.load

            def spy_load(self, *args, **kwargs):
                decodes.append(1)
                return real_load(self, *args, **kwargs)

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with patch.object(PILImage.Image, "load", spy_load):
                    ok = _generate_sync(source, dest, 200)

            self.assertFalse(ok)
            self.assertFalse(dest.exists())
            self.assertEqual(decodes, [], "the pixel bomb was decoded before being refused")

    def test_budget_is_below_pillows_effective_ceiling(self):
        """Pillow only raises above 2x MAX_IMAGE_PIXELS, so our gate must be stricter."""
        self.assertLess(_MAX_SOURCE_PIXELS, 2 * PILImage.MAX_IMAGE_PIXELS)
        self.assertLess(_MAX_SOURCE_PIXELS, 12000 * 8000)

    def test_ordinary_image_still_generates(self):
        """The gate must not refuse a normal photo-sized source."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "photo.png"
            _write_png(source, (300, 200))
            dest = Path(tmpdir) / "thumbs" / "photo.webp"
            dest.parent.mkdir()

            self.assertTrue(_generate_sync(source, dest, 200))
            with PILImage.open(dest) as img:
                self.assertEqual(img.format, "WEBP")

    def test_source_just_over_the_budget_is_refused(self):
        """The gate fires on pixel count alone, at whatever budget is configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "wide.png"
            _write_png(source, (100, 100))
            dest = Path(tmpdir) / "wide.webp"

            with patch.object(thumbs, "_MAX_SOURCE_PIXELS", 9999):
                self.assertFalse(_generate_sync(source, dest, 200))
            self.assertFalse(dest.exists())


# ============================================================================
# S26 - the cache path is either absent or complete
# ============================================================================


class TestAtomicThumbnailWrite(unittest.TestCase):
    """A concurrent reader must never observe a partially written thumbnail."""

    def _crash_midway(self, partial: bytes = b"RIFF-truncated"):
        """A save() that writes some bytes and then dies, as a full disk would."""
        real_save = PILImage.Image.save

        def failing_save(img_self, fp, *args, **kwargs):
            Path(fp).write_bytes(partial)
            raise OSError("no space left on device")

        return real_save, failing_save

    def test_crash_midway_leaves_no_truncated_file_in_the_cache(self):
        """An interrupted image write must not leave a servable, cacheable stub."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "photo.png"
            _write_png(source, (300, 200))
            dest = Path(tmpdir) / "thumbs" / "photo.webp"

            _real, failing_save = self._crash_midway()
            with patch.object(PILImage.Image, "save", failing_save):
                ok = _generate_sync(source, dest, 200)

            self.assertFalse(ok)
            self.assertFalse(dest.exists(), "a truncated thumbnail was left in the cache path")

    def test_video_crash_midway_leaves_no_truncated_file(self):
        """Same guarantee on the video path, which writes through the same helper."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "clip.mp4"
            source.write_bytes(b"\x00" * 128)
            dest = Path(tmpdir) / "thumbs" / "clip.webp"
            frame = Path(tmpdir) / "frame.jpg"
            _write_png(frame, (64, 64))  # Pillow picks the format from the data, not the name

            def fake_ffmpeg(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(frame.read_bytes())
                return MagicMock(returncode=0)

            _real, failing_save = self._crash_midway()
            with (
                patch.object(thumbs, "_check_ffmpeg", return_value=True),
                patch.object(thumbs.subprocess, "run", fake_ffmpeg),
                patch.object(PILImage.Image, "save", failing_save),
            ):
                ok = _generate_video_sync(source, dest, 200)

            self.assertFalse(ok)
            self.assertFalse(dest.exists(), "a truncated video thumbnail was left in the cache path")

    def test_cache_path_never_exists_while_the_write_is_in_flight(self):
        """dest.exists() is the only completeness check callers have; keep it honest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "photo.png"
            _write_png(source, (300, 200))
            dest = Path(tmpdir) / "thumbs" / "photo.webp"

            observed = []
            written_to = []
            real_save = PILImage.Image.save

            def spy_save(img_self, fp, *args, **kwargs):
                result = real_save(img_self, fp, *args, **kwargs)
                # What a concurrent request would see at this instant.
                written_to.append(Path(fp))
                observed.append(dest.exists())
                return result

            with patch.object(PILImage.Image, "save", spy_save):
                ok = _generate_sync(source, dest, 200)

            self.assertTrue(ok)
            self.assertEqual(observed, [False], "the cache path was visible before the write finished")
            self.assertNotIn(dest, written_to, "Pillow wrote straight into the live cache path")
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as img:
                self.assertEqual(img.format, "WEBP")

    def test_no_temp_files_are_left_behind(self):
        """Success and failure both clean up the temp file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "photo.png"
            _write_png(source, (300, 200))
            dest = Path(tmpdir) / "thumbs" / "photo.webp"

            self.assertTrue(_generate_sync(source, dest, 200))
            self.assertEqual([p.name for p in dest.parent.iterdir()], ["photo.webp"])

            _real, failing_save = self._crash_midway()
            with patch.object(PILImage.Image, "save", failing_save):
                _generate_sync(source, dest.parent / "other.webp", 200)
            self.assertEqual([p.name for p in dest.parent.iterdir()], ["photo.webp"])


# ============================================================================
# S28 - failures are cached too, so broken media costs one attempt, not one per request
# ============================================================================


class TestFailedGenerationIsNotRepeated(unittest.IsolatedAsyncioTestCase):
    """ensure_thumbnail must not re-run an expensive generation that just failed."""

    def setUp(self):
        thumbs._recent_failures.clear()

    def tearDown(self):
        thumbs._recent_failures.clear()

    async def _request_three_times(self, filename: str, gen_name: str):
        calls = []

        def failing_generator(source, dest, size):
            calls.append(1)
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            (media_root / "chat1").mkdir()
            (media_root / "chat1" / filename).write_bytes(b"\x00" * 64)

            with patch.object(thumbs, gen_name, failing_generator):
                for _ in range(3):
                    self.assertIsNone(await ensure_thumbnail(media_root, 200, "chat1", filename))
        return calls

    async def test_undecodable_video_runs_ffmpeg_once_not_per_request(self):
        """The expensive path (two 15s ffmpeg attempts) must not repeat per request."""
        calls = await self._request_three_times("clip.mp4", "_generate_video_sync")
        self.assertEqual(len(calls), 1, "video thumbnail generation re-ran for a known-bad file")

    async def test_undecodable_image_is_also_only_attempted_once(self):
        """Same guarantee on the image path — one failure, one attempt."""
        calls = await self._request_three_times("broken.png", "_generate_sync")
        self.assertEqual(len(calls), 1, "image thumbnail generation re-ran for a known-bad file")

    async def test_negative_result_expires_so_a_repaired_file_recovers(self):
        """A truncated download that later completes must not stay 404 forever."""
        calls = []

        def failing_generator(source, dest, size):
            calls.append(1)
            return False

        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            (media_root / "chat1").mkdir()
            (media_root / "chat1" / "clip.mp4").write_bytes(b"\x00" * 64)

            with patch.object(thumbs, "_generate_video_sync", failing_generator):
                await ensure_thumbnail(media_root, 200, "chat1", "clip.mp4")
                # Age the recorded failure past its TTL.
                for key in list(thumbs._recent_failures):
                    thumbs._recent_failures[key] = time.monotonic() - 1
                await ensure_thumbnail(media_root, 200, "chat1", "clip.mp4")

        self.assertEqual(len(calls), 2, "an expired negative result was not retried")

    async def test_failure_cache_is_bounded(self):
        """The negative cache must not grow without limit."""
        for index in range(thumbs._MAX_FAILURE_ENTRIES + 50):
            thumbs._record_failure((200, f"/media/chat1/file{index}.mp4"))
        self.assertLessEqual(len(thumbs._recent_failures), thumbs._MAX_FAILURE_ENTRIES)


# ============================================================================
# S5 - push fan-out is bounded, off the event loop, and concurrent
# ============================================================================


@_skip_unless_push
class TestPushFanout(unittest.IsolatedAsyncioTestCase):
    """One unreachable push endpoint must not stall the viewer."""

    def _subs(self, count: int):
        return [
            {"endpoint": f"https://push.example.com/sub{i}", "keys": {"p256dh": "k", "auth": "a"}} for i in range(count)
        ]

    async def test_webpush_is_given_a_timeout(self):
        """pywebpush forwards timeout to requests.post; omitting it means no timeout at all."""
        mgr = _make_enabled_manager()
        mgr.get_subscriptions = AsyncMock(return_value=self._subs(1))

        with patch("src.web.push.webpush") as mock_webpush:
            await mgr.send_notification("Title", "Body", chat_id=1)

        timeout = mock_webpush.call_args.kwargs.get("timeout")
        self.assertIsNotNone(timeout, "webpush was called without a timeout")
        self.assertGreater(timeout, 0)

    async def test_slow_endpoints_neither_block_the_loop_nor_serialize(self):
        """A hung push service must not freeze the event loop or the other sends."""
        mgr = _make_enabled_manager()
        mgr.get_subscriptions = AsyncMock(return_value=self._subs(4))

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        def slow_webpush(**kwargs):
            time.sleep(0.15)

        beat = asyncio.create_task(heartbeat())
        try:
            with patch("src.web.push.webpush", slow_webpush):
                started = time.monotonic()
                sent = await mgr.send_notification("Title", "Body", chat_id=1)
                elapsed = time.monotonic() - started
        finally:
            beat.cancel()

        self.assertEqual(sent, 4)
        # Sequential on-loop sends would be 4 x 0.15s = 0.6s with zero heartbeats.
        self.assertLess(elapsed, 0.45, f"sends did not run concurrently (took {elapsed:.2f}s)")
        self.assertGreater(ticks, 0, "the event loop was blocked for the whole fan-out")

    async def test_one_failing_endpoint_does_not_stop_the_others(self):
        """Delivery to healthy subscribers survives a broken one."""
        mgr = _make_enabled_manager()
        mgr.get_subscriptions = AsyncMock(return_value=self._subs(3))
        mgr.unsubscribe = AsyncMock()

        calls = []

        def flaky_webpush(**kwargs):
            calls.append(kwargs["subscription_info"]["endpoint"])
            if kwargs["subscription_info"]["endpoint"].endswith("sub1"):
                raise OSError("connection reset")

        with patch("src.web.push.webpush", flaky_webpush):
            sent = await mgr.send_notification("Title", "Body", chat_id=1)

        self.assertEqual(sent, 2)
        self.assertEqual(len(calls), 3)


# ============================================================================
# Residual 1 - the video lane is routed by the sender-chosen filename, so a
# still image renamed .mp4 reaches ffmpeg's decoder; -max_pixels bounds it
# ============================================================================


class TestVideoDecodeIsBounded(unittest.TestCase):
    """_generate_video_sync must cap ffmpeg's decode at the pixel budget."""

    def test_ffmpeg_command_carries_the_pixel_cap(self):
        """The bound lives in the ffmpeg argv: -max_pixels == the pixel budget.

        Without it, ffmpeg sizes its frame buffer from whatever dimensions the
        bytes declare, so a 96 MP still renamed .mp4 allocates ~675 MB. This is
        the deterministic half of the proof; the real-tool test below shows the
        flag actually refusing the bomb at ~16 MB.
        """
        commands = []

        def fake_ffmpeg(cmd, **kwargs):
            commands.append(cmd)
            PILImage.new("RGB", (64, 64), "blue").save(cmd[-1], "JPEG")
            return MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "clip.mp4"
            source.write_bytes(b"\x00" * 128)
            dest = Path(tmpdir) / "thumbs" / "clip.webp"
            with (
                patch.object(thumbs, "_check_ffmpeg", return_value=True),
                patch.object(thumbs.subprocess, "run", fake_ffmpeg),
            ):
                ok = _generate_video_sync(source, dest, 200)

        self.assertTrue(ok)
        self.assertTrue(commands, "ffmpeg was never invoked")
        for cmd in commands:
            self.assertIn("-max_pixels", cmd, "ffmpeg was invoked without a pixel cap")
            idx = cmd.index("-max_pixels")
            self.assertEqual(
                int(cmd[idx + 1]),
                thumbs._MAX_SOURCE_PIXELS,
                "the ffmpeg pixel cap does not match the configured budget",
            )
            # The cap must precede -i so it constrains the decoder that reads it.
            self.assertLess(idx, cmd.index("-i"), "-max_pixels came after the input; ffmpeg ignores it there")


@unittest.skipUnless(_REAL_FFMPEG, "needs real ffmpeg on PATH")
class TestVideoLaneAgainstRealTools(unittest.TestCase):
    """The measured attack, end to end: the same 96 MP flat PNG, renamed .mp4."""

    def test_renamed_pixel_bomb_is_refused_without_a_large_decode(self):
        """The ~280 KB / 96 MP PNG behind a .mp4 name cost ffmpeg ~675 MB RSS;
        -max_pixels now makes the decoder refuse the frame at a small cost, so
        no thumbnail is produced and no large buffer is ever allocated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "bomb.mp4"  # sender-chosen name routes to the video lane
            source.write_bytes(_flat_png(12000, 8000))
            dest = Path(tmpdir) / "thumbs" / "bomb.webp"

            children_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            ok = _generate_video_sync(source, dest, 200)
            children_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

            self.assertFalse(ok, "the renamed pixel bomb produced a video thumbnail")
            self.assertFalse(dest.exists())
            child_growth = _maxrss_bytes(children_after) - _maxrss_bytes(children_before)
            self.assertLess(
                child_growth,
                300 * 1024 * 1024,
                f"a decode child peaked {child_growth} bytes above the previous high-water mark "
                "(the ~675 MB frame buffer was allocated)",
            )

    def test_genuine_small_video_still_thumbnails(self):
        """The bound must not cost real, in-budget videos their thumbnails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "clip.mp4"
            created = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=duration=2:size=64x64:rate=10",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    str(source),
                ],
                capture_output=True,
                timeout=60,
            )
            if created.returncode != 0 or not source.exists():
                self.skipTest("local ffmpeg cannot synthesize a test video")

            dest = Path(tmpdir) / "thumbs" / "clip.webp"
            self.assertTrue(_generate_video_sync(source, dest, 200))
            with PILImage.open(dest) as img:
                self.assertEqual(img.format, "WEBP")
                self.assertLessEqual(max(img.size), 200)


# ============================================================================
# Residual 2 - the pixel gate must measure what Pillow will decode (draft()),
# not what the header declares, or large valid JPEGs 404 for no benefit
# ============================================================================

# Runs _generate_sync in a fresh interpreter and reports its peak RSS. A child
# process is the only honest gauge here: ru_maxrss is a high-water mark, so in
# the test process the source image we just created would mask the decode.
_CHILD_DECODE_SCRIPT = """
import resource
import sys
from pathlib import Path

from src.web.thumbnails import _generate_sync

ok = _generate_sync(Path(sys.argv[1]), Path(sys.argv[2]), 200)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform != "darwin":
    peak *= 1024
print(int(ok), peak)
"""

_REPO_ROOT = Path(thumbs.__file__).resolve().parents[2]


class TestDraftAwarePixelGate(unittest.TestCase):
    """96 MP JPEG -> thumbnail (draft decodes ~1.5 MP); 96 MP PNG -> refused."""

    def test_large_valid_jpeg_still_thumbnails_in_bounded_memory(self):
        """A 96 MP JPEG drafts down to a ~1.5 MP decode and must not 404."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "document.jpg"
            page = PILImage.new("RGB", (12000, 8000), (40, 90, 130))
            page.save(source, "JPEG", quality=60)
            del page
            dest = Path(tmpdir) / "thumbs" / "document.webp"

            child = subprocess.run(
                [sys.executable, "-c", _CHILD_DECODE_SCRIPT, str(source), str(dest)],
                capture_output=True,
                timeout=120,
                cwd=_REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            )
            self.assertEqual(child.returncode, 0, "decode child crashed")
            ok_flag, peak = child.stdout.split()

            self.assertEqual(int(ok_flag), 1, "a 96 MP JPEG was refused; the gate ignored draft()")
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as img:
                self.assertEqual(img.format, "WEBP")
                self.assertLessEqual(max(img.size), 200)
            self.assertLess(
                int(peak),
                250 * 1024 * 1024,
                f"decode peaked at {int(peak)} bytes; draft() was not applied before decoding",
            )

    def test_decodable_png_bomb_of_the_same_size_is_still_refused(self):
        """The same 96 MP as a real PNG has no draft path and stays refused,
        without decoding a pixel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "bomb.png"
            source.write_bytes(_flat_png(12000, 8000))
            dest = Path(tmpdir) / "out.webp"

            decodes = []
            real_load = PILImage.Image.load

            def spy_load(img_self, *args, **kwargs):
                decodes.append(1)
                return real_load(img_self, *args, **kwargs)

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with patch.object(PILImage.Image, "load", spy_load):
                    ok = _generate_sync(source, dest, 200)

            self.assertFalse(ok, "a fully decodable 96 MP PNG got past the pixel gate")
            self.assertFalse(dest.exists())
            self.assertEqual(decodes, [], "the PNG bomb was decoded before being refused")

    def test_small_jpeg_is_unaffected_by_the_draft_pass(self):
        """draft() on an already-small JPEG is a no-op; output stays correct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "photo.jpg"
            PILImage.new("RGB", (300, 200), (200, 30, 30)).save(source, "JPEG")
            dest = Path(tmpdir) / "thumbs" / "photo.webp"

            self.assertTrue(_generate_sync(source, dest, 200))
            with PILImage.open(dest) as img:
                self.assertEqual(img.format, "WEBP")
                self.assertEqual(img.size, (200, 133))
