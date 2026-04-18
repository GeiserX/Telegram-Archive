"""Extended tests for src/telegram_import.py to cover missing lines.

Missing lines targeted:
118, 126-127, 141-142, 146-147, 200-201, 295-307, 323, 327, 330-339,
397-398, 410, 431, 440, 445, 457, 513-515, 518, 558, 570-571, 588-591,
647, 669-670, 724, 727-731
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.telegram_import import (
    BATCH_SIZE,
    TelegramImporter,
    _build_service_text,
    _extract_html_media_info,
    _find_html_files,
    _parse_html_export,
    flatten_text,
    parse_date,
    parse_edited_date,
)


class TestFlattenTextNonStandardType(unittest.TestCase):
    """Cover line 118: flatten_text with a non-str/list/None type."""

    def test_returns_str_for_integer_input(self):
        """flatten_text converts integer to string as fallback."""
        self.assertEqual(flatten_text(42), "42")

    def test_returns_str_for_float_input(self):
        """flatten_text converts float to string as fallback."""
        self.assertEqual(flatten_text(3.14), "3.14")

    def test_returns_str_for_bool_input(self):
        """flatten_text converts boolean to string as fallback."""
        self.assertEqual(flatten_text(True), "True")


class TestParseDateExceptionBranches(unittest.TestCase):
    """Cover lines 126-127: exception handling in parse_date for invalid unixtime."""

    def test_returns_none_for_non_numeric_unixtime(self):
        """parse_date returns None when date_unixtime is not a valid number."""
        result = parse_date({"date_unixtime": "not_a_number"})
        self.assertIsNone(result)

    def test_returns_none_for_negative_overflow_unixtime(self):
        """parse_date returns None for an out-of-range unix timestamp."""
        result = parse_date({"date_unixtime": "99999999999999999"})
        self.assertIsNone(result)

    def test_falls_back_to_iso_when_unixtime_invalid(self):
        """parse_date uses ISO date field when unixtime parsing fails."""
        msg = {"date_unixtime": "bad", "date": "2024-06-15T12:00:00"}
        result = parse_date(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)

    def test_returns_none_for_invalid_iso_date(self):
        """parse_date returns None when ISO date is malformed."""
        result = parse_date({"date": "not-a-date"})
        self.assertIsNone(result)

    def test_returns_none_when_both_formats_invalid(self):
        """parse_date returns None when both unixtime and ISO date are invalid."""
        result = parse_date({"date_unixtime": "abc", "date": "xyz"})
        self.assertIsNone(result)


class TestParseEditedDateExceptionBranches(unittest.TestCase):
    """Cover lines 141-142, 146-147: exception handling in parse_edited_date."""

    def test_returns_none_for_non_numeric_edited_unixtime(self):
        """parse_edited_date returns None when edited_unixtime is not a valid number."""
        result = parse_edited_date({"edited_unixtime": "garbage"})
        self.assertIsNone(result)

    def test_returns_none_for_overflow_edited_unixtime(self):
        """parse_edited_date returns None for an out-of-range edited unix timestamp."""
        result = parse_edited_date({"edited_unixtime": "99999999999999999"})
        self.assertIsNone(result)

    def test_falls_back_to_iso_when_edited_unixtime_invalid(self):
        """parse_edited_date uses ISO field when edited_unixtime parsing fails."""
        msg = {"edited_unixtime": "bad", "edited": "2024-06-15T14:00:00"}
        result = parse_edited_date(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2024)

    def test_returns_none_for_invalid_edited_iso(self):
        """parse_edited_date returns None when edited ISO string is malformed."""
        result = parse_edited_date({"edited": "not-a-date"})
        self.assertIsNone(result)

    def test_returns_none_when_both_edited_formats_invalid(self):
        """parse_edited_date returns None when both edited fields are invalid."""
        result = parse_edited_date({"edited_unixtime": "abc", "edited": "xyz"})
        self.assertIsNone(result)


class TestBuildServiceTextWithMembers(unittest.TestCase):
    """Cover lines 200-201: _build_service_text with members field."""

    def test_includes_string_member_names(self):
        """Service text includes member names when members field has strings."""
        msg = {
            "action": "invite_members",
            "actor": "Alice",
            "members": ["Bob", "Charlie"],
        }
        result = _build_service_text(msg)
        self.assertIn("Alice", result)
        self.assertIn("invited members", result)
        self.assertIn("Bob", result)
        self.assertIn("Charlie", result)

    def test_converts_non_string_members_to_str(self):
        """Service text converts non-string member entries to str."""
        msg = {
            "action": "remove_members",
            "from": "Admin",
            "members": [123, {"name": "unknown"}],
        }
        result = _build_service_text(msg)
        self.assertIn("123", result)
        self.assertIn("unknown", result)


# ---------------------------------------------------------------------------
# HTML media extraction edge cases (lines 295-307, 323, 327, 330-339)
# ---------------------------------------------------------------------------


class TestExtractHtmlMediaInfoEdgeCases(unittest.TestCase):
    """Cover lines 295-307, 323, 327, 330-339 in _extract_html_media_info."""

    def _make_soup(self, html):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser")

    def test_bare_link_in_media_wrap_photo_folder(self):
        """Bare link in media_wrap without .media element returns photo for photos/ folder."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <a href="photos/img.jpg">Photo</a>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["photo"], "photos/img.jpg")

    def test_bare_link_in_media_wrap_images_folder(self):
        """Bare link in media_wrap for images/ folder returns photo."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <a href="images/pic.png">Image</a>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["photo"], "images/pic.png")

    def test_bare_link_in_media_wrap_files_folder(self):
        """Bare link in media_wrap for files/ folder returns file with media_type."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <a href="files/doc.pdf">Document</a>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["file"], "files/doc.pdf")
        self.assertEqual(result["file_name"], "doc.pdf")
        self.assertEqual(result["media_type"], "")

    def test_bare_link_in_media_wrap_external_href_returns_none(self):
        """Bare link with external href in media_wrap returns None."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <a href="https://example.com/file.pdf">External</a>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNone(result)

    def test_media_wrap_no_media_no_link_returns_none(self):
        """media_wrap with no .media and no link returns None."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <span>No link here</span>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNone(result)

    def test_media_element_no_link_returns_none(self):
        """Media element with no <a> tag returns None (line 323)."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <div class="media media_video">
           <span>No link</span>
          </div>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNone(result)

    def test_media_element_with_hash_href_returns_none(self):
        """Media element with href starting with # returns None (line 327)."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <div class="media media_video">
           <a href="#anchor">Link</a>
          </div>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNone(result)

    def test_media_element_with_http_href_returns_none(self):
        """Media element with external http href returns None (line 327)."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <div class="media media_photo">
           <a href="http://example.com/photo.jpg">Photo</a>
          </div>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNone(result)

    def test_media_photo_with_img_dimensions(self):
        """media_photo CSS class with img dimensions extracts width/height (lines 330-339)."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <div class="media media_photo">
           <a href="photos/pic.jpg">
            <img style="width: 640px; height: 480px" src="photos/pic.jpg">
           </a>
          </div>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["photo"], "photos/pic.jpg")
        self.assertEqual(result["width"], 640)
        self.assertEqual(result["height"], 480)

    def test_media_photo_without_img_element(self):
        """media_photo without img element still returns photo path."""
        html = """
        <div class="body">
         <div class="media_wrap">
          <div class="media media_photo">
           <a href="photos/pic.jpg">Photo</a>
          </div>
         </div>
        </div>
        """
        body = self._make_soup(html).select_one(".body")
        result = _extract_html_media_info(body, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["photo"], "photos/pic.jpg")
        self.assertNotIn("width", result)
        self.assertNotIn("height", result)


