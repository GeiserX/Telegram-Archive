"""Tests for avatar utility functions."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from telethon.tl.types import ChatPhotoEmpty, UserProfilePhotoEmpty

from src.avatar_utils import _get_avatar_dir, get_avatar_paths


class TestGetAvatarDir(unittest.TestCase):
    """Test _get_avatar_dir helper."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_returns_users_dir_for_user_entity(self):
        """Returns avatars/users path for User entities."""
        from telethon.tl.types import User

        entity = MagicMock(spec=User)
        result = _get_avatar_dir(self.temp_dir, entity)
        expected = os.path.join(self.temp_dir, "avatars", "users")
        assert result == expected
        assert os.path.isdir(expected)

    def test_returns_chats_dir_for_non_user_entity(self):
        """Returns avatars/chats path for non-User entities (channels, groups)."""
        entity = MagicMock()  # Not a User instance
        result = _get_avatar_dir(self.temp_dir, entity)
        expected = os.path.join(self.temp_dir, "avatars", "chats")
        assert result == expected
        assert os.path.isdir(expected)

    def test_creates_directory_if_not_exists(self):
        """Creates the avatar directory when it does not exist yet."""
        from telethon.tl.types import User

        entity = MagicMock(spec=User)
        media_path = os.path.join(self.temp_dir, "new_media")
        result = _get_avatar_dir(media_path, entity)
        assert os.path.isdir(result)


class TestGetAvatarPaths(unittest.TestCase):
    """Test get_avatar_paths function."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_returns_none_target_when_photo_is_none(self):
        """Returns (None, legacy_path) when entity has no photo attribute."""
        entity = MagicMock()
        entity.photo = None
        target, legacy = get_avatar_paths(self.temp_dir, entity, 12345)
        assert target is None
        assert legacy.endswith("12345.jpg")

    def test_returns_none_target_when_photo_is_chat_photo_empty(self):
        """Returns (None, legacy_path) when entity photo is ChatPhotoEmpty."""
        entity = MagicMock()
        entity.photo = ChatPhotoEmpty()
        target, legacy = get_avatar_paths(self.temp_dir, entity, 99999)
        assert target is None
        assert legacy.endswith("99999.jpg")

    def test_returns_none_target_when_photo_is_user_profile_photo_empty(self):
        """Returns (None, legacy_path) when entity photo is UserProfilePhotoEmpty."""
        entity = MagicMock()
        entity.photo = UserProfilePhotoEmpty()
        target, legacy = get_avatar_paths(self.temp_dir, entity, 77777)
        assert target is None
        assert legacy.endswith("77777.jpg")

    def test_returns_target_with_photo_id(self):
        """Returns target path with photo_id suffix when photo has photo_id."""
        entity = MagicMock()
        photo = MagicMock()
        photo.photo_id = 123456789
        entity.photo = photo
        target, legacy = get_avatar_paths(self.temp_dir, entity, 42)

        assert target is not None
        assert target.endswith("42_123456789.jpg")
        assert legacy.endswith("42.jpg")

    def test_returns_target_with_id_fallback(self):
        """Returns target path with id suffix when photo has id but no photo_id."""
        entity = MagicMock()
        photo = MagicMock(spec=[])
        photo.id = 987654321
        # photo_id does not exist on this spec, so getattr returns None
        entity.photo = photo
        target, legacy = get_avatar_paths(self.temp_dir, entity, 55)

        assert target is not None
        assert target.endswith("55_987654321.jpg")
        assert legacy.endswith("55.jpg")

    def test_returns_target_with_current_suffix_when_no_id(self):
        """Returns target path with _current suffix when photo has no IDs."""
        entity = MagicMock()
        photo = MagicMock(spec=[])
        # No photo_id and no id attributes
        entity.photo = photo
        target, legacy = get_avatar_paths(self.temp_dir, entity, 88)

        assert target is not None
        assert target.endswith("88_current.jpg")
        assert legacy.endswith("88.jpg")

    def test_returns_none_target_when_no_photo_attr(self):
        """Returns (None, legacy_path) when entity has no photo attribute at all."""
        entity = MagicMock(spec=[])
        target, legacy = get_avatar_paths(self.temp_dir, entity, 33)
        assert target is None
        assert legacy.endswith("33.jpg")

    def test_legacy_path_uses_correct_folder_for_user(self):
        """Legacy path uses avatars/users for User entities."""
        from telethon.tl.types import User

        entity = MagicMock(spec=User)
        entity.photo = None
        target, legacy = get_avatar_paths(self.temp_dir, entity, 100)
        assert "/avatars/users/" in legacy

    def test_legacy_path_uses_correct_folder_for_channel(self):
        """Legacy path uses avatars/chats for non-User entities."""
        entity = MagicMock()
        entity.photo = None
        target, legacy = get_avatar_paths(self.temp_dir, entity, 200)
        assert "/avatars/chats/" in legacy


if __name__ == "__main__":
    unittest.main()
