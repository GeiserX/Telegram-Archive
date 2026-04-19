"""
Extended tests for the real-time listener module.

Covers lines missing from the initial test_listener.py:
- MassOperationProtector advanced flows (start/stop, expiry, stats, blocked chats)
- _should_process_chat with various include lists
- _get_chat_type for all entity types
- _get_media_type for all media variants
- _get_media_filename edge cases
- _download_media edge cases (skip types, size limit, dedup, no-dedup)
- _download_avatar paths
- _notify_update paths
- on_chat_action handler (all action types)
- on_pinned_messages handler (channel and group variants)
- on_new_message with sender/media/grouped_id paths
- on_message_deleted rate limiting on resolved chat path
- run() and stop() lifecycle
- _log_stats paths
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import events
from telethon.tl.types import (
    UpdatePinnedChannelMessages,
    UpdatePinnedMessages,
)

from src.listener import MassOperationProtector, TelegramListener

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    """Build a fully-populated mock Config with sensible defaults."""
    config = MagicMock()
    config.api_id = 12345
    config.api_hash = "test_hash"
    config.phone = "+1234567890"
    config.session_path = "/tmp/test_session"
    config.media_path = "/tmp/test_media"
    config.global_include_ids = set()
    config.private_include_ids = set()
    config.groups_include_ids = set()
    config.channels_include_ids = set()
    config.validate_credentials = MagicMock()
    config.whitelist_mode = False
    config.chat_ids = set()
    config.listen_edits = True
    config.listen_deletions = True
    config.listen_new_messages = True
    config.listen_new_messages_media = True
    config.listen_chat_actions = True
    config.skip_topic_ids = {}
    config.should_skip_topic = MagicMock(return_value=False)
    config.mass_operation_threshold = 100
    config.mass_operation_window_seconds = 30
    config.mass_operation_buffer_delay = 2.0
    config.should_download_media_for_chat = MagicMock(return_value=True)
    config.get_max_media_size_bytes = MagicMock(return_value=50 * 1024 * 1024)
    config.deduplicate_media = True
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _make_db():
    """Build a fully-populated mock DatabaseAdapter."""
    db = AsyncMock()
    db.get_all_chats = AsyncMock(return_value=[])
    db.update_message_text = AsyncMock()
    db.delete_message = AsyncMock()
    db.resolve_message_chat_id = AsyncMock(return_value=None)
    db.upsert_chat = AsyncMock()
    db.upsert_user = AsyncMock()
    db.insert_message = AsyncMock()
    db.insert_media = AsyncMock()
    db.set_metadata = AsyncMock()
    db.update_message_pinned = AsyncMock()
    db.close = AsyncMock()
    return db


def _make_listener_with_handlers(**config_overrides):
    """Create a TelegramListener with handlers captured for direct invocation."""
    config = _make_config(**config_overrides)
    db = _make_db()
    listener = TelegramListener(config, db)
    listener._tracked_chat_ids = {-1001234567890}
    listener._notifier = None

    handlers = {}
    mock_client = MagicMock()

    def capture_on(event_type):
        def decorator(fn):
            handlers[event_type] = fn
            return fn

        return decorator

    mock_client.on = capture_on
    listener.client = mock_client
    listener._register_handlers()

    return listener, handlers, db, config


# ===========================================================================
# MassOperationProtector -- advanced flows
# ===========================================================================


class TestMassOperationProtectorAdvanced:
    """Tests for MassOperationProtector edge cases not covered by test_listener.py."""

    def test_start_sets_running_flag(self):
        """start() sets _running to True."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        assert protector._running is False
        protector.start()
        assert protector._running is True

    async def test_stop_clears_running_flag(self):
        """stop() sets _running to False."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        protector.start()
        await protector.stop()
        assert protector._running is False

    def test_is_blocked_returns_false_when_not_blocked(self):
        """is_blocked returns (False, '') for a chat that was never rate-limited."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        blocked, reason = protector.is_blocked(12345)
        assert blocked is False
        assert reason == ""

    def test_is_blocked_returns_true_while_block_active(self):
        """is_blocked returns True for a chat with an active block."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        future = datetime.now() + timedelta(hours=1)
        protector._blocked[12345] = (future, "test reason", 5)

        blocked, reason = protector.is_blocked(12345)
        assert blocked is True
        assert "test reason" in reason

    def test_is_blocked_expires_old_block(self):
        """is_blocked removes expired blocks and returns False."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        past = datetime.now() - timedelta(seconds=1)
        protector._blocked[12345] = (past, "old reason", 3)

        blocked, reason = protector.is_blocked(12345)
        assert blocked is False
        assert reason == ""
        assert 12345 not in protector._blocked

    def test_count_ops_in_window_returns_zero_for_unknown_chat(self):
        """_count_ops_in_window returns 0 for a chat with no recorded operations."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        assert protector._count_ops_in_window(99999) == 0

    def test_count_ops_in_window_prunes_old_entries(self):
        """_count_ops_in_window drops entries older than the window."""
        from collections import deque

        protector = MassOperationProtector(threshold=5, window_seconds=10)
        old_ts = datetime.now() - timedelta(seconds=20)
        recent_ts = datetime.now()
        protector._operation_history[100] = deque([old_ts, recent_ts])

        count = protector._count_ops_in_window(100)
        assert count == 1
        assert len(protector._operation_history[100]) == 1

    def test_get_blocked_chats_filters_expired(self):
        """get_blocked_chats only returns currently-active blocks."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        future = datetime.now() + timedelta(hours=1)
        past = datetime.now() - timedelta(seconds=1)
        protector._blocked[100] = (future, "active", 5)
        protector._blocked[200] = (past, "expired", 3)

        blocked = protector.get_blocked_chats()
        assert 100 in blocked
        assert 200 not in blocked

    def test_get_stats_includes_currently_blocked_count(self):
        """get_stats counts chats that are currently blocked."""
        protector = MassOperationProtector(threshold=5, window_seconds=30)
        future = datetime.now() + timedelta(hours=1)
        protector._blocked[100] = (future, "active", 5)

        stats = protector.get_stats()
        assert stats["currently_blocked"] == 1

    def test_check_operation_records_and_counts(self):
        """check_operation records timestamps and uses sliding window correctly."""
        protector = MassOperationProtector(threshold=2, window_seconds=60)

        allowed1, _ = protector.check_operation(100, "edit")
        allowed2, _ = protector.check_operation(100, "edit")
        assert allowed1 is True
        assert allowed2 is True

        # Third exceeds threshold (3 > 2)
        allowed3, reason3 = protector.check_operation(100, "edit")
        assert allowed3 is False
        assert "Rate limit" in reason3
        assert protector.stats["rate_limits_triggered"] == 1
        assert 100 in protector.stats["chats_rate_limited"]

    def test_check_operation_blocked_chat_increments_blocked_stats(self):
        """Subsequent operations on an already-blocked chat increment operations_blocked."""
        protector = MassOperationProtector(threshold=1, window_seconds=60)
        protector.check_operation(100, "deletion")  # allowed
        protector.check_operation(100, "deletion")  # triggers block

        before = protector.stats["operations_blocked"]
        protector.check_operation(100, "deletion")  # already blocked
        assert protector.stats["operations_blocked"] == before + 1


# ===========================================================================
# _should_process_chat -- include-list variants
# ===========================================================================


