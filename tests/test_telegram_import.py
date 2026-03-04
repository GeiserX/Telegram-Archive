import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.telegram_import import (
    TelegramImporter,
    _build_service_text,
    _detect_media,
    _find_html_files,
    _parse_html_duration,
    _parse_html_export,
    derive_chat_id,
    flatten_text,
    parse_date,
    parse_edited_date,
    parse_from_id,
    parse_html_date,
)


class TestParseFromId(unittest.TestCase):
    def test_user_id(self):
        self.assertEqual(parse_from_id("user123456789"), 123456789)

    def test_channel_id(self):
        self.assertEqual(parse_from_id("channel1234567890"), -1001234567890)

    def test_group_id(self):
        self.assertEqual(parse_from_id("group123456789"), -123456789)

    def test_none(self):
        self.assertIsNone(parse_from_id(None))

    def test_empty_string(self):
        self.assertIsNone(parse_from_id(""))

    def test_unknown_prefix(self):
        self.assertIsNone(parse_from_id("bot123"))

    def test_invalid_number(self):
        self.assertIsNone(parse_from_id("userabc"))


class TestDeriveChatId(unittest.TestCase):
    def test_personal_chat(self):
        self.assertEqual(derive_chat_id(123456, "personal_chat"), 123456)

    def test_bot_chat(self):
        self.assertEqual(derive_chat_id(99999, "bot_chat"), 99999)

    def test_saved_messages(self):
        self.assertEqual(derive_chat_id(42, "saved_messages"), 42)

    def test_private_group(self):
        self.assertEqual(derive_chat_id(123456, "private_group"), -123456)

    def test_private_supergroup(self):
        self.assertEqual(derive_chat_id(1234567890, "private_supergroup"), -1001234567890)

    def test_public_supergroup(self):
        self.assertEqual(derive_chat_id(1234567890, "public_supergroup"), -1001234567890)

    def test_private_channel(self):
        self.assertEqual(derive_chat_id(1234567890, "private_channel"), -1001234567890)

    def test_public_channel(self):
        self.assertEqual(derive_chat_id(1234567890, "public_channel"), -1001234567890)

    def test_unknown_type(self):
        self.assertEqual(derive_chat_id(42, "unknown_type"), 42)


class TestFlattenText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(flatten_text("Hello world"), "Hello world")

    def test_empty_string(self):
        self.assertEqual(flatten_text(""), "")

    def test_none(self):
        self.assertEqual(flatten_text(None), "")

    def test_entity_list(self):
        entities = [
            {"type": "plain", "text": "Hello "},
            {"type": "bold", "text": "world"},
            {"type": "plain", "text": "!"},
        ]
        self.assertEqual(flatten_text(entities), "Hello world!")

    def test_mixed_list(self):
        entities = ["plain text", {"type": "link", "text": "http://example.com"}]
        self.assertEqual(flatten_text(entities), "plain texthttp://example.com")

    def test_empty_list(self):
        self.assertEqual(flatten_text([]), "")


class TestParseDate(unittest.TestCase):
    def test_unixtime(self):
        msg = {"date_unixtime": "1673779800"}
        result = parse_date(msg)
        self.assertIsInstance(result, datetime)
        self.assertEqual(result.year, 2023)

    def test_iso_format(self):
        msg = {"date": "2023-01-15T10:30:00"}
        result = parse_date(msg)
        self.assertIsInstance(result, datetime)
        self.assertEqual(result.year, 2023)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_prefers_unixtime(self):
        msg = {"date_unixtime": "1673779800", "date": "2025-06-01T00:00:00"}
        result = parse_date(msg)
        self.assertEqual(result.year, 2023)

    def test_no_date(self):
        self.assertIsNone(parse_date({}))

    def test_invalid_date(self):
        self.assertIsNone(parse_date({"date": "not-a-date"}))


class TestParseEditedDate(unittest.TestCase):
    def test_edited_unixtime(self):
        msg = {"edited_unixtime": "1673780100"}
        result = parse_edited_date(msg)
        self.assertIsInstance(result, datetime)

    def test_edited_iso(self):
        msg = {"edited": "2023-01-15T10:35:00"}
        result = parse_edited_date(msg)
        self.assertIsInstance(result, datetime)

    def test_no_edited(self):
        self.assertIsNone(parse_edited_date({}))


