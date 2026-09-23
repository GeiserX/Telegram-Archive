import logging
import os

from telethon.tl.types import ChatPhotoEmpty, User, UserProfilePhotoEmpty

logger = logging.getLogger(__name__)


def _get_avatar_dir(media_path: str, entity) -> str:
    """Return avatar directory for given entity and ensure it exists."""
    folder = "users" if isinstance(entity, User) else "chats"
    base_dir = os.path.join(media_path, "avatars", folder)
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


def avatar_photo_id(entity) -> int | None:
    """The photo id this account sees for ``entity``, or None when it has no avatar.

    The id is per viewing account, not per peer: a photo one account set for a
    contact ("set a photo for this contact") is visible only to that account,
    so two accounts can see different photos for the same user. It is the same
    id ``get_avatar_paths`` puts in the file name, which is what lets the viewer
    pick the file the owning account actually saw.
    """
    photo = getattr(entity, "photo", None)
    if photo is None or isinstance(photo, (ChatPhotoEmpty, UserProfilePhotoEmpty)):
        return None
    photo_id = getattr(photo, "photo_id", None) or getattr(photo, "id", None)
    return photo_id if isinstance(photo_id, int) else None


def get_avatar_paths(media_path: str, entity, chat_id: int) -> tuple[str | None, str]:
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

    photo_id = avatar_photo_id(entity)
    suffix = f"_{photo_id}" if photo_id is not None else "_current"
    file_name = f"{chat_id}{suffix}.jpg"
    return os.path.join(base_dir, file_name), legacy_path