class TestShouldProcessChatIncludeLists:
    """Tests for _should_process_chat with private/groups/channels include IDs."""

    def test_private_include_ids_allows_chat(self):
        """Chat in private_include_ids is processed even if not tracked."""
        config = _make_config(private_include_ids={555})
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._tracked_chat_ids = set()
        assert listener._should_process_chat(555) is True

    def test_groups_include_ids_allows_chat(self):
        """Chat in groups_include_ids is processed even if not tracked."""
        config = _make_config(groups_include_ids={-666})
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._tracked_chat_ids = set()
        assert listener._should_process_chat(-666) is True

    def test_channels_include_ids_allows_chat(self):
        """Chat in channels_include_ids is processed even if not tracked."""
        config = _make_config(channels_include_ids={-1007777})
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._tracked_chat_ids = set()
        assert listener._should_process_chat(-1007777) is True

    def test_returns_false_when_in_no_list(self):
        """Chat not in any list or tracking set returns False."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._tracked_chat_ids = set()
        assert listener._should_process_chat(99999) is False


# ===========================================================================
# _get_chat_type
# ===========================================================================


class TestGetChatType:
    """Tests for _get_chat_type entity classification."""

    def test_user_entity_returns_private(self):
        """User entity maps to 'private'."""
        from telethon.tl.types import User

        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        entity = MagicMock(spec=User)
        assert listener._get_chat_type(entity) == "private"

    def test_chat_entity_returns_group(self):
        """Chat entity maps to 'group'."""
        from telethon.tl.types import Chat as TelethonChat

        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        entity = MagicMock(spec=TelethonChat)
        assert listener._get_chat_type(entity) == "group"

    def test_channel_broadcast_returns_channel(self):
        """Channel with megagroup=False maps to 'channel'."""
        from telethon.tl.types import Channel

        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        entity = MagicMock(spec=Channel)
        entity.megagroup = False
        assert listener._get_chat_type(entity) == "channel"

    def test_channel_megagroup_returns_group(self):
        """Channel with megagroup=True maps to 'group'."""
        from telethon.tl.types import Channel

        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        entity = MagicMock(spec=Channel)
        entity.megagroup = True
        assert listener._get_chat_type(entity) == "group"

    def test_unknown_entity_returns_unknown(self):
        """Unknown entity type maps to 'unknown'."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        entity = MagicMock()  # Not spec'd to any known type
        assert listener._get_chat_type(entity) == "unknown"


# ===========================================================================
# _get_media_type
# ===========================================================================


class TestGetMediaType:
    """Tests for _get_media_type classification."""

    def _listener(self):
        return TelegramListener(_make_config(), _make_db())

    def test_photo_media(self):
        """MessageMediaPhoto returns 'photo'."""
        from telethon.tl.types import MessageMediaPhoto

        listener = self._listener()
        media = MagicMock(spec=MessageMediaPhoto)
        assert listener._get_media_type(media) == "photo"

    def test_document_media_plain(self):
        """MessageMediaDocument with no special attrs returns 'document'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        media.document.attributes = []
        assert listener._get_media_type(media) == "document"

    def test_document_media_video(self):
        """Document with Video attribute returns 'video'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        attr = MagicMock()
        type(attr).__name__ = "DocumentAttributeVideo"
        media.document = MagicMock()
        media.document.attributes = [attr]
        assert listener._get_media_type(media) == "video"

    def test_document_media_animation(self):
        """Document with Animated + Video attribute returns 'animation'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        anim_attr = MagicMock()
        type(anim_attr).__name__ = "DocumentAttributeAnimated"
        video_attr = MagicMock()
        type(video_attr).__name__ = "DocumentAttributeVideo"
        media.document = MagicMock()
        media.document.attributes = [anim_attr, video_attr]
        assert listener._get_media_type(media) == "animation"

    def test_document_media_animated_no_video(self):
        """Document with only Animated attribute (no Video) returns 'animation'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        anim_attr = MagicMock()
        type(anim_attr).__name__ = "DocumentAttributeAnimated"
        media.document = MagicMock()
        media.document.attributes = [anim_attr]
        assert listener._get_media_type(media) == "animation"

    def test_document_media_voice(self):
        """Document with Audio attribute and voice=True returns 'voice'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        attr = MagicMock()
        type(attr).__name__ = "DocumentAttributeAudio"
        attr.voice = True
        media.document = MagicMock()
        media.document.attributes = [attr]
        assert listener._get_media_type(media) == "voice"

    def test_document_media_audio(self):
        """Document with Audio attribute and voice=False returns 'audio'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        attr = MagicMock()
        type(attr).__name__ = "DocumentAttributeAudio"
        attr.voice = False
        media.document = MagicMock()
        media.document.attributes = [attr]
        assert listener._get_media_type(media) == "audio"

    def test_document_media_sticker(self):
        """Document with Sticker attribute returns 'sticker'."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        attr = MagicMock()
        type(attr).__name__ = "DocumentAttributeSticker"
        media.document = MagicMock()
        media.document.attributes = [attr]
        assert listener._get_media_type(media) == "sticker"

    def test_document_media_no_document_body(self):
        """MessageMediaDocument with document=None returns None (inaccessible)."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._listener()
        media = MagicMock(spec=MessageMediaDocument)
        media.document = None
        assert listener._get_media_type(media) is None

    def test_contact_media(self):
        """MessageMediaContact returns 'contact'."""
        from telethon.tl.types import MessageMediaContact

        listener = self._listener()
        media = MagicMock(spec=MessageMediaContact)
        assert listener._get_media_type(media) == "contact"

    def test_geo_media(self):
        """MessageMediaGeo returns 'geo'."""
        from telethon.tl.types import MessageMediaGeo

        listener = self._listener()
        media = MagicMock(spec=MessageMediaGeo)
        assert listener._get_media_type(media) == "geo"

    def test_poll_media(self):
        """MessageMediaPoll returns 'poll'."""
        from telethon.tl.types import MessageMediaPoll

        listener = self._listener()
        media = MagicMock(spec=MessageMediaPoll)
        assert listener._get_media_type(media) == "poll"

    def test_unknown_media_returns_none(self):
        """Unknown media type returns None."""
        listener = self._listener()
        media = MagicMock()  # Not spec'd to any known type
        assert listener._get_media_type(media) is None


# ===========================================================================
# _get_media_filename
# ===========================================================================


class TestGetMediaFilename:
    """Tests for _get_media_filename generation."""

    def _listener(self):
        return TelegramListener(_make_config(), _make_db())

    def test_uses_original_filename_with_file_id(self):
        """When document has file_name attribute and telegram_file_id is provided, combines both."""
        listener = self._listener()
        msg = MagicMock()
        attr = MagicMock()
        attr.file_name = "report.pdf"
        msg.media.document = MagicMock()
        msg.media.document.attributes = [attr]

        result = listener._get_media_filename(msg, "document", telegram_file_id="abc123")
        assert result == "abc123_report.pdf"

    def test_uses_original_filename_without_file_id(self):
        """When document has file_name but no telegram_file_id, returns just the filename."""
        listener = self._listener()
        msg = MagicMock()
        attr = MagicMock()
        attr.file_name = "report.pdf"
        msg.media.document = MagicMock()
        msg.media.document.attributes = [attr]

        result = listener._get_media_filename(msg, "document", telegram_file_id=None)
        assert result == "report.pdf"

    def test_generates_filename_with_file_id(self):
        """When no original filename, generates from file_id and extension."""
        listener = self._listener()
        msg = MagicMock()
        msg.media.document = MagicMock()
        # Attrs without file_name
        attr = MagicMock(spec=[])
        msg.media.document.attributes = [attr]

        result = listener._get_media_filename(msg, "photo", telegram_file_id="xyz789")
        assert result == "xyz789.jpg"

    def test_generates_filename_without_file_id(self):
        """When no original filename and no file_id, uses message id."""
        listener = self._listener()
        msg = MagicMock()
        msg.id = 42
        msg.media.document = MagicMock()
        attr = MagicMock(spec=[])
        msg.media.document.attributes = [attr]

        result = listener._get_media_filename(msg, "video", telegram_file_id=None)
        assert result == "42_video.mp4"

    def test_extension_mapping_for_all_types(self):
        """Verify extension mapping for each known media type."""
        listener = self._listener()
        msg = MagicMock()
        msg.id = 1
        # No document with file_name
        msg.media = MagicMock(spec=[])

        expected = {
            "photo": ".jpg",
            "video": ".mp4",
            "animation": ".mp4",
            "voice": ".ogg",
            "audio": ".mp3",
            "sticker": ".webp",
            "document": "",
        }
        for media_type, ext in expected.items():
            result = listener._get_media_filename(msg, media_type, telegram_file_id="id1")
            assert result == f"id1{ext}", f"Failed for {media_type}"


