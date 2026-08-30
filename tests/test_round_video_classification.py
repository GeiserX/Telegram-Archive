"""Round videos ("video notes") are classified as video_note by both capture lanes.

Telegram's circular video messages are ordinary documents carrying a
``DocumentAttributeVideo`` whose ``round_message`` flag is set. Neither the
scheduled sweep nor the realtime listener ever looked at that flag, so every
round video was archived as a plain ``video`` — while the Telegram Desktop
importer has always written ``video_note`` (src/telegram_import.py). The same
message therefore got a different type depending on which lane captured it.

The ladder lived twice, byte-identically, in src/telegram_backup.py and
src/listener.py, which is how it stayed unimplemented in both at once. It now
lives once in src/message_utils.py next to extract_media_attributes, which was
made the single extractor for the same reason after the same class of bug.
"""

import os
import sys

import pytest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.message_utils import classify_media_type


def _document(*attributes):
    """A MessageMediaDocument carrying real Telethon attribute objects.

    Real objects, not mocks, on purpose: ``getattr`` on a bare ``MagicMock``
    answers truthy for EVERY name, so a mocked video attribute that never sets
    ``round_message`` reads as a round video and the test proves nothing.
    """
    doc = MessageMediaDocument(
        document=type("Doc", (), {"attributes": list(attributes)})(),
    )
    return doc


class TestRoundVideoClassification:
    def test_a_round_message_is_a_video_note(self):
        media = _document(DocumentAttributeVideo(duration=7, w=384, h=384, round_message=True))
        assert classify_media_type(media) == "video_note"

    def test_control_an_ordinary_video_is_still_a_video(self):
        """The control that gives the test above its meaning: same attribute,
        same square dimensions, flag off."""
        media = _document(DocumentAttributeVideo(duration=7, w=384, h=384, round_message=False))
        assert classify_media_type(media) == "video"

    def test_control_an_unset_flag_is_not_a_round_video(self):
        """Telethon leaves the flag None when absent, which must read as false."""
        media = _document(DocumentAttributeVideo(duration=12, w=640, h=360))
        assert media.document.attributes[0].round_message in (None, False)
        assert classify_media_type(media) == "video"

    def test_a_round_video_is_never_mistaken_for_a_gif(self):
        """An animated round video is a contradiction Telegram does not produce,
        but the animation branch sits directly above and must not swallow it."""
        animated = type("DocumentAttributeAnimated", (), {})()
        media = _document(animated, DocumentAttributeVideo(duration=3, w=384, h=384, round_message=True))
        assert classify_media_type(media) == "video_note"

    def test_the_voice_split_still_works(self):
        """The round split is modelled on this one; changing either must not
        disturb the other."""
        assert classify_media_type(_document(DocumentAttributeAudio(duration=5, voice=True))) == "voice"
        assert classify_media_type(_document(DocumentAttributeAudio(duration=5, voice=False))) == "audio"

    def test_an_under_specified_mock_is_not_a_round_video(self):
        """A bare MagicMock answers truthy to EVERY getattr, so a fixture that
        never mentions round_message would silently classify as a round video.
        The check is ``is True`` for exactly this reason: Telethon's parser sets
        a real bool (``bool(flags & 1)``), so nothing on the wire is affected."""
        from unittest.mock import MagicMock

        attr = MagicMock()
        type(attr).__name__ = "DocumentAttributeVideo"
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        media.document.attributes = [attr]

        assert classify_media_type(media) == "video"

    def test_a_photo_is_still_a_photo(self):
        """Dispatch moved from isinstance to __class__.__name__ so the classifier
        could live in a module the telethon-free viewer image imports. This is
        the control that the move preserved ordinary classification."""
        assert classify_media_type(MessageMediaPhoto(photo=None)) == "photo"


class TestBothCaptureLanesAgree:
    """The bug was one ladder in two files. There is now one, and these two
    tests are what fail if anyone forks it again."""

    def test_the_sweep_and_the_listener_share_one_classifier(self):
        import src.listener as listener_mod
        import src.telegram_backup as backup_mod

        assert backup_mod.classify_media_type is listener_mod.classify_media_type
        assert backup_mod.classify_media_type is classify_media_type

    @pytest.mark.parametrize(
        "attributes,expected",
        [
            ((DocumentAttributeVideo(duration=7, w=384, h=384, round_message=True),), "video_note"),
            ((DocumentAttributeVideo(duration=7, w=640, h=360),), "video"),
            ((DocumentAttributeAudio(duration=5, voice=True),), "voice"),
        ],
    )
    def test_both_lanes_return_the_same_type(self, attributes, expected):
        from src.listener import TelegramListener
        from src.telegram_backup import TelegramBackup

        media = _document(*attributes)
        sweep = TelegramBackup.__new__(TelegramBackup)
        listener = TelegramListener.__new__(TelegramListener)

        assert sweep._get_media_type(media) == expected
        assert listener._get_media_type(media) == expected


