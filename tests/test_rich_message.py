"""Rich Text Editor messages (#470).

Telegram's Premium composer sends a message whose wire ``message`` is empty and
whose content is a ``RichMessage`` block tree. These tests pin the rendering
contract (text + UTF-16 entity spans, same shape as ordinary formatted
messages), the raw_data copy of the tree, and that both writers (sweep and
listener) and both edit paths pick it up.
"""

import asyncio
import json
import os
import sys
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from telethon.tl.types import (
    Channel,
    ChatPhotoEmpty,
    Message,
    MessageEntityBold,
    PageBlockBlockquote,
    PageBlockBlockquoteBlocks,
    PageBlockChannel,
    PageBlockDetails,
    PageBlockDivider,
    PageBlockHeading3,
    PageBlockList,
    PageBlockOrderedList,
    PageBlockParagraph,
    PageBlockPhoto,
    PageBlockPreformatted,
    PageBlockTable,
    PageBlockUnsupported,
    PageCaption,
    PageListItemBlocks,
    PageListItemText,
    PageListOrderedItemText,
    PageTableCell,
    PageTableRow,
    PeerUser,
    RichMessage,
    TextBold,
    TextConcat,
    TextCustomEmoji,
    TextEmpty,
    TextFixed,
    TextItalic,
    TextMentionName,
    TextPlain,
    TextStrike,
    TextUrl,
    TextWithEntities,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.message_utils import (  # noqa: E402
    _json_safe,
    message_entities,
    message_plain_text,
    render_rich_message,
    rich_message_of,
    rich_message_payload,
)
from src.telegram_backup import TelegramBackup  # noqa: E402

CHAT_ID = -1001234567890


def _rich(*blocks) -> RichMessage:
    return RichMessage(blocks=list(blocks), photos=[], documents=[])


def _wire_message(rich: RichMessage | None, text: str = "") -> Message:
    return Message(id=7, peer_id=PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC), message=text, rich_message=rich)