# ===========================================================================
# _download_media
# ===========================================================================


class TestDownloadMedia:
    """Tests for _download_media edge cases."""

    def _make_listener(self, **config_overrides):
        config = _make_config(**config_overrides)
        db = _make_db()
        listener = TelegramListener(config, db)
        listener.client = AsyncMock()
        return listener

    async def test_returns_none_for_contact_media(self):
        """Contact media has no downloadable file."""
        from telethon.tl.types import MessageMediaContact

        listener = self._make_listener()
        msg = MagicMock()
        msg.media = MagicMock(spec=MessageMediaContact)
        result = await listener._download_media(msg, -100)
        assert result is None

    async def test_returns_none_for_geo_media(self):
        """Geo media has no downloadable file."""
        from telethon.tl.types import MessageMediaGeo

        listener = self._make_listener()
        msg = MagicMock()
        msg.media = MagicMock(spec=MessageMediaGeo)
        result = await listener._download_media(msg, -100)
        assert result is None

    async def test_returns_none_for_poll_media(self):
        """Poll media has no downloadable file."""
        from telethon.tl.types import MessageMediaPoll

        listener = self._make_listener()
        msg = MagicMock()
        msg.media = MagicMock(spec=MessageMediaPoll)
        result = await listener._download_media(msg, -100)
        assert result is None

    async def test_skips_file_exceeding_max_size(self):
        """Files larger than max size are skipped."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._make_listener()
        listener.config.get_max_media_size_bytes.return_value = 1000
        msg = MagicMock()
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        media.document.size = 5000  # larger than 1000
        media.document.attributes = []
        media.document.id = 123
        msg.media = media
        msg.id = 1

        result = await listener._download_media(msg, -100)
        assert result is None

    async def test_returns_none_for_unknown_media_type(self):
        """Unknown media (returns None from _get_media_type) is skipped."""
        listener = self._make_listener()
        msg = MagicMock()
        msg.media = MagicMock()  # Not spec'd to any known type
        result = await listener._download_media(msg, -100)
        assert result is None

    @patch("os.path.exists", return_value=False)
    @patch("os.makedirs")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink")
    @patch("os.path.relpath", return_value="../_shared/file.jpg")
    async def test_download_with_dedup_creates_symlink(
        self, mock_relpath, mock_symlink, mock_lexists, mock_makedirs, mock_exists
    ):
        """When dedup is enabled and file not cached, downloads to shared and creates symlink."""
        from telethon.tl.types import MessageMediaPhoto

        listener = self._make_listener(deduplicate_media=True)
        listener.client.download_media = AsyncMock(return_value="/tmp/test_media/_shared/123.jpg")

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 123
        media.photo.sizes = []
        msg.media = media
        msg.id = 1

        result = await listener._download_media(msg, -100)
        assert result is not None
        listener.client.download_media.assert_called_once()

    @patch("os.path.exists", return_value=False)
    @patch("os.makedirs")
    async def test_download_without_dedup_downloads_directly(self, mock_makedirs, mock_exists):
        """When dedup is disabled, downloads directly to chat media dir."""
        from telethon.tl.types import MessageMediaPhoto

        listener = self._make_listener(deduplicate_media=False)
        listener.client.download_media = AsyncMock(return_value="/tmp/test_media/-100/123.jpg")

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 123
        media.photo.sizes = []
        msg.media = media
        msg.id = 1

        result = await listener._download_media(msg, -100)
        assert result is not None
        listener.client.download_media.assert_called_once()

    async def test_download_media_returns_none_on_exception(self):
        """Exception during download returns None instead of raising."""
        from telethon.tl.types import MessageMediaPhoto

        listener = self._make_listener()
        listener.client.download_media = AsyncMock(side_effect=Exception("network error"))

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 123
        media.photo.sizes = []
        msg.media = media
        msg.id = 1

        result = await listener._download_media(msg, -100)
        assert result is None

    async def test_download_media_checks_document_size(self):
        """Document size is checked against max_media_size_bytes."""
        from telethon.tl.types import MessageMediaDocument

        listener = self._make_listener()
        listener.config.get_max_media_size_bytes.return_value = 100

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaDocument)
        media.document = MagicMock()
        media.document.size = 200  # exceeds limit
        media.document.attributes = []
        media.document.id = 456
        msg.media = media
        msg.id = 2

        result = await listener._download_media(msg, -100)
        assert result is None
        listener.client.download_media.assert_not_called()

    async def test_download_media_photo_size_check(self):
        """Photo size from largest size entry is checked against max."""
        from telethon.tl.types import MessageMediaPhoto

        listener = self._make_listener()
        listener.config.get_max_media_size_bytes.return_value = 100

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 789
        size_entry = MagicMock()
        size_entry.size = 500  # exceeds limit
        media.photo.sizes = [size_entry]
        msg.media = media
        msg.id = 3

        result = await listener._download_media(msg, -100)
        assert result is None


# ===========================================================================
# _download_avatar
# ===========================================================================


class TestDownloadAvatar:
    """Tests for _download_avatar paths."""

    @patch("src.listener.get_avatar_paths", return_value=(None, "/legacy/path"))
    async def test_returns_early_when_no_avatar_set(self, mock_paths):
        """If avatar_path is None, returns without downloading."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        entity = MagicMock()

        await listener._download_avatar(entity, 123)
        listener.client.download_profile_photo.assert_not_called()

    @patch("src.listener.get_avatar_paths", return_value=("/path/avatar.jpg", "/legacy/path"))
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=1024)
    async def test_skips_download_when_file_exists(self, mock_size, mock_exists, mock_paths):
        """If avatar file already exists with content, skip download."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        entity = MagicMock()

        await listener._download_avatar(entity, 123)
        listener.client.download_profile_photo.assert_not_called()

    @patch("src.listener.get_avatar_paths", return_value=("/path/avatar.jpg", "/legacy/path"))
    @patch("os.path.exists", return_value=False)
    async def test_downloads_avatar_when_file_missing(self, mock_exists, mock_paths):
        """Downloads avatar when file does not exist."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        listener.client.download_profile_photo = AsyncMock(return_value="/path/avatar.jpg")
        entity = MagicMock()

        await listener._download_avatar(entity, 123)
        listener.client.download_profile_photo.assert_called_once()

    @patch("src.listener.get_avatar_paths", return_value=("/path/avatar.jpg", "/legacy/path"))
    @patch("os.path.exists", return_value=True)
    @patch("os.path.getsize", return_value=0)
    async def test_downloads_avatar_when_file_empty(self, mock_size, mock_exists, mock_paths):
        """Downloads avatar when file exists but is empty (0 bytes)."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        listener.client.download_profile_photo = AsyncMock(return_value="/path/avatar.jpg")
        entity = MagicMock()

        await listener._download_avatar(entity, 123)
        listener.client.download_profile_photo.assert_called_once()

    @patch("src.listener.get_avatar_paths", side_effect=Exception("filesystem error"))
    async def test_handles_exception_gracefully(self, mock_paths):
        """Exceptions during avatar download are caught and logged."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        entity = MagicMock()

        # Should not raise
        await listener._download_avatar(entity, 123)

    @patch("src.listener.get_avatar_paths", return_value=("/path/avatar.jpg", "/legacy/path"))
    @patch("os.path.exists", return_value=False)
    async def test_handles_none_download_result(self, mock_exists, mock_paths):
        """When download_profile_photo returns None, logs debug instead of info."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.client = AsyncMock()
        listener.client.download_profile_photo = AsyncMock(return_value=None)
        entity = MagicMock()

        # Should not raise
        await listener._download_avatar(entity, 123)


# ===========================================================================
# _notify_update
# ===========================================================================


class TestNotifyUpdate:
    """Tests for _notify_update notification dispatching."""

    async def test_returns_early_when_no_notifier(self):
        """If _notifier is None, returns without error."""
        listener = TelegramListener(_make_config(), _make_db())
        listener._notifier = None
        # Should not raise
        await listener._notify_update("edit", {"chat_id": 123, "message_id": 1})

    async def test_sends_edit_notification(self):
        """Sends edit notification with correct type mapping."""
        from src.realtime import NotificationType

        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("edit", {"chat_id": 123})
        notifier.notify.assert_called_once_with(NotificationType.EDIT, 123, {"chat_id": 123})

    async def test_sends_delete_notification(self):
        """Sends delete notification with correct type mapping."""
        from src.realtime import NotificationType

        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("delete", {"chat_id": 456})
        notifier.notify.assert_called_once_with(NotificationType.DELETE, 456, {"chat_id": 456})

    async def test_sends_new_message_notification(self):
        """Sends new_message notification with correct type mapping."""
        from src.realtime import NotificationType

        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("new_message", {"chat_id": 789})
        notifier.notify.assert_called_once_with(NotificationType.NEW_MESSAGE, 789, {"chat_id": 789})

    async def test_sends_pin_notification(self):
        """Sends pin notification with correct type mapping."""
        from src.realtime import NotificationType

        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("pin", {"chat_id": 111})
        notifier.notify.assert_called_once_with(NotificationType.PIN, 111, {"chat_id": 111})

    async def test_unknown_type_returns_without_sending(self):
        """Unknown notification type logs warning and returns without sending."""
        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("unknown_type", {"chat_id": 123})
        notifier.notify.assert_not_called()

    async def test_handles_exception_from_notifier(self):
        """Exception from notifier is caught and does not propagate."""
        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        notifier.notify = AsyncMock(side_effect=Exception("notify failed"))
        listener._notifier = notifier

        # Should not raise
        await listener._notify_update("edit", {"chat_id": 123})

    async def test_default_chat_id_when_missing(self):
        """Uses 0 as default chat_id when not provided in data."""
        from src.realtime import NotificationType

        listener = TelegramListener(_make_config(), _make_db())
        notifier = AsyncMock()
        listener._notifier = notifier

        await listener._notify_update("edit", {})
        notifier.notify.assert_called_once_with(NotificationType.EDIT, 0, {})


# ===========================================================================
# on_chat_action handler
# ===========================================================================


class TestOnChatActionHandler:
    """Tests for the on_chat_action event handler."""

    def _setup(self, **config_overrides):
        return _make_listener_with_handlers(**config_overrides)

    async def test_skips_when_listen_chat_actions_disabled(self):
        """Handler returns immediately when listen_chat_actions is False."""
        listener, handlers, db, config = self._setup(listen_chat_actions=False)
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        await handler(event)

        db.insert_message.assert_not_called()

    async def test_skips_untracked_chat(self):
        """Handler ignores actions from untracked chats."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = 99999  # Not tracked
        await handler(event)

        db.insert_message.assert_not_called()

    async def test_photo_changed_action(self):
        """Photo changed event saves service message and updates chat metadata."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = True
        event.new_title = None
        event.user_joined = False
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 111
        event.photo = MagicMock()

        # Mock get_entity for actor resolution
        actor = MagicMock()
        actor.first_name = "John"
        actor.last_name = "Doe"
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "photo" in call_data["text"].lower() or "changed" in call_data["text"].lower()
        assert call_data["raw_data"]["action_type"] == "photo_changed"

    async def test_title_changed_action(self):
        """Title changed event saves service message with new title."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()  # photo exists (not removed)
        event.new_title = "New Group Name"
        event.user_joined = False
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 111

        actor = MagicMock()
        actor.first_name = "Alice"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "New Group Name" in call_data["text"]
        assert call_data["raw_data"]["action_type"] == "title_changed"
        assert call_data["raw_data"]["new_title"] == "New Group Name"

    async def test_user_joined_action(self):
        """User joined event saves service message."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = True
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 222

        actor = MagicMock()
        actor.first_name = "Bob"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "joined" in call_data["text"].lower()

    async def test_user_left_action(self):
        """User left event saves service message."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = False
        event.user_left = True
        event.user_added = False
        event.user_kicked = False
        event.user_id = 333

        actor = MagicMock()
        actor.first_name = "Charlie"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "left" in call_data["text"].lower()

    async def test_user_added_action(self):
        """User added event saves service message."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = False
        event.user_left = False
        event.user_added = True
        event.user_kicked = False
        event.user_id = 444

        actor = MagicMock()
        actor.first_name = "Dave"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "added" in call_data["text"].lower()

    async def test_user_kicked_action(self):
        """User kicked event saves service message."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = False
        event.user_left = False
        event.user_added = False
        event.user_kicked = True
        event.user_id = 555

        actor = MagicMock()
        actor.first_name = "Eve"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "removed" in call_data["text"].lower()

    async def test_photo_removed_action(self):
        """Photo removed event (new_photo=False, photo=None) saves service message."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = None  # Photo was removed
        event.new_title = None
        event.user_joined = False
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 111

        actor = MagicMock()
        actor.first_name = "Admin"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert "removed" in call_data["text"].lower()
        assert call_data["raw_data"]["action_type"] == "photo_removed"

    async def test_increments_chat_actions_stat(self):
        """Handler increments the chat_actions stat counter."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = True
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = None  # No user_id

        await handler(event)

        assert listener.stats.get("chat_actions", 0) == 1

    async def test_error_in_handler_increments_errors(self):
        """Exception in handler increments error counter."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        listener._get_marked_id = MagicMock(side_effect=Exception("crash"))

        event = MagicMock()
        event.chat_id = -1001234567890

        await handler(event)
        assert listener.stats["errors"] == 1

    async def test_actor_without_user_id(self):
        """When event has no user_id, service message has no actor info."""
        listener, handlers, db, config = self._setup()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = True
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = None

        await handler(event)

        db.insert_message.assert_called_once()
        call_data = db.insert_message.call_args[0][0]
        assert call_data["sender_id"] is None


# ===========================================================================
# on_pinned_messages handler
# ===========================================================================


class TestOnPinnedMessagesHandler:
    """Tests for the on_pinned_messages event handler (pin/unpin)."""

    def _setup(self):
        return _make_listener_with_handlers()

    def _get_raw_handler(self, handlers):
        """Extract the Raw event handler from captured handlers."""
        for key, handler in handlers.items():
            # The Raw handler is registered with events.Raw(types=[...])
            if hasattr(key, "types") or (isinstance(key, type) and key != events.ChatAction):
                continue
            # Find the handler that handles UpdatePinnedChannelMessages
            if key not in (events.MessageEdited, events.MessageDeleted, events.NewMessage, events.ChatAction):
                return handler
        # Fallback: try all non-standard handlers
        for key, handler in handlers.items():
            if key not in (events.MessageEdited, events.MessageDeleted, events.NewMessage, events.ChatAction):
                return handler
        return None

    async def test_channel_pin_updates_messages(self):
        """Channel pin event updates pinned status in DB."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None, "Pin handler not found"

        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 1234567890
        event.messages = [10, 20, 30]
        event.pinned = True

        # The chat ID should be -1000000000000 - channel_id
        expected_chat_id = -1000000000000 - 1234567890
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        assert db.update_message_pinned.call_count == 3
        for msg_id in [10, 20, 30]:
            db.update_message_pinned.assert_any_call(expected_chat_id, msg_id, True)

    async def test_channel_unpin_updates_messages(self):
        """Channel unpin event sets pinned=False in DB."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 1234567890
        event.messages = [10]
        event.pinned = False

        expected_chat_id = -1000000000000 - 1234567890
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        db.update_message_pinned.assert_called_once_with(expected_chat_id, 10, False)

    async def test_group_pin_with_user_peer(self):
        """Group pin event with user peer extracts correct chat_id."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedMessages)
        peer = MagicMock()
        peer.user_id = 12345
        # Remove chat_id and channel_id attrs to ensure user_id path
        del peer.chat_id
        del peer.channel_id
        event.peer = peer
        event.messages = [5]
        event.pinned = True

        listener._tracked_chat_ids.add(12345)

        await pin_handler(event)

        db.update_message_pinned.assert_called_once_with(12345, 5, True)

    async def test_group_pin_with_chat_peer(self):
        """Group pin event with chat peer extracts negative chat_id."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedMessages)
        peer = MagicMock()
        # Remove user_id to trigger chat_id path
        del peer.user_id
        peer.chat_id = 67890
        del peer.channel_id
        event.peer = peer
        event.messages = [7]
        event.pinned = True

        expected_chat_id = -67890
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        db.update_message_pinned.assert_called_once_with(expected_chat_id, 7, True)

    async def test_group_pin_with_channel_peer(self):
        """Group pin event with channel peer uses -1000000000000 prefix."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedMessages)
        peer = MagicMock()
        del peer.user_id
        del peer.chat_id
        peer.channel_id = 999
        event.peer = peer
        event.messages = [8]
        event.pinned = True

        expected_chat_id = -1000000000000 - 999
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        db.update_message_pinned.assert_called_once_with(expected_chat_id, 8, True)

    async def test_skips_untracked_chat(self):
        """Pin event for untracked chat is ignored."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 9999999
        event.messages = [1]
        event.pinned = True

        await pin_handler(event)

        db.update_message_pinned.assert_not_called()

    async def test_increments_pins_stat(self):
        """Pin handler increments the pins stat counter."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 1234567890
        event.messages = [10, 20]
        event.pinned = True

        expected_chat_id = -1000000000000 - 1234567890
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        assert listener.stats.get("pins", 0) == 2

    async def test_unknown_event_type_returns_early(self):
        """Unknown event type (not UpdatePinned*) returns without action."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        # Pass an event that is neither UpdatePinnedChannelMessages nor UpdatePinnedMessages
        event = MagicMock()
        # Remove spec to make isinstance checks fail
        event.__class__ = type("SomeOtherUpdate", (), {})

        await pin_handler(event)

        db.update_message_pinned.assert_not_called()

    async def test_error_in_handler_increments_errors(self):
        """Exception in pin handler increments error counter."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        # Create event that will cause isinstance to succeed but then fail
        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 1234567890
        event.messages = [10]
        event.pinned = True

        expected_chat_id = -1000000000000 - 1234567890
        listener._tracked_chat_ids.add(expected_chat_id)

        db.update_message_pinned = AsyncMock(side_effect=Exception("db error"))

        await pin_handler(event)

        assert listener.stats["errors"] == 1

    async def test_pin_sends_notification(self):
        """Pin handler sends notification to viewer."""
        listener, handlers, db, config = self._setup()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        # Set up notifier
        notifier = AsyncMock()
        listener._notifier = notifier

        event = MagicMock(spec=UpdatePinnedChannelMessages)
        event.channel_id = 1234567890
        event.messages = [10]
        event.pinned = True

        expected_chat_id = -1000000000000 - 1234567890
        listener._tracked_chat_ids.add(expected_chat_id)

        await pin_handler(event)

        notifier.notify.assert_called_once()