# ============================================================ the viewer half
# Classification without rendering is a REGRESSION, not a partial win: a round
# video reclassified from `video` to `video_note` stops matching the bubble's
# video branch and falls through to the generic file-download row. These pin the
# branch that keeps that from happening.

import pathlib  # noqa: E402
import re  # noqa: E402

INDEX_HTML = pathlib.Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "index.html"


class TestViewerRendersRoundVideos:
    @pytest.fixture(scope="class")
    def html(self) -> str:
        return INDEX_HTML.read_text(encoding="utf-8")

    def test_the_sound_toggle_is_reachable_without_a_mouse(self, html: str) -> None:
        """A bare <video> with no native controls has no focus target and no key
        handler, so the toggle was mouse-only."""
        block = html[html.index('class="round-video gif-video cursor-pointer"') :][:700]
        assert 'tabindex="0"' in block
        assert ':aria-label="roundVideoHint"' in block
        assert "@keydown.enter.prevent" in block
        assert "@keydown.space.prevent" in block

    def test_the_bubble_has_a_branch_for_video_note(self, html):
        assert "msg.media?.type === 'video_note'" in html
        assert 'class="round-video gif-video cursor-pointer"' in html

    def test_the_branch_precedes_the_generic_file_fallback(self, html):
        """v-else-if order decides everything here: a branch after the generic
        `<a v-else>` row would never be reached."""
        branch = html.index("msg.media?.type === 'video_note'")
        fallback = html.index("<!-- Other documents / files -->")
        assert branch < fallback

    def test_it_opts_into_the_existing_autoplay_observer(self, html):
        """`gif-video` is the selector setupGifObserver watches, so visibility
        playback needs no new code — and if that class is dropped, this fails."""
        assert "gif-video" in html[html.index("msg.media?.type === 'video_note'") :][:1400]
        assert "document.querySelectorAll('.gif-video')" in html

    def test_the_circle_outranks_the_bubble_video_rule(self, html):
        """`.message-bubble video` is (0,1,1) and would stretch the circle to the
        bubble's width. The round rule must be scoped to the bubble too, at
        (0,2,1) — measured in a real browser as 240x240 against 520x150 for a
        plain video in the same bubble."""
        assert ".message-bubble .round-video {" in html
        assert ".round-video {" not in html.replace(".message-bubble .round-video {", "")

    def test_both_axes_ride_one_custom_property(self, html):
        """Capping only the width leaves height:auto in play and the circle
        becomes an ellipse on narrow screens."""
        block = html[html.index(".message-bubble .round-video {") :][:420]
        assert "--round-video-size: min(240px, 60vw);" in block
        assert "width: var(--round-video-size);" in block
        assert "height: var(--round-video-size);" in block
        assert "border-radius: 50%;" in block

    def test_the_gallery_badge_counts_what_the_gallery_fetches(self, html):
        """These two lists must agree or the tab badge disagrees with its own
        grid. #424 added video_note to the filter and not to the count."""
        types = re.search(r"photos: '([^']+)'", html).group(1).split(",")
        counts = re.search(r"photos: \((.*?)\),", html).group(1)
        for t in types:
            assert f"data.{t}" in counts, f"{t} is fetched but never counted"

    def test_a_round_video_is_not_a_dead_gallery_tile(self, html):
        """openMediaItem had no branch for it, so the tile was a silent no-op.
        It jumps to the message, like the voice tab's tiles do."""
        block = html[html.index("const openMediaItem = (item) =>") :][:900]
        assert "item.type === 'video_note'" in block
        assert "jumpToMessage(item)" in block

    def test_a_round_video_never_reaches_the_lightbox(self, html):
        """The overlay is `<img v-if>` / `<video v-else-if>` with NO `v-else`, so
        routing a type it cannot render into it produces a black modal with
        chrome and no media. Round videos have no fullscreen form in any official
        client, so both the predicate and the navigation list exclude them —
        and the gallery entry point above sends them elsewhere entirely."""
        predicate = html[html.index("const isLightboxVideo = (msg) => {") :][:300]
        assert "video_note" not in predicate
        nav = html[html.index("const mediaMessages = computed(") :][:500]
        assert "video_note" not in nav

    def test_a_broken_round_video_says_so(self, html):
        """Without a failure arm a 404 renders a blank circle, which reads as
        data loss rather than a broken link. The rescan can cause exactly that
        for a tab left open across it."""
        block = html[html.index("msg.media?.type === 'video_note'") :][:1400]
        assert 'v-if="msg.mediaLoadFailed"' in block
        assert "Media not found" in block

    def test_the_reply_quote_has_a_human_label(self, html):
        """Without an entry the raw column value 'video_note' is shown to users."""
        labels = html[html.index("const replyMediaLabels = {") :][:400]
        assert "video_note: 'Video message'," in labels