# ---------------------------------------------------------------------------
# HTML parsing edge cases (lines 397-398, 410, 431, 440, 445, 457)
# ---------------------------------------------------------------------------


class TestParseHtmlExportEdgeCases(unittest.TestCase):
    """Cover HTML parsing edge cases for message IDs, bodies, and sender names."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_html(self, content, filename="messages.html"):
        filepath = os.path.join(self.temp_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    def test_non_numeric_message_id_skipped(self):
        """Message with non-numeric ID like 'messageABC' is skipped (lines 397-398)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="messageABC">
   <div class="body">
    <div class="from_name">Alice</div>
    <div class="text">Bad ID</div>
   </div>
  </div>
  <div class="message default clearfix" id="message50">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Good ID</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 50)

    def test_service_message_without_body_skipped(self):
        """Service message with no body element is skipped (line 410)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message service" id="message10">
  </div>
  <div class="message default clearfix" id="message11">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Valid message</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 11)

    def test_regular_message_without_body_skipped(self):
        """Regular message with no body element is skipped (line 431)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message20">
  </div>
  <div class="message default clearfix" id="message21">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Bob</div>
    <div class="text">Has body</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 21)

    def test_sender_name_strips_via_bot_suffix(self):
        """Sender name with 'via @BotName' suffix is stripped (line 440)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message30">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice via @InlineBot</div>
    <div class="text">Inline result</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], "Alice")

    def test_non_joined_message_inherits_last_sender(self):
        """Non-joined message without from_name inherits last sender (line 445)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message40">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Charlie</div>
    <div class="text">First</div>
   </div>
  </div>
  <div class="message default clearfix" id="message41">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:01:00">10:01</div>
    <div class="text">Second without from_name</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["from"], "Charlie")
        self.assertEqual(messages[1]["from"], "Charlie")

    def test_first_message_no_sender_uses_empty_string(self):
        """First message with no from_name and no prior sender uses empty string."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message60">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="text">Anonymous message</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], "")

    def test_br_tags_converted_to_newlines_in_text(self):
        """<br> tags inside text div are replaced with newlines (line 457)."""
        html = """\
