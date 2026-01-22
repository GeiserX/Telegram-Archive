import os
import logging
from typing import Optional, Tuple

from telethon.tl.types import User, ChatPhotoEmpty, UserProfilePhotoEmpty

logger = logging.getLogger(__name__)


def _get_avatar_dir(media_path: str, entity) -> str:
    """Return avatar directory for given entity and ensure it exists."""
    folder = "users" if isinstance(entity, User) else "chats"
    base_dir = os.path.join(media_path, "avatars", folder)
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


def get_avatar_paths(media_path: str, entity, chat_id: int) -> Tuple[Optional[str], str]:
    """
    Build target and legacy avatar file paths.

    Returns:
        (target_path, legacy_path)
        - target_path is None when entity has no avatar
        - legacy_path is the old `<chat_id>.jpg` name used in past versions
    """
    base_dir = _get_avatar_dir(media_path, entity)
    legacy_path = os.path.join(base_dir, f"{chat_id}.jpg")

    photo = getattr(entity, "photo", None)
    if photo is None or isinstance(photo, (ChatPhotoEmpty, UserProfilePhotoEmpty)):
        return None, legacy_path

    photo_id = getattr(photo, "photo_id", None) or getattr(photo, "id", None)
    suffix = f"_{photo_id}" if photo_id is not None else "_current"
    file_name = f"{chat_id}{suffix}.jpg"
    return os.path.join(base_dir, file_name), legacy_path
