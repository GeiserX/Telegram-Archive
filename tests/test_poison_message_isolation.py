"""Unreadable documents must never crash a capture path.

Telegram returns ``documentEmpty`` for a document that is no longer retrievable.
Telethon deserializes that into ``DocumentEmpty``, which is truthy but carries no
``.attributes``. Walking it raises ``AttributeError``, which used to abort the whole
dialog in the sweep and drop the message outright in the listener -- and because the
sync cursor is checkpointed before the offending message, every later run resumed at
the same message and failed the same way, so that chat never advanced again.

These tests pin the defensive probe in every copy of the pattern, plus the
per-message isolation that keeps ANY future shape drift from wedging a chat the
same way, and the neighbouring capture-path guarantees audited alongside it:
peer-resolution errors never reaching the logs, terminal auth errors failing
fast, and the listener's media download holding the client-wide flood threshold
for one attempt only while never leaving a ``.part`` file behind.
"""

import asyncio
import os
import shutil
import struct
import tempfile
import unittest
import warnings
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image as PILImage
from PIL import ImageFile
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    SessionRevokedError,
)
from telethon.tl.types import DocumentEmpty, MessageMediaDocument, MessageMediaPhoto

from src.listener import TelegramListener
from src.telegram_backup import (
    _MAX_SOURCE_PIXELS,
    TelegramBackup,
    _pre_generate_thumbnail,
    call_with_flood_retry,
)

# Telethon's peer-resolution ValueError spells the id out in its message; the
# fake below is the exact template (telethon/client/users.py).
PEER_ERROR_TEXT = "Could not find the input entity for PeerUser(user_id=555000111)"
PEER_ID_IN_TEXT = "555000111"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _poison_media():
    """The exact shape Telegram sends for an expired document."""
    return MessageMediaDocument(document=DocumentEmpty(id=99887766))