<html><body>
<div class="page_wrap">
 <div class="page_body chat_page"><div class="history">
  <div class="message default clearfix" id="message70">
   <div class="body">
    <div class="pull_right date details" title="15.01.2024 10:00:00">10:00</div>
    <div class="from_name">Alice</div>
    <div class="text">Line one<br>Line two<br>Line three</div>
   </div>
  </div>
 </div></div>
</div>
</body></html>"""
        self._write_html(html)
        html_files = _find_html_files(Path(self.temp_dir))
        _, messages = _parse_html_export(html_files, Path(self.temp_dir))
        self.assertEqual(len(messages), 1)
        self.assertIn("\n", messages[0]["text"])
        self.assertIn("Line one", messages[0]["text"])
        self.assertIn("Line two", messages[0]["text"])
        self.assertIn("Line three", messages[0]["text"])


# ---------------------------------------------------------------------------
# TelegramImporter async tests (lines 513-518, 558, 570-571, 588-591,
# 647, 669-670, 724, 727-731)
# ---------------------------------------------------------------------------


class TestTelegramImporterCreate(unittest.TestCase):
    """Cover lines 513-515: TelegramImporter.create classmethod."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @patch("src.telegram_import.get_adapter")
    @patch("src.telegram_import.init_database")
    def test_create_initializes_db_and_returns_importer(self, mock_init_db, mock_get_adapter):
        """TelegramImporter.create calls init_database and get_adapter."""
        mock_init_db.return_value = None
        mock_db = AsyncMock()
        mock_get_adapter.return_value = mock_db

        importer = self._run(TelegramImporter.create("/tmp/media"))

        mock_init_db.assert_awaited_once()
        mock_get_adapter.assert_awaited_once()
        self.assertIsInstance(importer, TelegramImporter)
        self.assertEqual(importer.media_path, "/tmp/media")