class TestDetectMedia(unittest.TestCase):
    def test_photo(self):
        msg = {"photo": "photos/photo_1.jpg"}
        media_type, rel, fname = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "photo")
        self.assertEqual(rel, "photos/photo_1.jpg")
        self.assertEqual(fname, "photo_1.jpg")

    def test_document(self):
        msg = {"file": "files/doc.pdf", "file_name": "document.pdf", "mime_type": "application/pdf"}
        media_type, rel, fname = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "document")
        self.assertEqual(fname, "document.pdf")

    def test_video(self):
        msg = {"file": "videos/vid.mp4", "media_type": "video_file"}
        media_type, rel, fname = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "video")

    def test_voice(self):
        msg = {"file": "voice/msg.ogg", "media_type": "voice_message"}
        media_type, rel, fname = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "voice")

    def test_animation(self):
        msg = {"file": "animations/anim.mp4", "media_type": "animation"}
        media_type, rel, fname = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "animation")

    def test_no_media(self):
        media_type, rel, fname = _detect_media({}, Path("/tmp"))
        self.assertIsNone(media_type)
        self.assertIsNone(rel)

    def test_photo_takes_precedence(self):
        msg = {"photo": "photos/p.jpg", "file": "files/f.pdf"}
        media_type, _, _ = _detect_media(msg, Path("/tmp"))
        self.assertEqual(media_type, "photo")


class TestBuildServiceText(unittest.TestCase):
    def test_pin_message(self):
        msg = {"action": "pin_message", "from": "Alice"}
        self.assertIn("pinned a message", _build_service_text(msg))
        self.assertIn("Alice", _build_service_text(msg))

    def test_create_group(self):
        msg = {"action": "create_group", "actor": "Bob", "title": "My Group"}
        result = _build_service_text(msg)
        self.assertIn("Bob", result)
        self.assertIn("created the group", result)
        self.assertIn("My Group", result)

    def test_unknown_action(self):
        msg = {"action": "some_new_action", "from": "Charlie"}
        result = _build_service_text(msg)
        self.assertIn("some new action", result)


class TestTelegramImporterExtractChats(unittest.TestCase):
    def _make_importer(self):
        db = MagicMock()
        return TelegramImporter(db, "/tmp/media")

    def test_single_chat_export(self):
        data = {"name": "Test Chat", "type": "personal_chat", "id": 123, "messages": []}
        importer = self._make_importer()
        chats = importer._extract_chats(data)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["name"], "Test Chat")

    def test_full_account_export(self):
        data = {
            "chats": {
                "list": [
                    {"name": "Chat 1", "type": "personal_chat", "id": 1, "messages": []},
                    {"name": "Chat 2", "type": "private_group", "id": 2, "messages": []},
                ]
            }
        }
        importer = self._make_importer()
        chats = importer._extract_chats(data)
        self.assertEqual(len(chats), 2)

    def test_empty_data(self):
        importer = self._make_importer()
        self.assertEqual(importer._extract_chats({}), [])


