"""Nine media kinds stop vanishing (9t6.10.2).

Venue, dice, invoice, story, giveaway, giveaway results, live location, game
and unsupported media used to flatten to nothing: ``_get_media_type`` returned
None, no media record was written, and the viewer showed a bare bubble.
Official apps render each as a typed placeholder. Now:

- ``classify_extended_media`` names the kind (name-based, MagicMock-inert);
- ``extract_extended_media_details`` captures salient fields into
  ``raw_data[<kind>]`` for the viewer chip;
- both writers store a metadata-only media row (downloaded=0, no file);
- the download drain and the operator status counts exclude metadata-only
  kinds, so archives full of polls/dice do not show a permanently-red
  pending pipeline.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db.adapter import DatabaseAdapter
from src.db.base import DatabaseManager
from src.db.models import Base, Chat, Media, Message
from src.listener import TelegramListener
from src.message_utils import (
    _EXTENDED_MEDIA_TYPES,
    METADATA_ONLY_MEDIA_TYPES,
    classify_extended_media,
    extract_extended_media_details,
)
from src.telegram_backup import TelegramBackup

CHAT_ID = -1001


def _fake_media(class_name: str, **attrs):
    """An object whose type NAME matches a Telethon MessageMedia class.

    classify_extended_media is name-based (like service_action_type), so a
    locally defined class with the right name exercises the real contract.
    """
    cls = type(class_name, (), {})
    obj = cls()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


class TestClassifyExtendedMedia(unittest.TestCase):
    def test_real_telethon_classes_map_to_their_kind(self):
        """Every name in the map is a REAL Telethon class and classifies.

        Guards against a typo'd key: duck-typed tests alone would keep passing
        while real captures silently missed the kind.
        """
        import telethon.tl.types as tl

        for class_name, kind in _EXTENDED_MEDIA_TYPES.items():
            cls = getattr(tl, class_name)  # AttributeError => stale map
            instance = object.__new__(cls)
            self.assertEqual(classify_extended_media(instance), kind)

    def test_covers_all_metadata_only_kinds_beyond_the_original_three(self):
        self.assertEqual(
            set(_EXTENDED_MEDIA_TYPES.values()),
            METADATA_ONLY_MEDIA_TYPES - {"contact", "geo", "poll"},
        )

    def test_magicmock_is_inert(self):
        self.assertIsNone(classify_extended_media(MagicMock()))

    def test_none_and_unrelated_types_are_none(self):
        self.assertIsNone(classify_extended_media(None))
        self.assertIsNone(classify_extended_media(_fake_media("MessageMediaPhoto")))


class TestExtractExtendedMediaDetails(unittest.TestCase):
    def test_dice(self):
        media = _fake_media("MessageMediaDice", emoticon="🎯", value=6)
        self.assertEqual(
            extract_extended_media_details(media),
            ("dice", {"emoticon": "🎯", "value": 6}),
        )

    def test_venue(self):
        media = _fake_media(
            "MessageMediaVenue",
            title="Cafe",
            address="Calle Mayor 1",
            provider="foursquare",
            geo=SimpleNamespace(lat=40.4, long=-3.7),
        )
        kind, details = extract_extended_media_details(media)
        self.assertEqual(kind, "venue")
        self.assertEqual(details["title"], "Cafe")
        self.assertEqual(details["address"], "Calle Mayor 1")
        self.assertEqual(details["lat"], 40.4)
        self.assertEqual(details["long"], -3.7)

    def test_invoice(self):
        media = _fake_media(
            "MessageMediaInvoice",
            title="Subscription",
            description="One month",
            currency="USD",
            total_amount=14500,
            test=False,
        )
        kind, details = extract_extended_media_details(media)
        self.assertEqual(kind, "invoice")
        self.assertEqual(details["total_amount"], 14500)
        self.assertEqual(details["currency"], "USD")
        self.assertFalse(details["test"])

    def test_story_uses_real_peer_id(self):
        from telethon.tl.types import PeerChannel

        media = _fake_media("MessageMediaStory", peer=PeerChannel(channel_id=123), id=77)
        kind, details = extract_extended_media_details(media)
        self.assertEqual(kind, "story")
        self.assertEqual(details["story_id"], 77)
        self.assertEqual(details["peer_id"], -1000000000123)

    def test_giveaway(self):
        media = _fake_media(
            "MessageMediaGiveaway",
            quantity=5,
            months=3,
            until_date=datetime(2026, 9, 1),
            channels=[1, 2],
        )
        kind, details = extract_extended_media_details(media)
        self.assertEqual(kind, "giveaway")
        self.assertEqual(details["quantity"], 5)
        self.assertEqual(details["months"], 3)
        self.assertEqual(details["until_date"], "2026-09-01T00:00:00")
        self.assertEqual(details["channel_count"], 2)

    def test_giveaway_results(self):
        media = _fake_media("MessageMediaGiveawayResults", winners_count=4, months=6)
        self.assertEqual(
            extract_extended_media_details(media),
            ("giveaway_results", {"winners_count": 4, "months": 6}),
        )

    def test_geo_live(self):
        media = _fake_media("MessageMediaGeoLive", geo=SimpleNamespace(lat=40.0, long=-3.0), period=900)
        self.assertEqual(
            extract_extended_media_details(media),
            ("geo_live", {"lat": 40.0, "long": -3.0, "period": 900}),
        )

    def test_game(self):
        media = _fake_media(
            "MessageMediaGame",
            game=SimpleNamespace(title="Chess", short_name="chess", description="Play"),
        )
        self.assertEqual(
            extract_extended_media_details(media),
            ("game", {"title": "Chess", "short_name": "chess", "description": "Play"}),
        )

    def test_unsupported_is_kind_with_empty_payload(self):
        media = _fake_media("MessageMediaUnsupported")
        self.assertEqual(extract_extended_media_details(media), ("unsupported", {}))

    def test_missing_attributes_degrade_to_bare_kind(self):
        """A Telethon layer change must degrade the chip, never fail capture."""
        kind, details = extract_extended_media_details(_fake_media("MessageMediaVenue"))
        self.assertEqual(kind, "venue")
        self.assertEqual(details, {})

    def test_non_scalar_values_are_filtered(self):
        media = _fake_media("MessageMediaDice", emoticon=["not", "a", "string"], value=3)
        kind, details = extract_extended_media_details(media)
        self.assertEqual(details, {"value": 3})

    def test_unrelated_media_returns_none(self):
        self.assertIsNone(extract_extended_media_details(MagicMock()))
        self.assertIsNone(extract_extended_media_details(None))


class TestMediaTypeTail(unittest.TestCase):
    """Both _get_media_type ladders fall through to the classifier."""

    def setUp(self):
        self.backup = TelegramBackup.__new__(TelegramBackup)
        self.listener = TelegramListener.__new__(TelegramListener)

    def test_backup_returns_extended_kind(self):
        media = _fake_media("MessageMediaDice", emoticon="🎲", value=2)
        self.assertEqual(self.backup._get_media_type(media), "dice")

    def test_listener_returns_extended_kind(self):
        media = _fake_media("MessageMediaVenue", title="Cafe")
        self.assertEqual(self.listener._get_media_type(media), "venue")

    def test_magicmock_still_falls_out_as_none_on_both(self):
        mock_media = MagicMock()
        mock_media.photo = None
        mock_media.document = None
        mock_media.webpage = None
        self.assertIsNone(self.backup._get_media_type(mock_media))
        self.assertIsNone(self.listener._get_media_type(mock_media))


class TestMetadataOnlyGates(unittest.TestCase):
    """Extended kinds produce metadata rows, never download attempts."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_backup_process_media_writes_metadata_row_for_dice(self):
        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.db = AsyncMock()
        backup.config = MagicMock()
        backup.client = AsyncMock()

        message = MagicMock()
        message.id = 11
        message.media = _fake_media("MessageMediaDice", emoticon="🎲", value=4)

        result = self._run(backup._process_media(message, CHAT_ID))
        self.assertEqual(result["type"], "dice")
        self.assertEqual(result["file_size"], 0)
        backup.client.download_media.assert_not_awaited()

    def test_listener_download_media_skips_extended_kinds(self):
        # Fully wired (media_path, size cap, dedup off): without the gate this
        # fixture genuinely reaches the download call, so the assertion cannot
        # pass vacuously on an AttributeError swallowed by the except.
        listener = TelegramListener.__new__(TelegramListener)
        listener.client = AsyncMock()
        listener.db = AsyncMock()
        listener.account_id = 1
        listener.config = MagicMock()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        listener.config.media_path = tmp
        listener.config.get_max_media_size_bytes = MagicMock(return_value=100 * 1024 * 1024)
        listener.config.deduplicate_media = False

        message = MagicMock()
        message.id = 12
        message.media = _fake_media("MessageMediaGiveaway", quantity=1, months=1)

        result = self._run(listener._download_media(message, CHAT_ID))
        self.assertIsNone(result)
        listener.client.download_media.assert_not_awaited()