class TestTelegramImporterClose(unittest.TestCase):
    """Cover line 518: TelegramImporter.close method."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @patch("src.telegram_import.close_database")
    def test_close_calls_close_database(self, mock_close_db):
        """TelegramImporter.close delegates to close_database."""
        mock_close_db.return_value = None
        db = AsyncMock()
        importer = TelegramImporter(db, "/tmp/media")

        self._run(importer.close())

        mock_close_db.assert_awaited_once()


class TestImporterRunEdgeCases(unittest.TestCase):
    """Cover lines 558, 570-571, 588-591, 647, 669-670, 724, 727-731."""

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

    def test_empty_chats_raises_value_error(self):
        """run raises ValueError when export has no chats (line 558)."""
        self._write_export({"chats": {"list": []}})
        db = AsyncMock()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        with self.assertRaises(ValueError) as ctx:
            self._run(importer.run(self.export_dir))
        self.assertIn("No chats found", str(ctx.exception))

    def test_chat_with_zero_id_skipped(self):
        """Chat that derives to ID 0 is skipped with warning (lines 570-571)."""
        self._write_export(
            {
                "chats": {
                    "list": [
                        {
                            "name": "Zero ID Chat",
                            "type": "personal_chat",
                            "id": 0,
                            "messages": [
                                {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Hi"},
                            ],
                        },
                    ]
                }
            }
        )
        db = AsyncMock()
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["chats_imported"], 0)
        self.assertEqual(summary["total_messages"], 0)

    def test_chat_id_override_with_multi_chat_imports_only_first(self):
        """chat_id_override with multiple chats imports only the first (lines 588-591)."""
        self._write_export(
            {
                "chats": {
                    "list": [
                        {
                            "name": "Chat A",
                            "type": "personal_chat",
                            "id": 100,
                            "messages": [
                                {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "A"},
                            ],
                        },
                        {
                            "name": "Chat B",
                            "type": "personal_chat",
                            "id": 200,
                            "messages": [
                                {"id": 2, "type": "message", "date": "2024-01-15T11:00:00", "text": "B"},
                            ],
                        },
                    ]
                }
            }
        )
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, chat_id_override=-1009999))

        self.assertEqual(summary["chats_imported"], 1)
        self.assertEqual(summary["details"][0]["chat_name"], "Chat A")

    def test_message_without_id_skipped(self):
        """Message with no 'id' field is skipped (line 647)."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"type": "message", "date": "2024-01-15T10:00:00", "text": "No ID"},
                    {"id": 1, "type": "message", "date": "2024-01-15T10:01:00", "text": "Has ID"},
                ],
            }
        )
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 1)

    def test_message_without_valid_date_skipped(self):
        """Message with no valid date is skipped with warning (lines 669-670)."""
        self._write_export(
            {
                "name": "Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "text": "No date at all"},
                    {"id": 2, "type": "message", "date": "2024-01-15T10:00:00", "text": "Valid"},
                ],
            }
        )
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["details"][0]["messages"], 1)

    def test_media_file_not_found_logs_warning(self):
        """Missing media source file logs warning instead of crashing (line 724)."""
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
                        "photo": "photos/missing_photo.jpg",
                    },
                ],
            }
        )
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        self.assertEqual(summary["total_messages"], 1)
        self.assertEqual(summary["total_media"], 0)
        db.insert_media.assert_not_called()

    def test_batch_flush_at_batch_size_boundary(self):
        """Messages are flushed in batches when count reaches BATCH_SIZE (lines 727-731)."""
        messages = []
        for i in range(1, BATCH_SIZE + 10):
            messages.append(
                {
                    "id": i,
                    "type": "message",
                    "date": "2024-01-15T10:00:00",
                    "from": "User",
                    "from_id": "user1",
                    "text": f"Message {i}",
                }
            )

        self._write_export(
            {
                "name": "Big Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": messages,
            }
        )

        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 0}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir))

        total = BATCH_SIZE + 9
        self.assertEqual(summary["total_messages"], total)
        # insert_messages_batch called at least twice: once at BATCH_SIZE, once for remainder
        self.assertGreaterEqual(db.insert_messages_batch.await_count, 2)

    def test_merge_flag_skips_existing_check(self):
        """With merge=True, existing chat stats check is bypassed."""
        self._write_export(
            {
                "name": "Existing Chat",
                "type": "personal_chat",
                "id": 42,
                "messages": [
                    {"id": 1, "type": "message", "date": "2024-01-15T10:00:00", "text": "Merge me"},
                ],
            }
        )
        db = AsyncMock()
        db.get_chat_stats.return_value = {"messages": 500}
        importer = TelegramImporter(db, os.path.join(self.temp_dir, "media"))

        summary = self._run(importer.run(self.export_dir, merge=True))

        self.assertEqual(summary["total_messages"], 1)
        db.upsert_chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