class TestTelegramImporterRun(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_export(self, data):
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(data, f)

    def test_dry_run_no_db_writes(self):
        self._write_export(
            {
                "name": "Test",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "Hello",
                    },
                    {
                        "id": 2,
                        "type": "message",
                        "date": "2024-01-15T10:01:00",
                        "from": "Bob",
                        "from_id": "user99",
                        "text": "World",
                    },
                ],
            }
        )

        db = AsyncMock()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, dry_run=True))

        self.assertEqual(summary["total_messages"], 2)
        self.assertEqual(summary["chats_imported"], 1)
        db.upsert_chat.assert_not_called()
        db.insert_messages_batch.assert_not_called()

    def test_import_with_merge_check(self):
        self._write_export(
            {
                "name": "Existing Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Hi"},
                ],
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 100}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        with self.assertRaises(ValueError) as ctx:
            self._run(importer.run(self.export_dir, merge=False))
        self.assertIn("already has", str(ctx.exception))

    def test_import_messages(self):
        self._write_export(
            {
                "name": "Test Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "Hello",
                    },
                    {
                        "id": 2,
                        "type": "service",
                        "date": "2024-01-15T10:05:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "action": "pin_message",
                    },
                ],
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 2)
        db.upsert_chat.assert_called_once()
        db.insert_messages_batch.assert_called_once()
        db.update_sync_status.assert_called_once_with(42, 2, 2)

    def test_import_with_media(self):
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        photo_path = os.path.join(photos_dir, "photo_1.jpg")
        with open(photo_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        self._write_export(
            {
                "name": "Media Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "from": "Alice",
                        "from_id": "user42",
                        "text": "",
                        "photo": "photos/photo_1.jpg",
                        "width": 800,
                        "height": 600,
                    },
                ],
            }
        )

        media_dir = os.path.join(self.temp_dir, "media")
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, media_dir)

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()
        media_call = db.insert_media.call_args[0][0]
        self.assertEqual(media_call["type"], "photo")
        self.assertEqual(media_call["message_id"], 1)
        self.assertTrue(Path(media_dir, "42").exists())

    def test_skip_media_flag(self):
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1.jpg"), "wb") as f:
            f.write(b"\x00" * 50)

        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "",
                        "photo": "photos/photo_1.jpg",
                    },
                ],
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, skip_media=True))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_missing_result_json(self):
        db = AsyncMock()
        importer = TelegramImporter(db, "/tmp/media")

        with self.assertRaises(FileNotFoundError):
            self._run(importer.run(self.export_dir))

    def test_forwarded_message(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2024-01-15T10:00:00",
                        "text": "Forwarded content",
                        "forwarded_from": "Some Channel",
                    },
                ],
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["raw_data"]["forward_from_name"], "Some Channel")

    def test_chat_id_override(self):
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Hi"},
                ],
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=-1009999))

        self.assertEqual(summary["details"][0]["chat_id"], -1009999)
        chat_call = db.upsert_chat.call_args[0][0]
        self.assertEqual(chat_call["id"], -1009999)


# ---------------------------------------------------------------------------
# HTML import tests
# ---------------------------------------------------------------------------

SAMPLE_HTML_MESSAGE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Test Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message100">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00 UTC+02:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Hello world!</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_JOINED = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Group Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message200">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">First message</div>
   </div>
  </div>
  <div class="message default clearfix joined" id="message201">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:01:00">10:01</div>
    <div class="text">Second message (same sender)</div>
   </div>
  </div>
  <div class="message default clearfix" id="message202">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:02:00">10:02</div>
    <div class="from_name">Bob</div>
    <div class="text">Different sender</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_SERVICE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Group</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message service" id="message300">
   <div class="body details">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    Alice joined group via invite link
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_REPLY = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message400">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Original message</div>
   </div>
  </div>
  <div class="message default clearfix" id="message401">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:01:00">10:01</div>
    <div class="from_name">Bob</div>
    <div class="reply_to details">
     In reply to <a href="#go_to_message400">this message</a>
    </div>
    <div class="text">This is a reply</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_FORWARDED = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message500">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="forwarded body">
     <div class="from_name">Original Channel</div>
     <div class="text">Forwarded content</div>
    </div>
    <div class="text">Alice's comment</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_PHOTO = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Media Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message600">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <a class="photo_wrap clearfix pull_left" href="photos/photo_1@15-01-2024_10-00-00.jpg">
     <img class="photo" src="photos/photo_1@15-01-2024_10-00-00.jpg" style="width: 320px; height: 240px">
    </a>
    <div class="text">Check this photo!</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_VIDEO = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message700">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_video">
      <a href="video_files/video@15-01-2024_10-00-00.mp4">Video</a>
      <div class="description">01:30</div>
     </div>
    </div>
    <div class="text"></div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_VOICE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message800">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_voice_message">
      <a href="voice_messages/audio_1@15-01-2024_10-00-00.ogg">Voice message</a>
      <div class="description">00:15</div>
     </div>
    </div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""

SAMPLE_HTML_FILE = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message900">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="media_wrap clearfix">
     <div class="media clearfix pull_left media_file">
      <a href="files/document.pdf">document.pdf (1.2 MB)</a>
     </div>
    </div>
   </div>
  </div>
 </div></div>