class TestProcessMessageCapturesExtendedPayload(unittest.TestCase):
    """The sweep stores raw_data[<kind>] so the viewer chip has fields."""

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

    def _make_message(self, msg_id, media):
        msg = MagicMock()
        msg.id = msg_id
        msg.sender = None
        msg.sender_id = 42
        msg.date = datetime(2026, 1, 1)
        msg.text = ""
        msg.reply_to_msg_id = None
        msg.reply_to = None
        msg.edit_date = None
        msg.out = False
        msg.pinned = False
        msg.grouped_id = None
        msg.fwd_from = None
        msg.media = media
        msg.reactions = None
        msg.post_author = None
        msg.action = None
        return msg

    def test_dice_payload_lands_in_raw_data(self):
        msg = self._make_message(21, _fake_media("MessageMediaDice", emoticon="🎯", value=5))
        result = self._run(self.backup._process_message(msg, CHAT_ID))
        self.assertEqual(result["raw_data"]["dice"], {"emoticon": "🎯", "value": 5})

    def test_plain_text_message_stores_no_extended_key(self):
        msg = self._make_message(22, None)
        result = self._run(self.backup._process_message(msg, CHAT_ID))
        for kind in _EXTENDED_MEDIA_TYPES.values():
            self.assertNotIn(kind, result["raw_data"])