# ===========================================================================
# on_message_deleted -- rate limiting on resolved chat path
# ===========================================================================


class TestOnMessageDeletedRateLimiting:
    """Tests for deletion handler rate limiting and resolved-chat paths."""

    async def test_resolved_chat_rate_limited(self):
        """When chat_id is None and resolved chat is rate-limited, deletion is discarded."""
        listener, handlers, db, config = _make_listener_with_handlers(
            mass_operation_threshold=1,
            mass_operation_window_seconds=60,
        )
        handler = handlers[events.MessageDeleted]

        # First: exhaust rate limit for the resolved chat
        db.resolve_message_chat_id = AsyncMock(return_value=-1001234567890)
        listener._tracked_chat_ids = {-1001234567890}

        # First deletion allowed
        event1 = MagicMock()
        event1.chat_id = None
        event1.deleted_ids = [1]
        await handler(event1)

        # Second triggers rate limit
        event2 = MagicMock()
        event2.chat_id = None
        event2.deleted_ids = [2]
        await handler(event2)

        # Third is blocked by already-blocked check
        event3 = MagicMock()
        event3.chat_id = None
        event3.deleted_ids = [3]
        await handler(event3)

        assert listener.stats["operations_discarded"] >= 1

    async def test_resolved_chat_not_tracked_is_skipped(self):
        """When chat_id is None and resolved chat is not tracked, deletion is skipped."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.MessageDeleted]

        db.resolve_message_chat_id = AsyncMock(return_value=99999)  # Not tracked
        listener._tracked_chat_ids = {-1001234567890}

        event = MagicMock()
        event.chat_id = None
        event.deleted_ids = [42]

        await handler(event)

        db.delete_message.assert_not_called()

    async def test_known_chat_rate_limited_increments_discarded(self):
        """When chat_id is known and rate-limited, operations_discarded is incremented."""
        listener, handlers, db, config = _make_listener_with_handlers(
            mass_operation_threshold=1,
            mass_operation_window_seconds=60,
        )
        handler = handlers[events.MessageDeleted]
        listener._tracked_chat_ids = {-1001234567890}

        # First: allowed
        event1 = MagicMock()
        event1.chat_id = -1001234567890
        event1.deleted_ids = [1]
        await handler(event1)

        # Second: triggers rate limit
        event2 = MagicMock()
        event2.chat_id = -1001234567890
        event2.deleted_ids = [2]
        await handler(event2)

        assert listener.stats["operations_discarded"] >= 1

    async def test_resolve_exception_is_caught(self):
        """Exception during chat_id resolution is caught and logged."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.MessageDeleted]

        db.resolve_message_chat_id = AsyncMock(side_effect=Exception("db error"))

        event = MagicMock()
        event.chat_id = None
        event.deleted_ids = [42]

        # Should not raise
        await handler(event)

        # The deletion is not applied
        db.delete_message.assert_not_called()

    async def test_second_should_process_check_on_known_chat(self):
        """When chat_id is known but not tracked, inner should_process_chat check skips."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.MessageDeleted]

        # Chat ID is known but NOT in tracked set after the first check
        # The first check passes (chat_id is not None), but the per-message check fails
        listener._tracked_chat_ids = set()  # Empty -- nothing tracked
        config.whitelist_mode = False
        config.global_include_ids = set()

        event = MagicMock()
        event.chat_id = -1001234567890
        event.deleted_ids = [1, 2]

        await handler(event)

        # Both messages skip the inner should_process_chat check
        db.delete_message.assert_not_called()


# ===========================================================================
# on_new_message -- sender, media, grouped_id paths
# ===========================================================================


class TestOnNewMessageAdvanced:
    """Tests for new message handler edge cases: sender info, media, grouped_id."""

    async def test_saves_sender_user_info(self):
        """When sender is a User entity, upsert_user is called."""
        from telethon.tl.types import User

        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.NewMessage]

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = "Hello"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None

        sender = MagicMock(spec=User)
        sender.id = 111
        sender.username = "testuser"
        sender.first_name = "Test"
        sender.last_name = "User"
        sender.phone = "+1234"
        sender.bot = False
        msg.sender = sender
        event.message = msg

        chat_entity = MagicMock()
        chat_entity.title = "Group"
        event.get_chat = AsyncMock(return_value=chat_entity)

        await handler(event)

        db.upsert_user.assert_called_once()
        user_data = db.upsert_user.call_args[0][0]
        assert user_data["id"] == 111
        assert user_data["username"] == "testuser"

    async def test_grouped_id_stored_in_raw_data(self):
        """When message has grouped_id, it is stored in raw_data."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.NewMessage]

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = "Album photo"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = True
        msg.grouped_id = 9876543210
        msg.media = None
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        await handler(event)

        call_data = db.insert_message.call_args[0][0]
        assert call_data["raw_data"]["grouped_id"] == "9876543210"
        assert call_data["is_outgoing"] == 1

    async def test_media_download_on_new_message(self):
        """When media is present and download is enabled, media is downloaded and saved."""
        from telethon.tl.types import MessageMediaPhoto

        listener, handlers, db, config = _make_listener_with_handlers(
            listen_new_messages_media=True,
        )
        handler = handlers[events.NewMessage]

        # Mock _download_media to return a path
        listener._download_media = AsyncMock(return_value="/tmp/media/-100/photo.jpg")

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = ""
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = MagicMock(spec=MessageMediaPhoto)
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        await handler(event)

        listener._download_media.assert_called_once()
        db.insert_media.assert_called_once()
        media_data = db.insert_media.call_args[0][0]
        assert media_data["file_path"] == "/tmp/media/-100/photo.jpg"
        assert media_data["downloaded"] is True

    async def test_media_download_failure_does_not_crash(self):
        """When media download raises, the error is caught and message is still saved."""
        from telethon.tl.types import MessageMediaPhoto

        listener, handlers, db, config = _make_listener_with_handlers(
            listen_new_messages_media=True,
        )
        handler = handlers[events.NewMessage]

        listener._download_media = AsyncMock(side_effect=Exception("download failed"))

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = ""
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = MagicMock(spec=MessageMediaPhoto)
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        await handler(event)

        # Message still saved despite media failure
        db.insert_message.assert_called_once()
        # Media record NOT inserted (download failed)
        db.insert_media.assert_not_called()

    async def test_notifier_called_for_new_message(self):
        """When notifier is set, it is called after saving new message."""
        from src.realtime import NotificationType

        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.NewMessage]

        notifier = AsyncMock()
        listener._notifier = notifier

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = "Short"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        await handler(event)

        notifier.notify.assert_called_once()
        call_args = notifier.notify.call_args
        assert call_args[0][0] == NotificationType.NEW_MESSAGE

    async def test_long_text_preview_truncated(self):
        """Messages with text > 50 chars are truncated for logging (no assertion on log, just no crash)."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.NewMessage]

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = "A" * 100  # 100 chars
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=MagicMock())

        # Should not raise
        await handler(event)
        assert listener.stats["new_messages_saved"] == 1

    async def test_get_chat_returns_none(self):
        """When get_chat returns None, upsert_chat is not called but message is still saved."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.NewMessage]

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.sender_id = 111
        msg.date = datetime(2025, 1, 1)
        msg.text = "hello"
        msg.reply_to_msg_id = None
        msg.edit_date = None
        msg.out = False
        msg.grouped_id = None
        msg.media = None
        msg.sender = None
        event.message = msg
        event.get_chat = AsyncMock(return_value=None)

        await handler(event)

        db.upsert_chat.assert_not_called()
        db.insert_message.assert_called_once()