</div>
</body></html>
"""


class TestParseHtmlDate(unittest.TestCase):
    def test_basic_date(self):
        self.assertEqual(parse_html_date("15.01.2024 10:30:00"), "2024-01-15T10:30:00")

    def test_date_with_timezone(self):
        self.assertEqual(parse_html_date("15.01.2024 10:30:00 UTC+02:00"), "2024-01-15T10:30:00")

    def test_empty_string(self):
        self.assertIsNone(parse_html_date(""))

    def test_none(self):
        self.assertIsNone(parse_html_date(None))

    def test_invalid_format(self):
        self.assertIsNone(parse_html_date("not a date"))

    def test_partial_date(self):
        self.assertIsNone(parse_html_date("15.01.2024"))


class TestFindHtmlFiles(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_file(self):
        Path(self.temp_dir, "messages.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "messages.html")

    def test_multiple_files(self):
        Path(self.temp_dir, "messages.html").touch()
        Path(self.temp_dir, "messages2.html").touch()
        Path(self.temp_dir, "messages3.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 3)
        self.assertEqual([f.name for f in files], ["messages.html", "messages2.html", "messages3.html"])

    def test_no_html_files(self):
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(files, [])

    def test_only_numbered_files(self):
        # messages2.html without messages.html - starts from messages2
        Path(self.temp_dir, "messages2.html").touch()
        files = _find_html_files(Path(self.temp_dir))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "messages2.html")


class TestParseHtmlDuration(unittest.TestCase):
    def test_minutes_seconds(self):
        self.assertEqual(_parse_html_duration("01:30"), 90)

    def test_hours_minutes_seconds(self):
        self.assertEqual(_parse_html_duration("1:30:00"), 5400)

    def test_zero_duration(self):
        self.assertEqual(_parse_html_duration("00:00"), 0)

    def test_invalid(self):
        self.assertIsNone(_parse_html_duration("not a duration"))

    def test_empty(self):
        self.assertIsNone(_parse_html_duration(""))


class TestParseHtmlExport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_html(self, content, filename="messages.html"):
        filepath = os.path.join(self.temp_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    def test_basic_message(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Test Chat")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 100)
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[0]["text"], "Hello world!")
        self.assertEqual(messages[0]["date"], "2024-01-15T10:00:00")
        self.assertEqual(messages[0]["type"], "message")

    def test_joined_messages(self):
        self._write_html(SAMPLE_HTML_JOINED)
        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Group Chat")
        self.assertEqual(len(messages), 3)
        # Joined message inherits sender from previous
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[1]["from"], "Alice")
        self.assertEqual(messages[1]["text"], "Second message (same sender)")
        self.assertEqual(messages[2]["from"], "Bob")

    def test_service_message(self):
        self._write_html(SAMPLE_HTML_SERVICE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "service")
        self.assertEqual(messages[0]["id"], 300)
        self.assertIn("Alice joined group", messages[0]["text"])

    def test_reply_reference(self):
        self._write_html(SAMPLE_HTML_REPLY)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 2)
        self.assertIsNone(messages[0].get("reply_to_message_id"))
        self.assertEqual(messages[1]["reply_to_message_id"], 400)
        self.assertEqual(messages[1]["text"], "This is a reply")

    def test_forwarded_message(self):
        self._write_html(SAMPLE_HTML_FORWARDED)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        # Sender should be the forwarder (Alice), not the original (from .forwarded body)
        self.assertEqual(messages[0]["from"], "Alice")
        self.assertEqual(messages[0]["forwarded_from"], "Original Channel")
        self.assertEqual(messages[0]["text"], "Alice's comment")

    def test_photo_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["photo"], "photos/photo_1@15-01-2024_10-00-00.jpg")
        self.assertEqual(messages[0]["width"], 320)
        self.assertEqual(messages[0]["height"], 240)
        self.assertEqual(messages[0]["text"], "Check this photo!")

    def test_video_media(self):
        self._write_html(SAMPLE_HTML_VIDEO)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "video_files/video@15-01-2024_10-00-00.mp4")
        self.assertEqual(messages[0]["media_type"], "video_file")
        self.assertEqual(messages[0]["duration_seconds"], 90)

    def test_voice_media(self):
        self._write_html(SAMPLE_HTML_VOICE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "voice_messages/audio_1@15-01-2024_10-00-00.ogg")
        self.assertEqual(messages[0]["media_type"], "voice_message")
        self.assertEqual(messages[0]["duration_seconds"], 15)

    def test_file_media(self):
        self._write_html(SAMPLE_HTML_FILE)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["file"], "files/document.pdf")
        self.assertEqual(messages[0]["file_name"], "document.pdf")

    def test_multi_file_html(self):
        """Test that multiple HTML files are combined in order."""
        html1 = """\