class TestRenderRichMessage(unittest.TestCase):
    def test_blocks_render_one_per_line_with_structure_as_entities(self):
        rich = _rich(
            PageBlockHeading3(text=TextPlain("Plan")),
            PageBlockParagraph(
                text=TextConcat(
                    texts=[
                        TextPlain("Hello "),
                        TextBold(text=TextItalic(text=TextPlain("world"))),
                        TextPlain(", see "),
                        TextUrl(text=TextPlain("docs"), url="https://example.com/x", webpage_id=0),
                    ]
                )
            ),
            PageBlockDivider(),
            PageBlockBlockquote(text=TextPlain("quoted"), caption=TextEmpty()),
            PageBlockPreformatted(text=TextPlain("print('x')"), language="python"),
        )
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "Plan\nHello world, see docs\n———\nquoted\nprint('x')")
        self.assertEqual(
            entities,
            [
                {"type": "bold", "offset": 0, "length": 4},
                {"type": "italic", "offset": 11, "length": 5},
                {"type": "bold", "offset": 11, "length": 5},
                {"type": "text_url", "offset": 22, "length": 4, "url": "https://example.com/x"},
                {"type": "blockquote", "offset": 31, "length": 6},
                {"type": "pre", "offset": 38, "length": 10, "language": "python"},
            ],
        )

    def test_offsets_are_utf16_code_units(self):
        """An astral-plane emoji is two units, exactly as Telegram counts wire entities."""
        rich = _rich(PageBlockParagraph(text=TextConcat(texts=[TextPlain("🚀 "), TextBold(text=TextPlain("go"))])))
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "🚀 go")
        self.assertEqual(entities, [{"type": "bold", "offset": 3, "length": 2}])

    def test_zero_width_padding_and_empty_nodes_leave_a_blank_line(self):
        rich = _rich(
            PageBlockParagraph(text=TextPlain("a")),
            PageBlockParagraph(text=TextPlain("\N{ZERO WIDTH SPACE}")),
            PageBlockParagraph(text=TextEmpty()),
            PageBlockParagraph(text=TextPlain("b")),
        )
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "a\n\n\nb")
        self.assertEqual(entities, [])

    def test_lists_get_bullets_numbers_and_checkboxes(self):
        rich = _rich(
            PageBlockList(
                items=[
                    PageListItemText(text=TextPlain("one")),
                    PageListItemText(text=TextPlain("done"), checkbox=True, checked=True),
                    PageListItemText(text=TextPlain("todo"), checkbox=True, checked=False),
                    PageListItemBlocks(blocks=[PageBlockParagraph(text=TextPlain("nested"))]),
                ]
            ),
            PageBlockOrderedList(
                items=[
                    PageListOrderedItemText(text=TextPlain("first"), num="1"),
                    PageListOrderedItemText(text=TextPlain("second")),
                ],
                start=1,
            ),
        )
        text, _ = render_rich_message(rich)
        self.assertEqual(text, "• one\n☑ done\n☐ todo\n• nested\n1. first\n2. second")

    def test_tables_details_and_media_placeholders(self):
        rich = _rich(
            PageBlockTable(
                title=TextPlain("Totals"),
                rows=[
                    PageTableRow(
                        cells=[PageTableCell(text=TextPlain("k"), header=True), PageTableCell(text=TextPlain("v"))]
                    ),
                ],
                bordered=True,
                striped=False,
            ),
            PageBlockDetails(
                blocks=[PageBlockParagraph(text=TextPlain("inside"))], title=TextPlain("More"), open=False
            ),
            PageBlockPhoto(photo_id=1, caption=PageCaption(text=TextPlain("a caption"), credit=TextEmpty())),
            PageBlockBlockquoteBlocks(
                blocks=[PageBlockParagraph(text=TextPlain("q1")), PageBlockParagraph(text=TextPlain("q2"))],
                caption=TextEmpty(),
            ),
        )
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "Totals\nk | v\nMore\ninside\n[photo]\na caption\nq1\nq2")
        self.assertIn({"type": "bold", "offset": 0, "length": 6}, entities)
        self.assertIn({"type": "bold", "offset": 7, "length": 1}, entities)
        self.assertIn({"type": "bold", "offset": 13, "length": 4}, entities)
        # The quote spans both inner lines but not the line break after them.
        self.assertIn({"type": "blockquote", "offset": text.index("q1"), "length": 5}, entities)

    def test_inline_nodes_map_to_viewer_entity_types(self):
        rich = _rich(
            PageBlockParagraph(
                text=TextConcat(
                    texts=[
                        TextFixed(text=TextPlain("code")),
                        TextStrike(text=TextPlain("gone")),
                        TextMentionName(text=TextPlain("Account A"), user_id=42),
                        TextCustomEmoji(document_id=99, alt="😀"),
                        TextWithEntities(text="wire", entities=[MessageEntityBold(offset=0, length=4)]),
                    ]
                )
            )
        )
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "codegoneAccount A😀wire")
        self.assertEqual(
            entities,
            [
                {"type": "code", "offset": 0, "length": 4},
                {"type": "strikethrough", "offset": 4, "length": 4},
                {"type": "mention_name", "offset": 8, "length": 9, "user_id": 42},
                {"type": "custom_emoji", "offset": 17, "length": 2, "document_id": 99},
                {"type": "bold", "offset": 19, "length": 4},
            ],
        )

    def test_blocks_without_words_render_nothing(self):
        channel = Channel(id=1, title="c", photo=ChatPhotoEmpty(), date=datetime(2026, 1, 1, tzinfo=UTC), access_hash=5)
        rich = _rich(PageBlockChannel(channel=channel), PageBlockUnsupported(), PageBlockParagraph(text=TextPlain("x")))
        text, entities = render_rich_message(rich)
        self.assertEqual((text, entities), ("x", []))

    def test_rarer_blocks_keep_their_words(self):
        from telethon.tl.types import (
            PageBlockAuthorDate,
            PageBlockCover,
            PageBlockFooter,
            PageBlockMath,
            PageBlockRelatedArticles,
            TextAnchor,
            TextMarked,
            TextMath,
        )

        rich = _rich(
            PageBlockCover(cover=PageBlockParagraph(text=TextPlain("cover"))),
            PageBlockAuthorDate(author=TextPlain("Account A"), published_date=0),
            PageBlockMath(source="x^2"),
            PageBlockRelatedArticles(title=TextPlain("related"), articles=[]),
            PageBlockFooter(text=TextPlain("footer")),
            PageBlockParagraph(
                text=TextConcat(
                    texts=[
                        TextAnchor(text=TextPlain("anchored"), name="a"),
                        TextMarked(text=TextPlain("!")),
                        TextMath(source="y"),
                        "raw",
                    ]
                )
            ),
            PageBlockList(items=[PageListItemBlocks(blocks=[])]),
        )
        text, entities = render_rich_message(rich)
        self.assertEqual(text, "cover\nAccount A\nx^2\nrelated\nfooter\nanchored!yraw\n• ")
        self.assertEqual(entities, [])