# ===========================================================================
# on_message_edited -- notification path
# ===========================================================================


class TestOnMessageEditedNotification:
    """Tests for edit handler notification path."""

    async def test_edit_sends_notification(self):
        """Edit handler sends notification to viewer after applying edit."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.MessageEdited]

        notifier = AsyncMock()
        listener._notifier = notifier

        event = MagicMock()
        event.chat_id = -1001234567890
        msg = MagicMock()
        msg.reply_to = None
        msg.id = 42
        msg.text = "Updated"
        msg.edit_date = datetime(2025, 6, 1)
        event.message = msg

        await handler(event)

        # _notify_update is called, which calls notifier.notify
        notifier.notify.assert_called_once()

    async def test_edit_rate_limited_increments_discarded(self):
        """When edit is rate-limited, operations_discarded is incremented."""
        listener, handlers, db, config = _make_listener_with_handlers(
            mass_operation_threshold=1,
            mass_operation_window_seconds=60,
        )
        handler = handlers[events.MessageEdited]

        # First: allowed
        event1 = MagicMock()
        event1.chat_id = -1001234567890
        msg1 = MagicMock()
        msg1.reply_to = None
        msg1.id = 1
        msg1.text = "edit1"
        msg1.edit_date = None
        event1.message = msg1
        await handler(event1)

        # Second: triggers rate limit
        event2 = MagicMock()
        event2.chat_id = -1001234567890
        msg2 = MagicMock()
        msg2.reply_to = None
        msg2.id = 2
        msg2.text = "edit2"
        msg2.edit_date = None
        event2.message = msg2
        await handler(event2)

        assert listener.stats["operations_discarded"] >= 1


# ===========================================================================
# run() and stop() lifecycle
# ===========================================================================


class TestRunAndStopLifecycle:
    """Tests for run() and stop() methods."""

    async def test_run_sets_stats_and_starts_protector(self):
        """run() sets start_time, starts protector, writes metadata, then runs client."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)

        mock_client = AsyncMock()
        mock_client.run_until_disconnected = AsyncMock(side_effect=asyncio.CancelledError)
        listener.client = mock_client

        await listener.run()

        assert listener.stats["start_time"] is not None
        assert listener._running is False  # Reset in finally
        db.set_metadata.assert_any_call("listener_active_since", "")

    async def test_stop_disconnects_owned_client(self):
        """stop() disconnects the client when listener owns it."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._owns_client = True
        listener.stats["start_time"] = datetime.now()

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        listener.client = mock_client

        await listener.stop()

        assert listener._running is False
        mock_client.disconnect.assert_called_once()

    async def test_stop_does_not_disconnect_shared_client(self):
        """stop() does not disconnect when client was provided externally."""
        config = _make_config()
        db = _make_db()
        external_client = AsyncMock()
        listener = TelegramListener(config, db, client=external_client)
        listener.stats["start_time"] = datetime.now()

        assert listener._owns_client is False

        await listener.stop()

        external_client.disconnect.assert_not_called()

    async def test_stop_handles_disconnect_exception(self):
        """stop() catches exceptions during disconnect."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        listener._owns_client = True
        listener.stats["start_time"] = datetime.now()

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client.disconnect = AsyncMock(side_effect=Exception("disconnect error"))
        listener.client = mock_client

        # Should not raise
        await listener.stop()

    async def test_close_calls_stop_and_db_close(self):
        """close() calls stop() then db.close()."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)
        listener.client = AsyncMock()
        listener.client.is_connected = MagicMock(return_value=False)

        await listener.close()

        db.close.assert_called_once()

    async def test_run_cancels_processor_task_in_finally(self):
        """run() cancels _processor_task in finally block if it exists."""
        config = _make_config()
        db = _make_db()
        listener = TelegramListener(config, db)

        mock_client = AsyncMock()
        mock_client.run_until_disconnected = AsyncMock(side_effect=asyncio.CancelledError)
        listener.client = mock_client

        # Create a real asyncio Future that acts like a cancelled task
        loop = asyncio.get_event_loop()
        fake_task = loop.create_future()
        fake_task.cancel()
        # Wrap cancel in a MagicMock so we can assert it was called again by run()
        original_cancel = fake_task.cancel
        cancel_mock = MagicMock(side_effect=original_cancel)
        fake_task.cancel = cancel_mock
        listener._processor_task = fake_task

        await listener.run()

        cancel_mock.assert_called()


# ===========================================================================
# _log_stats
# ===========================================================================


class TestLogStats:
    """Tests for _log_stats output paths."""

    async def test_log_stats_with_no_start_time(self):
        """_log_stats does nothing when start_time is None."""
        listener = TelegramListener(_make_config(), _make_db())
        # Should not raise
        await listener._log_stats()

    async def test_log_stats_with_start_time_and_errors(self):
        """_log_stats logs error count when errors > 0."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.stats["start_time"] = datetime.now() - timedelta(hours=1)
        listener.stats["errors"] = 5
        listener.stats["deletions_skipped"] = 10
        # Should not raise
        await listener._log_stats()

    async def test_log_stats_with_blocked_chats(self):
        """_log_stats logs blocked chat details when chats are rate-limited."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.stats["start_time"] = datetime.now() - timedelta(minutes=30)

        # Add a blocked chat to protector
        future = datetime.now() + timedelta(hours=1)
        listener._protector._blocked[12345] = (future, "rate limited", 15)

        # Should not raise
        await listener._log_stats()

    async def test_log_stats_shows_zero_errors_without_warning(self):
        """_log_stats with zero errors does not log the error warning."""
        listener = TelegramListener(_make_config(), _make_db())
        listener.stats["start_time"] = datetime.now() - timedelta(minutes=5)
        listener.stats["errors"] = 0
        listener.stats["deletions_skipped"] = 0
        # Should not raise
        await listener._log_stats()


# ===========================================================================
# _load_tracked_chats error path
# ===========================================================================


class TestLoadTrackedChatsError:
    """Tests for _load_tracked_chats exception handling."""

    async def test_exception_sets_empty_tracked_chats(self):
        """When get_all_chats raises, tracked_chat_ids is set to empty set."""
        db = _make_db()
        db.get_all_chats = AsyncMock(side_effect=Exception("db error"))
        listener = TelegramListener(_make_config(), db)

        await listener._load_tracked_chats()

        assert listener._tracked_chat_ids == set()


# ===========================================================================
# _get_marked_id fallback path
# ===========================================================================


class TestGetMarkedIdFallback:
    """Tests for _get_marked_id fallback when get_peer_id fails."""

    def test_fallback_to_raw_integer(self):
        """When get_peer_id fails and input is raw int, returns the int."""
        listener = TelegramListener(_make_config(), _make_db())
        # Raw int -- get_peer_id will fail, no .id attr, returns raw
        result = listener._get_marked_id(42)
        assert result == 42

    def test_fallback_to_entity_id(self):
        """When get_peer_id fails, falls back to entity.id."""
        listener = TelegramListener(_make_config(), _make_db())
        entity = MagicMock()
        entity.id = 99999
        result = listener._get_marked_id(entity)
        assert result == 99999


# ===========================================================================
# create() factory method (lines 312-313)
# ===========================================================================


class TestListenerCreateFactory:
    """Tests for TelegramListener.create() factory method."""

    async def test_create_initializes_db_and_returns_instance(self):
        """create() calls create_adapter and returns a TelegramListener."""
        config = _make_config()
        mock_db = _make_db()

        with patch("src.listener.create_adapter", new_callable=AsyncMock, return_value=mock_db):
            listener = await TelegramListener.create(config)

        assert isinstance(listener, TelegramListener)
        assert listener.db is mock_db

    async def test_create_with_client_passes_through(self):
        """create() forwards the client parameter."""
        config = _make_config()
        mock_db = _make_db()
        mock_client = MagicMock()

        with patch("src.listener.create_adapter", new_callable=AsyncMock, return_value=mock_db):
            listener = await TelegramListener.create(config, client=mock_client)

        assert listener.client is mock_client
        assert listener._owns_client is False


# ===========================================================================
# connect() shared client not connected (lines 324-327)
# ===========================================================================


class TestListenerConnectSharedClient:
    """Tests for connect() shared client validation (lines 324-327)."""

    async def test_shared_client_not_connected_raises(self):
        """Shared client that is NOT connected raises RuntimeError."""
        config = _make_config()
        db = _make_db()
        mock_client = MagicMock()
        mock_client.is_connected = MagicMock(return_value=False)
        listener = TelegramListener(config, db, client=mock_client)

        import pytest

        with pytest.raises(RuntimeError, match="Shared client is not connected"):
            await listener.connect()

    async def test_shared_client_connected_succeeds(self):
        """Shared client that is connected returns normally."""
        config = _make_config()
        db = _make_db()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", phone="+1"))
        mock_client.on = lambda event_type: lambda fn: fn
        listener = TelegramListener(config, db, client=mock_client)

        mock_db_manager = MagicMock()
        mock_db_manager._is_sqlite = True

        with patch("src.db.get_db_manager", new_callable=AsyncMock, return_value=mock_db_manager):
            await listener.connect()


# ===========================================================================
# connect() not authorized (lines 342-344)
# ===========================================================================


class TestListenerConnectNotAuthorized:
    """Tests for connect() session not authorized (lines 342-344)."""

    async def test_not_authorized_raises_runtime_error(self):
        """connect() raises when session is not authorized."""
        config = _make_config()
        config.get_telegram_client_kwargs = MagicMock(return_value={})
        db = _make_db()
        listener = TelegramListener(config, db)

        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)

        with (
            patch("src.listener.TelegramClient", return_value=mock_client),
            pytest.raises(RuntimeError, match="Session not authorized"),
        ):
            await listener.connect()


# ===========================================================================
# _get_marked_id fallback returns raw value (line 385)
# ===========================================================================


class TestGetMarkedIdRawFallback:
    """Tests for _get_marked_id fallback to raw value (line 385)."""

    def test_raw_integer_returns_itself(self):
        """When input is a raw integer and has no .id, returns the integer."""
        listener = TelegramListener(_make_config(), _make_db())
        result = listener._get_marked_id(12345)
        assert result == 12345


# ===========================================================================
# _download_media symlink OSError fallback (lines 631-635)
# ===========================================================================


class TestDownloadMediaSymlinkFallback:
    """Tests for _download_media symlink failure fallback (lines 631-635)."""

    @patch("os.path.exists", return_value=False)
    @patch("os.makedirs")
    @patch("os.path.lexists", return_value=False)
    @patch("os.symlink", side_effect=OSError("symlinks not supported"))
    @patch("os.path.relpath", return_value="../_shared/file.jpg")
    @patch("shutil.move")
    async def test_symlink_failure_falls_back_to_move(
        self, mock_move, mock_relpath, mock_symlink, mock_lexists, mock_makedirs, mock_exists
    ):
        """When symlink fails, falls back to shutil.move."""
        from telethon.tl.types import MessageMediaPhoto

        config = _make_config(deduplicate_media=True)
        db = _make_db()
        listener = TelegramListener(config, db)
        listener.client = AsyncMock()
        listener.client.download_media = AsyncMock(return_value="/tmp/test_media/_shared/123.jpg")

        msg = MagicMock()
        media = MagicMock(spec=MessageMediaPhoto)
        media.photo = MagicMock()
        media.photo.id = 123
        media.photo.sizes = []
        msg.media = media
        msg.id = 1

        result = await listener._download_media(msg, -100)
        assert result is not None
        mock_move.assert_called_once()


# ===========================================================================
# on_message_deleted _should_process_chat inner check (line 768)
# ===========================================================================


class TestOnMessageDeletedInnerProcessCheck:
    """Tests for deletion handler inner _should_process_chat (line 768)."""

    async def test_resolved_chat_not_tracked_skips_deletion(self):
        """When resolved effective_chat_id fails _should_process_chat, skip deletion."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.MessageDeleted]

        db.resolve_message_chat_id = AsyncMock(return_value=-9999)
        listener._tracked_chat_ids = {-1001234567890}

        event = MagicMock()
        event.chat_id = None
        event.deleted_ids = [100]

        await handler(event)

        db.delete_message.assert_not_called()