<html><body>
<div class="page_wrap">
 <div class="page_header"><div class="content"><div class="text bold">Multi Chat</div></div></div>
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message1">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Message in file 1</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        html2 = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message2">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 11:00:00">11:00</div>
    <div class="from_name">Bob</div>
    <div class="text">Message in file 2</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html1, "messages.html")
        self._write_html(html2, "messages2.html")

        html_files = _find_html_files(Path(self.temp_dir))
        chat_name, messages = _parse_html_export(html_files, Path(self.temp_dir))

        self.assertEqual(chat_name, "Multi Chat")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["text"], "Message in file 1")
        self.assertEqual(messages[1]["text"], "Message in file 2")

    def test_message_without_id_skipped(self):
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix">
   <div class="body">
    <div class="from_name">Alice</div>
    <div class="text">No ID message</div>
   </div>
  </div>
  <div class="message default clearfix" id="message1">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Has ID</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], "Has ID")


class TestHtmlImportIntegration(unittest.TestCase):
    """Integration tests for HTML import through TelegramImporter."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _write_html(self, content, filename="messages.html"):
        filepath = os.path.join(self.export_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def test_html_import_requires_chat_id(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = AsyncMock()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        with self.assertRaises(ValueError) as ctx:
            self._run(importer.run(self.export_dir))
        self.assertIn("chat ID", str(ctx.exception))

    def test_html_import_basic(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=-1001234567890))

        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["chats_imported"], 1)
        self.assertEqual(summary["details"][0]["chat_name"], "Test Chat")
        self.assertEqual(summary["details"][0]["chat_id"], -1001234567890)
        db.upsert_chat.assert_called_once()
        db.insert_messages_batch.assert_called_once()

    def test_html_import_dry_run(self):
        self._write_html(SAMPLE_HTML_MESSAGE)
        db = AsyncMock()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=42, dry_run=True))

        self.assertEqual(summary["total_messages"], 1)
        db.upsert_chat.assert_not_called()
        db.insert_messages_batch.assert_not_called()

    def test_html_import_with_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)

        # Create the actual photo file
        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1@15-01-2024_10-00-00.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        media_dir = os.path.join(self.temp_dir, "media")
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, media_dir)

        summary = self._run(importer.run(self.export_dir, chat_id_override=42))

        self.assertEqual(summary["total_media"], 1)
        db.insert_media.assert_called_once()
        media_call = db.insert_media.call_args[0][0]
        self.assertEqual(media_call["type"], "photo")
        self.assertTrue(Path(media_dir, "42").exists())

    def test_html_import_skip_media(self):
        self._write_html(SAMPLE_HTML_PHOTO)

        photos_dir = os.path.join(self.export_dir, "photos")
        os.makedirs(photos_dir)
        with open(os.path.join(photos_dir, "photo_1@15-01-2024_10-00-00.jpg"), "wb") as f:
            f.write(b"\x00" * 50)

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=42, skip_media=True))

        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_html_import_forwarded(self):
        self._write_html(SAMPLE_HTML_FORWARDED)
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=42))

        call_args = db.insert_messages_batch.call_args[0][0]
        self.assertEqual(call_args[0]["raw_data"]["forward_from_name"], "Original Channel")

    def test_html_import_reply(self):
        self._write_html(SAMPLE_HTML_REPLY)
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        self._run(importer.run(self.export_dir, chat_id_override=42))

        call_args = db.insert_messages_batch.call_args[0][0]
        # First message has no reply
        self.assertIsNone(call_args[0]["reply_to_msg_id"])
        # Second message replies to first
        self.assertEqual(call_args[1]["reply_to_msg_id"], 400)

    def test_json_takes_priority_over_html(self):
        """When both result.json and messages.html exist, JSON is used."""
        self._write_html(SAMPLE_HTML_MESSAGE)
        # Also write a result.json
        with open(os.path.join(self.export_dir, "result.json"), "w") as f:
            json.dump(
                {
                    "name": "JSON Chat",
                    "type": "personal_chat",
                    "id": 42,
                    "messages": [
                        {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "From JSON"},
                    ],
                },
                f,
            )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        # Should use JSON (chat_id derived from JSON data, not requiring override)
        self.assertEqual(summary["details"][0]["chat_name"], "JSON Chat")

    def test_no_export_files_raises_error(self):
        """Neither result.json nor messages.html should raise FileNotFoundError."""
        db = AsyncMock()
        importer = TelegramImporter(db, "/tmp/media")

        with self.assertRaises(FileNotFoundError) as ctx:
            self._run(importer.run(self.export_dir))
        self.assertIn("No result.json or messages.html", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