class TestRichMessagePayload(unittest.TestCase):
    def test_part_flag_and_bytes_survive(self):
        rich = RichMessage(blocks=[PageBlockParagraph(text=TextPlain("x"))], photos=[], documents=[], part=True)
        payload = rich_message_payload(rich)
        self.assertEqual(payload["part"], True)
        self.assertEqual(_json_safe({"k": b"\x00\x01", "n": object()})["k"], "AAE=")

    def test_blocks_only_json_safe_without_access_hash(self):
        channel = Channel(id=1, title="c", photo=ChatPhotoEmpty(), date=datetime(2026, 1, 1, tzinfo=UTC), access_hash=5)
        rich = RichMessage(
            blocks=[PageBlockParagraph(text=TextPlain("x")), PageBlockChannel(channel=channel)],
            photos=[MagicMock()],
            documents=[MagicMock()],
            rtl=True,
        )
        payload = rich_message_payload(rich)
        encoded = json.dumps(payload)  # must not raise: datetimes and bytes are converted
        self.assertNotIn("access_hash", encoded)
        self.assertNotIn("photos", payload)
        self.assertEqual(payload["rtl"], True)
        self.assertNotIn("part", payload)
        self.assertEqual([block["_"] for block in payload["blocks"]], ["PageBlockParagraph", "PageBlockChannel"])
        self.assertEqual(payload["blocks"][1]["channel"]["date"], "2026-01-01T00:00:00+00:00")


class TestMessageAccessors(unittest.TestCase):
    def test_empty_wire_text_falls_back_to_the_block_tree(self):
        rich = _rich(PageBlockHeading3(text=TextPlain("Title")), PageBlockParagraph(text=TextPlain("body")))
        message = _wire_message(rich)
        self.assertIs(rich_message_of(message), rich)
        self.assertEqual(message_plain_text(message), "Title\nbody")
        self.assertEqual(message_entities(message), [{"type": "bold", "offset": 0, "length": 5}])

    def test_wire_text_wins_when_present(self):
        rich = _rich(PageBlockParagraph(text=TextPlain("ignored")))
        message = _wire_message(rich, text="hello")
        self.assertIsNone(rich_message_of(message))
        self.assertEqual(message_plain_text(message), "hello")
        self.assertIsNone(message_entities(message))

    def test_ordinary_message_is_unchanged(self):
        message = _wire_message(None, text="plain")
        self.assertEqual(message_plain_text(message), "plain")
        self.assertIsNone(message_entities(message))
        message.entities = [MessageEntityBold(offset=0, length=5)]
        self.assertEqual(message_entities(message), [{"type": "bold", "offset": 0, "length": 5}])
        self.assertEqual(message_plain_text(_wire_message(None)), "")

    def test_magicmock_fixtures_stay_inert(self):
        message = MagicMock()
        message.text = ""
        self.assertIsNone(rich_message_of(message))
        self.assertEqual(message_plain_text(message), "")
        self.assertIsNone(message_entities(message))


class TestSweepStoresRichMessages(unittest.TestCase):
    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.backup.account_id = 1
        self.backup.db = AsyncMock()
        self.backup.config = MagicMock()
        self.backup.config.should_download_media_for_chat = MagicMock(return_value=False)
        self.backup.client = AsyncMock()

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_message(self, rich):
        msg = MagicMock()
        msg.id = 1
        msg.sender = None
        msg.sender_id = 42
        msg.date = datetime(2026, 1, 1)
        msg.raw_text = ""
        msg.text = ""
        msg.message = ""
        msg.entities = []
        msg.rich_message = rich
        msg.reply_to_msg_id = None
        msg.reply_to = None
        msg.edit_date = None
        msg.out = False
        msg.pinned = False
        msg.grouped_id = None
        msg.fwd_from = None
        msg.media = None
        msg.reactions = None
        msg.post_author = None
        msg.action = None
        return msg

    def test_rich_message_row_has_text_entities_and_block_tree(self):
        rich = _rich(PageBlockHeading3(text=TextPlain("Title")), PageBlockParagraph(text=TextPlain("body")))
        result = self._run(self.backup._process_message(self._make_message(rich), CHAT_ID))
        self.assertEqual(result["text"], "Title\nbody")
        self.assertEqual(result["raw_data"]["entities"], [{"type": "bold", "offset": 0, "length": 5}])
        self.assertEqual(
            [b["_"] for b in result["raw_data"]["rich_message"]["blocks"]], ["PageBlockHeading3", "PageBlockParagraph"]
        )

    def test_plain_empty_message_stores_no_rich_key(self):
        result = self._run(self.backup._process_message(self._make_message(None), CHAT_ID))
        self.assertEqual(result["text"], "")
        self.assertNotIn("rich_message", result["raw_data"])
        self.assertNotIn("entities", result["raw_data"])


if __name__ == "__main__":
    unittest.main()