# ===========================================================================
# on_chat_action actor resolution with last_name (lines 981-982)
# ===========================================================================


class TestOnChatActionActorLastName:
    """Tests for on_chat_action actor last_name appended (lines 981-982)."""

    async def test_actor_with_last_name_appended(self):
        """When actor has last_name, it is appended to actor_name."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.ChatAction]

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = True
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 111

        actor = MagicMock()
        actor.first_name = "John"
        actor.last_name = "Smith"
        listener.client.get_entity = AsyncMock(return_value=actor)

        await handler(event)

        db.insert_message.assert_called_once()
        text = db.insert_message.call_args[0][0]["text"]
        assert "John Smith" in text


# ===========================================================================
# on_chat_action insert_message exception (lines 1036-1037)
# ===========================================================================


class TestOnChatActionInsertMessageException:
    """Tests for on_chat_action insert_message exception (lines 1036-1037)."""

    async def test_insert_message_exception_caught(self):
        """Exception during insert_message is caught and logged."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.ChatAction]

        db.insert_message = AsyncMock(side_effect=Exception("DB error"))

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = False
        event.photo = MagicMock()
        event.new_title = None
        event.user_joined = True
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = None

        await handler(event)

        assert listener.stats.get("chat_actions", 0) == 1


# ===========================================================================
# on_chat_action metadata update exception (lines 1058-1059)
# ===========================================================================