# ---------------------------------------------------------------------------
# Drain + status-count exclusion (real in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def adapter():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with db_manager.async_session_factory() as session:
        session.add(Chat(id=CHAT_ID, type="channel", title="Chat A"))
        session.add(Message(id=1, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 1, 1), text=""))
        session.add(Message(id=2, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 1, 1), text=""))
        session.add(Message(id=3, chat_id=CHAT_ID, sender_id=None, date=datetime(2026, 1, 1), text=""))
        # A real pending download, an undownloaded dice, and an exhausted venue.
        session.add(
            Media(
                id=f"{CHAT_ID}_1_photo",
                message_id=1,
                chat_id=CHAT_ID,
                type="photo",
                file_size=1000,
                downloaded=0,
                download_attempts=0,
                account_id=1,
            )
        )
        session.add(
            Media(
                id=f"{CHAT_ID}_2_dice",
                message_id=2,
                chat_id=CHAT_ID,
                type="dice",
                file_size=0,
                downloaded=0,
                download_attempts=0,
                account_id=1,
            )
        )
        session.add(
            Media(
                id=f"{CHAT_ID}_3_venue",
                message_id=3,
                chat_id=CHAT_ID,
                type="venue",
                file_size=0,
                downloaded=0,
                download_attempts=99,
                account_id=1,
            )
        )
        await session.commit()

    return DatabaseAdapter(db_manager)


@pytest.mark.asyncio
async def test_drain_excludes_extended_metadata_rows(adapter):
    """get_pending_media_downloads must never hand a dice/venue row to the
    downloader — there is no file behind it, so it would retry forever."""
    pending = await adapter.get_pending_media_downloads(100 * 1024 * 1024, 5, account_id=1)
    assert [row["id"] for row in pending] == [f"{CHAT_ID}_1_photo"]


@pytest.mark.asyncio
async def test_status_counts_exclude_metadata_only_rows(adapter):
    """Operator status: dice (attempts=0) is not pending, venue (attempts=99)
    is not exhausted — both sit at downloaded=0 by design."""
    counts = await adapter.get_operator_status_counts(max_attempts=5)
    assert counts["pending"] == 1  # just the photo
    assert counts["exhausted"] == 0
