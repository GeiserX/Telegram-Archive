"""Classification of refreshable Telegram media-download errors.

Telegram's ``upload.GetFile`` can report a media file's storage location as
temporarily unavailable or invalid. These are transient/per-file conditions:
the documented response (mirrored by the official Telegram Desktop client) is to
re-fetch the message for a fresh reference/location and retry, then leave the
item for a later attempt rather than failing hard.

Two shapes reach us through Telethon:

* ``LOCATION_NOT_AVAILABLE`` has no generated Telethon exception class, so
  ``rpc_message_to_error`` falls back by numeric code (400 -> ``BadRequestError``)
  and preserves the raw server string in ``exc.message`` — match it by text.
* ``LOCATION_INVALID`` *does* have a generated class (``LocationInvalidError``)
  whose ``.message`` is the generic ``"BAD_REQUEST"`` — match it by type.

This lives in its own module (not ``message_utils``, which is stdlib-only and is
shipped inside the standalone viewer image) so the Telethon import never reaches
the viewer.
"""

from __future__ import annotations

from telethon.errors import LocationInvalidError, RPCError

# Server message codes (already upper-case) handled by re-fetch + backoff retry.
MEDIA_LOCATION_ERROR_MESSAGES = frozenset({"LOCATION_NOT_AVAILABLE", "LOCATION_INVALID"})


def is_media_location_error(exc: BaseException) -> bool:
    """Return ``True`` for a refreshable Telegram media-location error.

    Matches precisely (no loose substring): by exact ``exc.message`` for codes
    Telethon surfaces as a generic ``BadRequestError`` (e.g. ``LOCATION_NOT_AVAILABLE``)
    and by exception type for codes with a generated class (``LocationInvalidError``).
    """
    if not isinstance(exc, RPCError):
        return False
    if isinstance(exc, LocationInvalidError):
        return True
    message = getattr(exc, "message", None)
    return isinstance(message, str) and message.upper() in MEDIA_LOCATION_ERROR_MESSAGES