class TestOnChatActionMetadataUpdateException:
    """Tests for on_chat_action metadata update exception (lines 1058-1059)."""

    async def test_metadata_update_exception_caught(self):
        """Exception during chat metadata update is caught."""
        listener, handlers, db, config = _make_listener_with_handlers()
        handler = handlers[events.ChatAction]

        # Make upsert_chat raise
        db.upsert_chat = AsyncMock(side_effect=Exception("chat update failed"))

        event = MagicMock()
        event.chat_id = -1001234567890
        event.new_photo = True
        event.new_title = None
        event.user_joined = False
        event.user_left = False
        event.user_added = False
        event.user_kicked = False
        event.user_id = 111
        event.photo = MagicMock()

        actor = MagicMock()
        actor.first_name = "Admin"
        actor.last_name = None
        listener.client.get_entity = AsyncMock(return_value=actor)
        listener.client.get_input_entity = AsyncMock(return_value=MagicMock())

        await handler(event)

        # Should not crash
        assert listener.stats.get("chat_actions", 0) == 1


# ===========================================================================
# on_pinned_messages peer has no recognized attribute (line 1094)
# ===========================================================================


class TestOnPinnedMessagesNoPeerAttr:
    """Tests for on_pinned_messages when peer has no recognized attribute (line 1094)."""

    def _get_raw_handler(self, handlers):
        for key, handler in handlers.items():
            if key not in (events.MessageEdited, events.MessageDeleted, events.NewMessage, events.ChatAction):
                return handler
        return None

    async def test_peer_without_recognized_attrs_returns(self):
        """UpdatePinnedMessages with peer lacking user_id/chat_id/channel_id returns."""
        listener, handlers, db, config = _make_listener_with_handlers()
        pin_handler = self._get_raw_handler(handlers)
        assert pin_handler is not None

        event = MagicMock(spec=UpdatePinnedMessages)
        peer = MagicMock(spec=[])  # No user_id, chat_id, or channel_id
        event.peer = peer
        event.messages = [1]
        event.pinned = True

        await pin_handler(event)

        db.update_message_pinned.assert_not_called()


