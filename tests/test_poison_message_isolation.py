"""Unreadable documents must never crash a capture path.

Telegram returns ``documentEmpty`` for a document that is no longer retrievable.
Telethon deserializes that into ``DocumentEmpty``, which is truthy but carries no
``.attributes``. Walking it raises ``AttributeError``, which used to abort the whole
dialog in the sweep and drop the message outright in the listener -- and because the
sync cursor is checkpointed before the offending message, every later run resumed at
the same message and failed the same way, so that chat never advanced again.

These tests pin the defensive probe in every copy of the pattern.
"""

import unittest
from unittest.mock import MagicMock

from telethon.tl.types import DocumentEmpty, MessageMediaDocument

from src.listener import TelegramListener
from src.telegram_backup import TelegramBackup


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


if __name__ == "__main__":
    unittest.main()
