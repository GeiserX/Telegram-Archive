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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import FloodWaitError
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
    PeerChannel,
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

from telegram_archive.message_utils import (  # noqa: E402
    _json_safe,
    message_entities,
    message_plain_text,
    render_rich_message,
    rich_message_of,
    rich_message_payload,
)
from telegram_archive.telegram_backup import TelegramBackup, complete_rich_message  # noqa: E402

CHAT_ID = -1001234567890


def _rich(*blocks) -> RichMessage:
    return RichMessage(blocks=list(blocks), photos=[], documents=[])


def _wire_message(rich: RichMessage | None, text: str = "") -> Message:
    return Message(id=7, peer_id=PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC), message=text, rich_message=rich)


def _partial(*blocks) -> RichMessage:
    """A block tree Telegram delivered cut short."""
    return RichMessage(blocks=list(blocks), photos=[], documents=[], part=True)


def _whole_message(**kwargs) -> Message:
    """The message as messages.getRichMessage returns it: the whole tree, part unset."""
    whole = _rich(PageBlockParagraph(text=TextPlain("first")), PageBlockParagraph(text=TextBold(TextPlain("second"))))
    fields = {"id": 7, "peer_id": PeerChannel(1234567890), "date": datetime(2026, 1, 1, tzinfo=UTC), "message": ""}
    fields.update(kwargs)
    return Message(rich_message=fields.pop("rich_message", whole), **fields)


def _fetching_client(*answer):
    """A client whose messages.getRichMessage answers with ``answer``."""
    return AsyncMock(return_value=SimpleNamespace(messages=list(answer)))


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
        # 8.1B (9.0): "no formatting" is stored as an empty list.
        self.assertEqual(result["raw_data"]["entities"], [])

    def _partial_message(self):
        """A sweep message whose tree arrived cut short."""
        message = self._make_message(_partial(PageBlockParagraph(text=TextPlain("first"))))
        message.id = 7
        message.peer_id = PeerChannel(1234567890)
        return message

    def test_sweep_stores_the_whole_tree_of_a_partial_message(self):
        """The sweep fetches the rest of a cut-short tree and stores the whole text."""
        self.backup.client = _fetching_client(_whole_message())
        message = self._partial_message()
        result = self._run(self.backup._process_message(message, CHAT_ID))
        self.assertEqual(result["text"], "first\nsecond")
        self.assertEqual(result["raw_data"]["entities"], [{"type": "bold", "offset": 6, "length": 6}])
        self.assertNotIn("part", result["raw_data"]["rich_message"])
        self.assertFalse(result["text_may_be_truncated"])
        self.assertEqual(self.backup.client.call_args.args[0].peer, PeerChannel(1234567890))

    def test_sweep_of_a_tree_still_partial_says_the_text_may_be_truncated(self):
        """The fetch failed: the row says so, and the upsert keeps an archived text
        that starts with this one."""
        self.backup.client = _fetching_client()
        self.backup.client.side_effect = ConnectionError("unreachable")
        result = self._run(self.backup._process_message(self._partial_message(), CHAT_ID))
        self.assertEqual(result["text"], "first")
        self.assertTrue(result["text_may_be_truncated"])

    def test_backup_edit_applies_the_whole_tree(self):
        """The backup's edit path fetches the rest and edits with the whole text."""
        self.backup.client = _fetching_client(_whole_message())
        self.backup.db.update_message_text = AsyncMock(return_value=("applied", None))
        self._run(self.backup._apply_edit(self._partial_message(), CHAT_ID, "sync", media_changed=False))
        args = self.backup.db.update_message_text.call_args
        self.assertEqual(args.args[2], "first\nsecond")
        self.assertNotIn("part", args.kwargs["rich_message"])
        self.assertFalse(args.kwargs["text_may_be_truncated"])

    def test_backup_edit_of_a_tree_still_partial_says_the_text_may_be_truncated(self):
        """The fetch failed: the adapter decides (a prefix of the archived text is
        not an edit), and a replaced photo or file still moves the edit date."""
        self.backup.client = _fetching_client()
        self.backup.client.side_effect = ConnectionError("unreachable")
        self.backup.db.update_message_text = AsyncMock(return_value=("noop", None))
        message = self._partial_message()
        message.edit_hide = True
        outcome = self._run(self.backup._apply_edit(message, CHAT_ID, "sync", media_changed=True))
        self.assertEqual(outcome, "noop")
        args = self.backup.db.update_message_text.call_args
        self.assertEqual(args.args[2], "first")
        self.assertTrue(args.kwargs["text_may_be_truncated"])
        self.assertTrue(args.kwargs["media_changed"])