class TestDocumentEmptyIsNotFatal(unittest.TestCase):
    """Both capture paths classify an expired document instead of raising."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.listener = TelegramListener.__new__(TelegramListener)

    def test_documentempty_is_truthy_but_has_no_attributes(self):
        """Guard the assumption the whole bug rests on."""
        doc = DocumentEmpty(id=99887766)
        self.assertTrue(doc, "DocumentEmpty is truthy, which is why the guard is needed")
        self.assertFalse(hasattr(doc, "attributes"))

    def test_backup_get_media_type_returns_none(self):
        self.assertIsNone(self.backup._get_media_type(_poison_media()))

    def test_listener_get_media_type_returns_none(self):
        self.assertIsNone(self.listener._get_media_type(_poison_media()))

    def test_backup_get_media_filename_does_not_raise(self):
        message = MagicMock()
        message.reply_to = None
        message.id = 4242
        message.media = _poison_media()
        name = self.backup._get_media_filename(message, "document", telegram_file_id="fid1")
        self.assertIsInstance(name, str)
        self.assertTrue(name)

    def test_listener_get_media_filename_does_not_raise(self):
        message = MagicMock()
        message.reply_to = None
        message.id = 4242
        message.media = _poison_media()
        name = self.listener._get_media_filename(message, "document", telegram_file_id="fid1")
        self.assertIsInstance(name, str)
        self.assertTrue(name)

    def test_real_document_still_classified(self):
        """Positive control for the guard itself: a normal document is unaffected."""
        attr = MagicMock()
        attr.file_name = "report.pdf"
        type(attr).__name__ = "DocumentAttributeFilename"
        document = MagicMock()
        document.attributes = [attr]
        document.mime_type = "application/pdf"
        media = MagicMock(spec=MessageMediaDocument)
        media.document = document
        self.assertEqual(self.backup._get_media_type(media), "document")
        self.assertEqual(self.listener._get_media_type(media), "document")


class TestOneBadMessageCannotWedgeAChat(unittest.TestCase):
    """Per-message isolation in the sweep and in gap-fill.

    The probe above fixes the shape we know about. This pins the property that
    matters for the ones we don't: a message _process_message cannot handle must
    not abort the dialog, and the sync cursor must never move past it -- if it
    did, the message would be skipped forever instead of retried.
    """

    CHAT_ID = 100
    POISON_ID = 3

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

        self.config = MagicMock()
        self.config.batch_size = 2
        self.config.checkpoint_interval = 1
        self.config.skip_media_chat_ids = set()
        self.config.skip_media_delete_existing = False
        self.config.sync_deletions_edits = False
        self.config.reaction_resweep_days = 0
        self.config.should_skip_topic = MagicMock(return_value=False)
        self.config.media_path = os.path.join(self.temp_dir, "media")

        self.db = AsyncMock()
        self.db.get_last_message_id.return_value = 0

        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.db = self.db
        self.backup.client = MagicMock()
        self.backup._cleaned_media_chats = set()
        self.backup._get_marked_id = MagicMock(return_value=self.CHAT_ID)
        self.backup._extract_chat_data = MagicMock(return_value={"id": self.CHAT_ID})
        self.backup._ensure_profile_photo = AsyncMock()
        self.backup._sync_pinned_messages = AsyncMock()

        self.committed: list[int] = []

        async def commit(batch, chat_id):
            self.committed.extend(m["id"] for m in batch)

        self.backup._commit_batch = AsyncMock(side_effect=commit)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_message(self, msg_id):
        msg = MagicMock()
        msg.id = msg_id
        # MagicMock truthiness would otherwise make every message look like a
        # forum reply and be topic-filtered.
        msg.reply_to = None
        msg.action = None
        return msg

    def _feed(self, ids):
        messages = [self._make_message(i) for i in ids]

        async def fake_iter(*args, **kwargs):
            for message in messages:
                yield message

        self.backup.client.iter_messages = fake_iter

    def _poison_processor(self):
        async def process(message, chat_id):
            if message.id == self.POISON_ID:
                raise AttributeError("'DocumentEmpty' object has no attribute 'attributes'")
            return {"id": message.id, "chat_id": chat_id}

        return AsyncMock(side_effect=process)

    def _cursor_ids(self):
        return [call.args[1] for call in self.db.update_sync_status.await_args_list]

    def test_dialog_survives_one_unprocessable_message(self):
        """Every other message in the chat is still archived."""
        self._feed([1, 2, 3, 4, 5])
        self.backup._process_message = self._poison_processor()

        result = _run(self.backup._backup_dialog(MagicMock()))

        self.assertEqual(result, 4)
        self.assertEqual(self.committed, [1, 2, 4, 5])

    def test_cursor_never_moves_past_the_failed_message(self):
        """The poison message stays retryable instead of being skipped forever."""
        self._feed([1, 2, 3, 4, 5])
        self.backup._process_message = self._poison_processor()

        _run(self.backup._backup_dialog(MagicMock()))

        self.assertTrue(self._cursor_ids(), "the dialog must still checkpoint what it did archive")
        for cursor_id in self._cursor_ids():
            self.assertLess(cursor_id, self.POISON_ID)

    def test_failure_is_surfaced_as_a_count_with_no_identifiers(self):
        self._feed([1, 2, 3, 4, 5])
        self.backup._process_message = self._poison_processor()

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(self.backup._backup_dialog(MagicMock()))

        warnings = [r.getMessage() for r in cm.records if "could not be processed" in r.getMessage()]
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 message(s)", warnings[0])
        self.assertNotIn(str(self.POISON_ID), warnings[0])
        self.assertNotIn(str(self.CHAT_ID), warnings[0])

    def test_clean_dialog_still_advances_the_cursor(self):
        """Positive control: nothing freezes when nothing fails."""
        self._feed([1, 2, 3, 4])
        self.backup._process_message = AsyncMock(side_effect=lambda m, c: {"id": m.id, "chat_id": c})

        result = _run(self.backup._backup_dialog(MagicMock()))

        self.assertEqual(result, 4)
        self.assertEqual(self._cursor_ids()[-1], 4)

    def test_topic_skipped_messages_still_advance_the_cursor(self):
        """Skipping is not failing: a filtered chat must not re-scan forever."""
        self._feed([1, 2, 3, 4])
        self.config.should_skip_topic = MagicMock(return_value=True)
        self.backup._process_message = AsyncMock()

        result = _run(self.backup._backup_dialog(MagicMock()))

        self.assertEqual(result, 0)
        self.backup._process_message.assert_not_awaited()
        self.assertEqual(self._cursor_ids(), [4])

    def test_gap_fill_isolates_one_unprocessable_message(self):
        """_fill_gap_range must recover the rest of the gap, not abandon it."""
        self._feed([1, 2, 3, 4])
        self.backup._process_message = self._poison_processor()

        recovered = _run(self.backup._fill_gap_range(MagicMock(), self.CHAT_ID, 0, 5))

        self.assertEqual(recovered, 3)
        self.assertEqual(self.committed, [1, 2, 4])


class TestPeerResolutionErrorsNeverReachTheLogs(unittest.TestCase):
    """Telethon's exception text carries the chat id; only the type may be logged.

    These sites predate the #274 sweep's AST scanner, which is blind to ``{e}``.
    """

    def setUp(self):
        self.config = MagicMock()
        self.config.skip_media_chat_ids = set()
        self.config.gap_threshold = 5
        self.config.get_max_media_size_bytes = MagicMock(return_value=50 * 1024 * 1024)
        self.config.max_media_download_attempts = 3

        self.db = AsyncMock()
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.config = self.config
        self.backup.db = self.db
        self.backup.client = MagicMock()

    def _assert_no_peer_id(self, records):
        messages = [r.getMessage() for r in records if r.levelname in ("WARNING", "ERROR")]
        self.assertTrue(messages, "the failure must still be reported")
        for message in messages:
            self.assertNotIn(PEER_ID_IN_TEXT, message)
            self.assertNotIn("PeerUser", message)

    def test_gap_fill_entity_failure_logs_the_type_only(self):
        self.backup.client.get_entity = AsyncMock(side_effect=ValueError(PEER_ERROR_TEXT))

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            summary = _run(self.backup._fill_gaps(chat_id=-1001234567890))

        self.assertEqual(summary["errors"], 1)
        self._assert_no_peer_id(cm.records)

    def test_media_verification_access_failure_logs_the_type_only(self):
        self.db.get_media_for_verification.return_value = [
            {"file_path": "/nonexistent/photo.jpg", "file_size": 100, "chat_id": -1001234567890, "message_id": 10}
        ]
        self.backup.client.get_messages = AsyncMock(side_effect=ValueError(PEER_ERROR_TEXT))

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(self.backup._verify_and_redownload_media())

        self._assert_no_peer_id(cm.records)

    def test_pending_media_retry_access_failure_logs_the_type_only(self):
        self.db.get_pending_media_downloads.return_value = [
            {"id": 1, "chat_id": -1001234567890, "message_id": 10, "file_path": "/nonexistent/photo.jpg"}
        ]
        self.db.count_capped_media_downloads.return_value = 0
        self.backup.client.get_messages = AsyncMock(side_effect=ValueError(PEER_ERROR_TEXT))

        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(self.backup._retry_pending_media_downloads())

        self._assert_no_peer_id(cm.records)


class TestTerminalAuthErrorsFailFast(unittest.TestCase):
    """A revoked or duplicated session is permanent: retrying it is pure delay."""

    def _call_counting(self, exc):
        calls = []

        async def boom():
            calls.append(1)
            raise exc

        return boom, calls

    def _drive(self, exc):
        boom, calls = self._call_counting(exc)
        with patch("src.telegram_backup.asyncio.sleep", new=AsyncMock()) as slept, self.assertRaises(type(exc)):
            _run(call_with_flood_retry(boom))
        return calls, slept

    def test_session_revoked_raises_on_the_first_attempt(self):
        calls, slept = self._drive(SessionRevokedError(request=None))
        self.assertEqual(len(calls), 1)
        slept.assert_not_awaited()

    def test_auth_key_duplicated_raises_on_the_first_attempt(self):
        calls, slept = self._drive(AuthKeyDuplicatedError(request=None))
        self.assertEqual(len(calls), 1)
        slept.assert_not_awaited()

    def test_connection_errors_are_still_retried(self):
        """Positive control: the harness can tell 'retried' from 'not retried'."""
        calls, slept = self._drive(ConnectionError("transport dropped"))
        self.assertGreater(len(calls), 1)
        slept.assert_awaited()


class _FakeEventClient:
    """Telethon's add_event_handler only ever appends; so does this."""

    def __init__(self):
        self.handlers = []
        self.flood_sleep_threshold = 0

    def on(self, event):
        def decorator(callback):
            self.handlers.append((event, callback))
            return callback

        return decorator

    def remove_event_handler(self, callback):
        before = len(self.handlers)
        self.handlers = [(e, cb) for e, cb in self.handlers if cb is not callback]
        return before - len(self.handlers)

    def is_connected(self):
        return False


