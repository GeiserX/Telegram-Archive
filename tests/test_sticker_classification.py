"""Stickers are classified by DocumentAttributeSticker, wherever it sits.

Telegram sends a video sticker as ``[DocumentAttributeVideo,
DocumentAttributeFilename('sticker.webm'), DocumentAttributeSticker]``. The
classifier walked the attributes in order and returned at the first Video
attribute, so every video sticker was archived as a ``video``. Every official
client decides "sticker" by the attribute in any position (Telethon's own
``Message.sticker`` does the same).

The old branch also matched on the substring ``"Sticker"``, which caught
``DocumentAttributeHasStickers``: a video, GIF or picture that has stickers
drawn on it. Those are not stickers.

Real Telethon attribute objects, not mocks: a bare MagicMock answers truthy to
every getattr and its type name is "MagicMock", so it proves nothing here.
"""

import pytest
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeHasStickers,
    DocumentAttributeImageSize,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputStickerSetEmpty,
    MessageMediaDocument,
)

from telegram_archive.message_utils import classify_media_type


def _document(*attributes):
    return MessageMediaDocument(document=type("Doc", (), {"attributes": list(attributes)})())


def _sticker_attr():
    return DocumentAttributeSticker(alt="x", stickerset=InputStickerSetEmpty())


VIDEO_STICKER = (
    DocumentAttributeVideo(duration=3, w=512, h=512, nosound=True),
    DocumentAttributeFilename(file_name="sticker.webm"),
    _sticker_attr(),
)
ANIMATED_STICKER = (
    DocumentAttributeImageSize(w=512, h=512),
    _sticker_attr(),
    DocumentAttributeFilename(file_name="AnimatedSticker.tgs"),
)


class TestStickerClassification:
    def test_a_video_sticker_with_the_video_attribute_first_is_a_sticker(self):
        """The order Telegram actually sends. Before the fix: "video"."""
        assert classify_media_type(_document(*VIDEO_STICKER)) == "sticker"

    def test_control_the_sticker_attribute_first_is_a_sticker(self):
        assert classify_media_type(_document(*reversed(VIDEO_STICKER))) == "sticker"

    def test_an_animated_tgs_sticker_is_a_sticker(self):
        assert classify_media_type(_document(*ANIMATED_STICKER)) == "sticker"

    def test_control_the_same_video_without_the_sticker_attribute_is_a_video(self):
        assert classify_media_type(_document(*VIDEO_STICKER[:2])) == "video"

    def test_a_video_with_stickers_drawn_on_it_is_a_video(self):
        """Before the fix: "sticker"."""
        media = _document(DocumentAttributeHasStickers(), DocumentAttributeVideo(duration=9, w=640, h=360))
        assert classify_media_type(media) == "video"

    def test_a_gif_with_stickers_drawn_on_it_is_an_animation(self):
        """Before the fix: "sticker"."""
        media = _document(
            DocumentAttributeHasStickers(),
            DocumentAttributeAnimated(),
            DocumentAttributeVideo(duration=2, w=320, h=240),
        )
        assert classify_media_type(media) == "animation"

    def test_a_picture_file_with_stickers_drawn_on_it_is_a_document(self):
        """Before the fix: "sticker"."""
        media = _document(DocumentAttributeFilename(file_name="a.png"), DocumentAttributeHasStickers())
        assert classify_media_type(media) == "document"


class TestBothCaptureLanesAgreeOnStickers:
    @pytest.mark.parametrize(
        "attributes,expected",
        [
            (VIDEO_STICKER, "sticker"),
            (ANIMATED_STICKER, "sticker"),
            ((DocumentAttributeHasStickers(), DocumentAttributeVideo(duration=9, w=640, h=360)), "video"),
        ],
    )
    def test_the_sweep_and_the_listener_return_the_same_type(self, attributes, expected):
        from telegram_archive.listener import TelegramListener
        from telegram_archive.telegram_backup import TelegramBackup

        media = _document(*attributes)
        sweep = TelegramBackup.__new__(TelegramBackup)
        listener = TelegramListener.__new__(TelegramListener)

        assert sweep._get_media_type(media) == expected
        assert listener._get_media_type(media) == expected