class TestCompleteRichMessage(unittest.TestCase):
    """A long Rich Text Editor message arrives with a truncated tree (``part=True``);
    ``messages.getRichMessage`` returns the whole of it."""

    def _partial_message(self, **kwargs):
        """A wire message whose tree arrived cut short."""
        fields = {"id": 7, "peer_id": PeerChannel(1234567890), "date": datetime(2026, 1, 1, tzinfo=UTC), "message": ""}
        fields.update(kwargs)
        return Message(rich_message=_partial(PageBlockParagraph(text=TextPlain("first"))), **fields)

    def test_partial_tree_is_replaced_by_the_whole_one(self):
        """The fetched whole tree replaces the partial one in place."""
        from telethon.tl.functions.messages import GetRichMessageRequest

        message = self._partial_message()
        fetched = _whole_message()
        client = _fetching_client(fetched)

        self.assertTrue(asyncio.run(complete_rich_message(client, message)))

        self.assertIs(message.rich_message, fetched.rich_message)
        self.assertEqual(message_plain_text(message), "first\nsecond")
        self.assertEqual(message_entities(message), [{"type": "bold", "offset": 6, "length": 6}])
        request = client.call_args.args[0]
        self.assertIsInstance(request, GetRichMessageRequest)
        self.assertEqual((request.peer, request.id), (PeerChannel(1234567890), 7))

    def test_a_tree_that_arrived_whole_asks_nothing(self):
        """No request for a whole tree, no tree, or a message with wire text."""
        for message in (
            _wire_message(_rich(PageBlockParagraph(text=TextPlain("whole")))),
            _wire_message(None),
            _wire_message(_partial(PageBlockParagraph(text=TextPlain("x"))), text="wire text wins"),
        ):
            with self.subTest(message=message.message or message.rich_message):
                client = _fetching_client()
                self.assertTrue(asyncio.run(complete_rich_message(client, message)))
                client.assert_not_called()

    def test_a_failed_request_keeps_the_partial_tree(self):
        """A failed request is not retried, keeps the partial tree, and logs its type only."""
        message = self._partial_message()
        partial = message.rich_message
        client = _fetching_client()
        client.side_effect = ConnectionError("unreachable")

        with self.assertLogs("telegram_archive.telegram_backup", level="WARNING") as logs:
            self.assertFalse(asyncio.run(complete_rich_message(client, message)))

        self.assertIs(message.rich_message, partial)
        self.assertEqual(client.call_count, 1)  # not retried: the partial tree is kept instead
        self.assertIn("ConnectionError", logs.output[0])
        self.assertNotIn("unreachable", logs.output[0])
        self.assertNotIn("first", logs.output[0])

    def test_only_the_same_message_at_the_same_edit_and_complete_is_taken(self):
        """Another message, another edit, a tree still partial or another text is not taken."""
        cases = {
            "another message": _whole_message(id=8),
            "a newer edit": _whole_message(edit_date=datetime(2026, 1, 2, tzinfo=UTC)),
            "still partial": _whole_message(rich_message=_partial(PageBlockParagraph(text=TextPlain("first")))),
            "another text (same-second edit)": _whole_message(
                rich_message=_rich(
                    PageBlockParagraph(text=TextPlain("rewritten")), PageBlockParagraph(text=TextPlain("x"))
                )
            ),
        }
        for name, fetched in cases.items():
            with self.subTest(name):
                message = self._partial_message()
                partial = message.rich_message
                self.assertFalse(asyncio.run(complete_rich_message(_fetching_client(fetched), message)))
                self.assertIs(message.rich_message, partial)

    def test_a_flood_wait_pauses_completion_for_as_long_as_telegram_asked(self):
        """A FloodWait is not waited out: the partial tree stays, and that client asks
        nothing more until the wait has passed."""
        clock = [1000.0]
        client = _fetching_client()
        client.side_effect = [FloodWaitError(request=None, capture=30), SimpleNamespace(messages=[_whole_message()])]

        with patch("telegram_archive.telegram_backup.time", SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertLogs("telegram_archive.telegram_backup", level="WARNING") as logs:
                self.assertFalse(asyncio.run(complete_rich_message(client, self._partial_message())))
            self.assertEqual(client.call_count, 1)  # not retried
            self.assertIn("30s", logs.output[0])
            self.assertNotIn("first", logs.output[0])

            clock[0] += 29
            message = self._partial_message()
            partial = message.rich_message
            self.assertFalse(asyncio.run(complete_rich_message(client, message)))
            self.assertIs(message.rich_message, partial)
            self.assertEqual(client.call_count, 1)  # paused: nothing asked
            other = _fetching_client(_whole_message())
            self.assertTrue(asyncio.run(complete_rich_message(other, self._partial_message())))

            clock[0] += 2
            self.assertTrue(asyncio.run(complete_rich_message(client, self._partial_message())))
            self.assertEqual(client.call_count, 2)


if __name__ == "__main__":
    unittest.main()