def _listener_config(**overrides):
    config = MagicMock()
    config.validate_credentials = MagicMock()
    config.listen_edits = True
    config.listen_deletions = False
    config.listen_new_messages = True
    config.listen_new_messages_media = True
    config.listen_reactions = False
    config.listen_chat_actions = True
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    config.max_filename_bytes = 255
    config.deduplicate_media = True
    config.get_max_media_size_bytes = MagicMock(return_value=50 * 1024 * 1024)
    config.should_download_media_for_chat = MagicMock(return_value=True)
    config.media_flood_sleep_threshold = 60
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestListenerDetachesItsHandlers(unittest.TestCase):
    """A stopped listener must stop receiving events.

    The scheduler builds a NEW listener on the SAME shared client after every
    network blip, so a listener that never detaches leaves one more live
    instance behind on each restart: duplicate writes, duplicate viewer
    broadcasts, duplicate media downloads.
    """

    def _make_listener(self, client):
        return TelegramListener(_listener_config(), AsyncMock(), client=client)

    def test_stop_detaches_every_handler_it_registered(self):
        client = _FakeEventClient()
        listener = self._make_listener(client)
        listener._register_handlers()
        registered = len(client.handlers)
        self.assertGreater(registered, 0)

        _run(listener.stop())

        self.assertEqual(client.handlers, [])
        self.assertEqual(listener._registered_handlers, [])

    def test_restart_does_not_double_the_handlers_on_a_shared_client(self):
        client = _FakeEventClient()
        first = self._make_listener(client)
        first._register_handlers()
        registered = len(client.handlers)

        _run(first.stop())
        second = self._make_listener(client)
        second._register_handlers()

        self.assertEqual(len(client.handlers), registered)


