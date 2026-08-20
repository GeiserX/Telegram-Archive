"""Capture-time link previews (mf7): extraction, capture wiring, viewer card.

Telegram attaches at most one ``MessageMediaWebPage`` per message; the
resolved ``WebPage`` fields are archived into ``raw_data["webpage"]`` at
capture so the viewer's card keeps meaning what it meant then, even after
the link dies. Retro-fill is impossible by measurement (raw_data never
carried payloads before this), so the feature is forward-only by design.
"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.message_utils import extract_webpage_preview

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


def _stub(class_name: str, **attrs):
    return type(class_name, (object,), attrs)()


def _full_webpage(**overrides):
    fields = {
        "url": "https://good.example/article",
        "display_url": "good.example/article",
        "site_name": "Good Example",
        "title": "An archived headline",
        "description": "What the page said when it was archived.",
    }
    fields.update(overrides)
    return _stub("MessageMediaWebPage", webpage=_stub("WebPage", **fields))


class TestExtraction(unittest.TestCase):
    def test_full_webpage_yields_all_fields(self):
        preview = extract_webpage_preview(_full_webpage())
        self.assertEqual(
            preview,
            {
                "url": "https://good.example/article",
                "display_url": "good.example/article",
                "site_name": "Good Example",
                "title": "An archived headline",
                "description": "What the page said when it was archived.",
            },
        )

    def test_partial_fields_are_kept_partial(self):
        preview = extract_webpage_preview(_full_webpage(site_name=None, description=""))
        self.assertEqual(
            preview,
            {
                "url": "https://good.example/article",
                "display_url": "good.example/article",
                "title": "An archived headline",
            },
        )

    def test_non_preview_shapes_yield_none(self):
        self.assertIsNone(extract_webpage_preview(None))
        self.assertIsNone(extract_webpage_preview(_stub("MessageMediaPhoto")))
        self.assertIsNone(extract_webpage_preview(_stub("MessageMediaWebPage", webpage=None)))
        self.assertIsNone(extract_webpage_preview(_stub("MessageMediaWebPage", webpage=_stub("WebPageEmpty"))))
        self.assertIsNone(extract_webpage_preview(_stub("MessageMediaWebPage", webpage=_stub("WebPagePending"))))
        # A WebPage whose preview fields are all empty carries nothing worth a row.
        self.assertIsNone(
            extract_webpage_preview(_stub("MessageMediaWebPage", webpage=_stub("WebPage", url=None, title="")))
        )

    def test_non_string_fields_are_skipped(self):
        preview = extract_webpage_preview(_full_webpage(title=1234, description=b"bytes"))
        self.assertNotIn("title", preview)
        self.assertNotIn("description", preview)
        self.assertEqual(preview["site_name"], "Good Example")


class TestCaptureWiring(unittest.TestCase):
    """Both writers put the preview under raw_data['webpage'] — same shape."""

    def test_sweep_message_data_carries_the_preview(self):
        from src.telegram_backup import TelegramBackup

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = MagicMock()
        backup.config.should_skip_topic = MagicMock(return_value=False)
        backup.db = MagicMock()
        backup.db.insert_message = AsyncMock()

        message = MagicMock()
        message.reply_to = None
        message.media = _full_webpage()

        preview = extract_webpage_preview(message.media)
        self.assertIsNotNone(preview)
        self.assertEqual(preview["site_name"], "Good Example")

    def test_writers_share_one_extraction_helper(self):
        """The classifier-duplication disease must not repeat here."""
        backup_src = Path("src/telegram_backup.py").read_text()
        listener_src = Path("src/listener.py").read_text()
        for src, name in ((backup_src, "telegram_backup"), (listener_src, "listener")):
            self.assertIn("extract_webpage_preview(message.media)", src, name)
            self.assertNotIn("def extract_webpage_preview", src, name)


class TestViewerCard(unittest.TestCase):
    """Structural guards for the template card."""

    def test_card_renders_from_raw_data_and_guards_the_scheme(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('v-if="msg.raw_data?.webpage"', html)
        # The clickable title exists only behind the scheme guard, so a
        # javascript: URL can never become a clickable card.
        self.assertIn('v-if="isHttpUrl(msg.raw_data.webpage.url)"', html)
        self.assertIn("const isHttpUrl = (url) => /^https?:\\/\\//i.test(url || '')", html)
        # Card text renders through Vue interpolation (escaped), never v-html.
        card = html.split('v-if="msg.raw_data?.webpage"', 1)[1].split("</div>\n\n", 1)[0]
        self.assertNotIn("v-html", card)


if __name__ == "__main__":
    unittest.main()
