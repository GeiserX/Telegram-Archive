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
    derive_chat_id,
    flatten_text,
    parse_date,
    parse_edited_date,
    parse_from_id,
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


if __name__ == "__main__":
    unittest.main()