class TestListenerMediaDownloadDiscipline(unittest.TestCase):
    """Two guarantees on the live download path, both already true in the sweep."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.chat_id = -1001234567890

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_listener(self, **overrides):
        config = _listener_config(media_path=self.temp_dir, **overrides)
        listener = TelegramListener(config, AsyncMock())
        listener.db.find_media_by_content_hash = AsyncMock(return_value=None)
        listener.client = MagicMock()
        listener.client.flood_sleep_threshold = 0
        return listener

    def _photo_message(self):
        message = MagicMock()
        message.id = 4242
        message.reply_to = None
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 123
        media.photo.sizes = []
        message.media = media
        return message

    def _part_files(self):
        found = []
        for root, _dirs, files in os.walk(self.temp_dir):
            found.extend(os.path.join(root, f) for f in files if f.endswith(".part"))
        return found

    def _failing_download(self, listener):
        """Write a partial file, then fail the way a real transfer fails."""

        async def fake_download(message, path):
            with open(path, "wb") as handle:
                handle.write(b"partial bytes")
            # Above MAX_FLOOD_WAIT_SECONDS, so call_with_flood_retry gives up at once.
            raise FloodWaitError(request=None, capture=7200)

        listener.client.download_media = AsyncMock(side_effect=fake_download)

    def test_failed_dedup_download_leaves_no_part_file(self):
        listener = self._make_listener(deduplicate_media=True)
        self._failing_download(listener)

        result = _run(listener._download_media(self._photo_message(), self.chat_id))

        self.assertIsNone(result)
        self.assertEqual(self._part_files(), [])

    def test_failed_direct_download_leaves_no_part_file(self):
        listener = self._make_listener(deduplicate_media=False)
        self._failing_download(listener)

        result = _run(listener._download_media(self._photo_message(), self.chat_id))

        self.assertIsNone(result)
        self.assertEqual(self._part_files(), [])

    def _drive_flood_retries(self, listener):
        """Two floods then a success, sampling the client-wide threshold throughout."""
        samples = {"attempt": [], "sleep": []}
        floods = {"left": 2}

        async def fake_download(message, path):
            samples["attempt"].append(listener.client.flood_sleep_threshold)
            if floods["left"]:
                floods["left"] -= 1
                raise FloodWaitError(request=None, capture=5)
            with open(path, "wb") as handle:
                handle.write(b"done")
            return path

        listener.client.download_media = AsyncMock(side_effect=fake_download)

        async def fake_sleep(_seconds):
            samples["sleep"].append(listener.client.flood_sleep_threshold)

        with patch("src.telegram_backup.asyncio.sleep", new=fake_sleep):
            result = _run(listener._download_media(self._photo_message(), self.chat_id))

        self.assertIsNotNone(result)
        return samples

    def test_flood_threshold_is_released_between_retry_sleeps(self):
        """absorb_media_floods is client-wide: it may cover one attempt, not the ladder.

        Holding it across call_with_flood_retry's sleeps leaves every other
        coroutine on the shared client silently sleeping inside Telethon for as
        long as the retry ladder lasts.
        """
        listener = self._make_listener(deduplicate_media=False)

        samples = self._drive_flood_retries(listener)

        self.assertEqual(samples["attempt"], [60, 60, 60])
        self.assertEqual(samples["sleep"], [0, 0])
        self.assertEqual(listener.client.flood_sleep_threshold, 0)

    def test_flood_threshold_is_released_between_retry_sleeps_when_deduplicating(self):
        """Same ordering on the dedup path -- the default, and the other copy."""
        listener = self._make_listener(deduplicate_media=True)

        samples = self._drive_flood_retries(listener)

        self.assertEqual(samples["attempt"], [60, 60, 60])
        self.assertEqual(samples["sleep"], [0, 0])
        self.assertEqual(listener.client.flood_sleep_threshold, 0)


class TestThumbnailPreGenerationHardening(unittest.TestCase):
    """The archiver-side thumbnail pass must survive hostile media it just fetched.

    ``_pre_generate_thumbnail`` runs inside the backup process right after a
    media download, so a poisoned attachment reaches it before the viewer ever
    sees the file. Its only size guard used to be the 50 MB byte gate, but
    decode memory is set by pixel count, not compressed size: a 12000x8000
    flat PNG is under 1 MB on disk and cost ~370 MB RSS to decode. The final
    save also streamed straight into the cache path, so a concurrent viewer --
    for which ``dest.exists()`` means "complete" -- could read and cache a torn
    file. These tests are the attack: the pixel bomb must be refused from the
    header alone, a large JPEG (draft-decoded at up to 1/8 scale) must keep its
    thumbnail, and the destination must never exist half-written.
    """

    @staticmethod
    def _write_flat_png(path: Path, width: int, height: int) -> None:
        """A fully valid flat-black RGB PNG, streamed through zlib so the test
        itself never allocates width*height*3 bytes. Under 4 MB on disk even at
        96 MP -- comfortably inside the byte gate, which is exactly the attack.
        """

        def chunk(tag: bytes, payload: bytes) -> bytes:
            crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
            return len(payload).to_bytes(4, "big") + tag + payload + crc.to_bytes(4, "big")

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        compressor = zlib.compressobj(1)
        row = b"\x00" * (1 + width * 3)  # filter byte + RGB pixels
        idat = bytearray()
        for _ in range(height):
            idat += compressor.compress(row)
        idat += compressor.flush()
        path.parent.mkdir(parents=True, exist_ok=True)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", bytes(idat)) + chunk(b"IEND", b"")
        path.write_bytes(png)

    def test_png_pixel_bomb_is_refused_without_decoding(self):
        """The reviewer's 96 MP flat PNG: refused from the header, zero pixels decoded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "bomb.png"
            self._write_flat_png(source, 12000, 8000)
            self.assertGreater(12000 * 8000, _MAX_SOURCE_PIXELS)
            self.assertLess(source.stat().st_size, 4 * 1024 * 1024)  # the byte gate cannot see it

            decoded = []
            real_load = ImageFile.ImageFile.load

            def spying_load(img_self):
                decoded.append(img_self.size)
                return real_load(img_self)

            # 96 MP is past Image.MAX_IMAGE_PIXELS (50 MP), so open() itself
            # warns before our gate runs; silence it so the refusal behaves
            # identically under any warnings configuration.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
                with patch.object(ImageFile.ImageFile, "load", spying_load):
                    _pre_generate_thumbnail(str(source), str(media_root))

            self.assertFalse((media_root / ".thumbs" / "200" / "chat1" / "bomb.webp").exists())
            self.assertEqual(decoded, [])

    def test_pixel_bomb_under_pillows_own_limit_is_still_refused(self):
        """The gate must hold at the viewer's threshold, not at Pillow's.

        32 MP sits under ``Image.MAX_IMAGE_PIXELS`` (50 MP), so Pillow neither
        warns nor raises -- only the header gate stands between this file and a
        ~100 MB decode per poisoned message inside the backup process.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "bomb.png"
            self._write_flat_png(source, 8000, 4000)
            self.assertGreater(8000 * 4000, _MAX_SOURCE_PIXELS)

            _pre_generate_thumbnail(str(source), str(media_root))

            self.assertFalse((media_root / ".thumbs" / "200" / "chat1" / "bomb.webp").exists())

    def test_normal_photo_still_gets_a_complete_thumbnail(self):
        """An ordinary photo passes the gate and comes out whole via os.replace()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "photo.png"
            source.parent.mkdir(parents=True)
            PILImage.new("RGB", (640, 480), "blue").save(source)
            self.assertLess(640 * 480, _MAX_SOURCE_PIXELS)

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "photo.webp"
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as thumb:
                self.assertEqual(thumb.format, "WEBP")
                self.assertLessEqual(thumb.width, 200)
                self.assertLessEqual(thumb.height, 200)
            self.assertEqual(list(dest.parent.glob(".thumb-*.tmp")), [])

    def test_oversized_jpeg_keeps_its_thumbnail(self):
        """The gate is format-aware: a 26 MP camera JPEG is over the pixel
        threshold but safe, because ``img.thumbnail()`` drafts JPEG decoding
        down to 1/8 scale. Refusing it would silently strip thumbnails from
        every full-resolution camera upload.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "camera.jpg"
            source.parent.mkdir(parents=True)
            self.assertGreater(6000 * 4400, _MAX_SOURCE_PIXELS)
            PILImage.new("RGB", (6000, 4400), "green").save(source, "JPEG", quality=50)

            _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "camera.webp"
            self.assertTrue(dest.exists())
            with PILImage.open(dest) as thumb:
                self.assertEqual(thumb.format, "WEBP")

    def test_thumbnail_is_written_via_temp_file_and_atomic_replace(self):
        """Pillow must never stream into the final cache path directly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "photo.png"
            source.parent.mkdir(parents=True)
            PILImage.new("RGB", (300, 200), "red").save(source)

            saved_to = []
            real_save = PILImage.Image.save

            def recording_save(img_self, fp, *args, **kwargs):
                saved_to.append(Path(fp))
                return real_save(img_self, fp, *args, **kwargs)

            with patch.object(PILImage.Image, "save", recording_save):
                _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "photo.webp"
            self.assertEqual(len(saved_to), 1)
            self.assertNotEqual(saved_to[0], dest)
            self.assertEqual(saved_to[0].parent, dest.parent)  # same dir, so os.replace() is atomic
            self.assertTrue(saved_to[0].name.startswith(".thumb-"))
            self.assertTrue(dest.exists())
            self.assertEqual(list(dest.parent.glob(".thumb-*.tmp")), [])

    def test_interrupted_write_never_leaves_a_visible_thumbnail(self):
        """A save that dies mid-write must not leave dest half-written.

        The viewer's only completeness check is ``dest.exists()``, and it
        serves thumbnails with a long-lived cache header -- a torn file would
        be cached by every client that raced the write.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            media_root = Path(tmpdir)
            source = media_root / "chat1" / "photo.png"
            source.parent.mkdir(parents=True)
            PILImage.new("RGB", (300, 200), "red").save(source)

            def torn_save(img_self, fp, *args, **kwargs):
                Path(fp).write_bytes(b"RIFF\x00\x00")  # half a WEBP header...
                raise OSError(28, "No space left on device")  # ...then the disk fills

            with patch.object(PILImage.Image, "save", torn_save):
                _pre_generate_thumbnail(str(source), str(media_root))

            dest = media_root / ".thumbs" / "200" / "chat1" / "photo.webp"
            self.assertFalse(dest.exists())
            self.assertEqual(list(dest.parent.glob("*")), [])  # and no torn temp left behind


if __name__ == "__main__":
    unittest.main()