# ===========================================================================
# run() metadata write exception (lines 1141-1142)
# ===========================================================================


class TestRunMetadataWriteException:
    """Tests for run() metadata write exception (lines 1141-1142)."""

    async def test_metadata_write_exception_caught(self):
        """Exception writing listener_active_since to DB is caught."""
        config = _make_config()
        db = _make_db()
        db.set_metadata = AsyncMock(side_effect=Exception("DB write failed"))
        listener = TelegramListener(config, db)

        mock_client = AsyncMock()
        mock_client.run_until_disconnected = AsyncMock(side_effect=asyncio.CancelledError)
        listener.client = mock_client

        await listener.run()

        assert listener._running is False


# ===========================================================================
# run() finally clears metadata (lines 1160-1161)
# ===========================================================================


class TestRunFinallyMetadataClear:
    """Tests for run() finally block clearing listener_active_since (lines 1160-1161)."""

    async def test_finally_clears_metadata_even_on_exception(self):
        """Finally block clears listener_active_since even when exception occurs."""
        config = _make_config()
        db = _make_db()
        call_count = [0]
        original_set_metadata = db.set_metadata

        async def track_set_metadata(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return  # First call (setting active) succeeds
            raise Exception("clear failed")  # Second call (clearing) fails

        db.set_metadata = AsyncMock(side_effect=track_set_metadata)
        listener = TelegramListener(config, db)

        mock_client = AsyncMock()
        mock_client.run_until_disconnected = AsyncMock(side_effect=asyncio.CancelledError)
        listener.client = mock_client

        await listener.run()

        assert listener._running is False


# ===========================================================================
# run_listener standalone function (lines 1258-1266)
# ===========================================================================


class TestRunListenerStandalone:
    """Tests for run_listener standalone function (lines 1258-1266)."""

    async def test_run_listener_connects_runs_and_closes(self):
        """run_listener creates listener, connects, runs, and closes."""
        from src.listener import run_listener

        mock_listener = AsyncMock()
        mock_listener.connect = AsyncMock()
        mock_listener.run = AsyncMock()
        mock_listener.close = AsyncMock()

        config = _make_config()

        with patch("src.listener.TelegramListener.create", new_callable=AsyncMock, return_value=mock_listener):
            await run_listener(config)

        mock_listener.connect.assert_awaited_once()
        mock_listener.run.assert_awaited_once()
        mock_listener.close.assert_awaited_once()

    async def test_run_listener_closes_on_keyboard_interrupt(self):
        """run_listener calls close() even on KeyboardInterrupt."""
        from src.listener import run_listener

        mock_listener = AsyncMock()
        mock_listener.connect = AsyncMock()
        mock_listener.run = AsyncMock(side_effect=KeyboardInterrupt)
        mock_listener.close = AsyncMock()

        config = _make_config()

        with patch("src.listener.TelegramListener.create", new_callable=AsyncMock, return_value=mock_listener):
            await run_listener(config)

        mock_listener.close.assert_awaited_once()


# ===========================================================================
# main() entry point (lines 1271-1291, 1295)
# ===========================================================================


class TestListenerMainEntryPoint:
    """Tests for listener main() entry point (lines 1271-1291)."""

    async def test_main_creates_config_and_runs(self):
        """main() creates Config, sets up logging, and calls run_listener."""
        from src.listener import main

        mock_config = MagicMock()

        with (
            patch("src.config.Config", return_value=mock_config),
            patch("src.config.setup_logging"),
            patch("src.listener.run_listener", new_callable=AsyncMock) as mock_run,
        ):
            await main()

        mock_run.assert_awaited_once_with(mock_config)

    async def test_main_value_error_raises(self):
        """main() re-raises ValueError from config."""
        from src.listener import main

        with (
            patch("src.config.Config", side_effect=ValueError("bad config")),
            patch("src.config.setup_logging"),
            pytest.raises(ValueError, match="bad config"),
        ):
            await main()

    async def test_main_generic_exception_raises(self):
        """main() re-raises generic exceptions."""
        from src.listener import main

        with (
            patch("src.config.Config", side_effect=RuntimeError("fatal")),
            patch("src.config.setup_logging"),
            pytest.raises(RuntimeError, match="fatal"),
        ):
            await main()
