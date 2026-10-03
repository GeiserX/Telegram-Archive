"""The map picture of a shared location, fetched from Telegram itself.

Three of the official apps (Telegram Desktop, Web A and Web K, plus Android
with the Telegram map provider) draw a location as a picture Telegram's own
servers render: ``upload.getWebFile`` with an ``inputWebFileGeoPointLocation``,
sent to the data centre the server config names as ``webfile_dc_id``. The
client paints the pin over the centre. This module makes the same request,
with Telegram Desktop's size (320x240 at scale 2, so 640x480) and zoom 15, and
stores the answer as a file beside the chat's other media
(docs/design/location-and-contact.md, "Map picture").

Nobody but Telegram sees the point: no tile server, no third party. Telethon
has no download path for a web file location (``download_file`` accepts only
an ``InputFileLocation``, and ``_download_web_document`` fetches a URL over
HTTP), so the request is built here and sent the way Telethon's own downloads
reach another data centre: a borrowed exported sender.

Nothing here is logged but counts and error type names: a coordinate is
message content.
"""

import asyncio
import hashlib
import logging
import os

from telethon.errors import FloodPremiumWaitError, FloodWaitError, LocationInvalidError, RPCError
from telethon.tl.functions.help import GetConfigRequest
from telethon.tl.functions.upload import GetWebFileRequest
from telethon.tl.types import InputGeoPoint, InputWebFileGeoPointLocation

from .message_utils import finalize_atomic_download

logger = logging.getLogger(__name__)

# Telegram Desktop's locationSize (320x240, 4:3, which Web A also uses),
# Android's middle zoom, and scale 2: a 640x480 picture, sharp at 2x.
MAP_WIDTH = 320
MAP_HEIGHT = 240
MAP_ZOOM = 15
MAP_SCALE = 2

# Web A asks in 512 KB parts. A 640x480 picture is far smaller, so one part
# is the rule; the cap only stops a server that never ends its answer.
MAP_PART_BYTES = 512 * 1024
MAP_MAX_BYTES = 4 * 1024 * 1024

# Web A's fallback when the server config carries no webfile_dc_id.
FALLBACK_WEBFILE_DC = 4

# The errors that mean Telegram will not render this point.
NOT_SERVED_ERRORS = frozenset({"LOCATION_INVALID", "WEBFILE_NOT_AVAILABLE"})

_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
)

# help.getConfig's webfile_dc_id, read once per process. The data centre is
# a property of Telegram's network, the same for every account.
_webfile_dc_id: int | None = None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def map_point(media: object) -> tuple[float, float, int] | None:
    """(lat, long, access_hash) of a location, venue or live location, or None.

    None for ``GeoPointEmpty``, a missing point and an ``access_hash`` of 0:
    Telegram cannot render those, so no request is made.
    """
    geo = getattr(media, "geo", None)
    if type(geo).__name__ != "GeoPoint":
        return None
    lat = getattr(geo, "lat", None)
    long = getattr(geo, "long", None)
    access_hash = getattr(geo, "access_hash", None)
    if not (_is_number(lat) and _is_number(long)):
        return None
    if not isinstance(access_hash, int) or isinstance(access_hash, bool) or access_hash == 0:
        return None
    return lat, long, access_hash


def map_preview_stem(lat: float, long: float) -> str:
    """``map_<16 hex>``: the picture's name, from the point and the size asked for.

    Content-addressed, so two rows that show the same point share one file,
    and a name that exists already is the same picture and is never written
    again. A name built from the message id would let two accounts that hold
    the same chat id and message ids meet each other's picture.
    """
    key = f"{lat!r},{long!r},{MAP_WIDTH},{MAP_HEIGHT},{MAP_ZOOM},{MAP_SCALE}"
    return "map_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def _image_kind(data: bytes) -> tuple[str, str] | None:
    """(extension, mime type) from the picture's first bytes, or None when it is no JPEG or PNG."""
    for signature, extension, mime_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mime_type
    return None


def _saved(path: str, mime_type: str) -> dict:
    return {
        "status": "saved",
        "file_path": path,
        "file_name": os.path.basename(path),
        "file_size": os.path.getsize(path),
        "mime_type": mime_type,
        "width": MAP_WIDTH * MAP_SCALE,
        "height": MAP_HEIGHT * MAP_SCALE,
    }


def _existing_picture(chat_dir: str, stem: str) -> dict | None:
    for _signature, extension, mime_type in _IMAGE_SIGNATURES:
        path = os.path.join(chat_dir, stem + extension)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return _saved(path, mime_type)
    return None


async def _webfile_dc(client) -> int:
    """The data centre web files are served from: help.getConfig's webfile_dc_id, else 4."""
    global _webfile_dc_id
    if _webfile_dc_id is not None:
        return _webfile_dc_id
    try:
        config = await client(GetConfigRequest())
    except FloodWaitError, FloodPremiumWaitError:
        raise
    except Exception as e:
        # Not kept: the next picture asks again.
        logger.debug(f"Could not read the server config ({type(e).__name__}); using data centre {FALLBACK_WEBFILE_DC}")
        return FALLBACK_WEBFILE_DC
    dc_id = getattr(config, "webfile_dc_id", None)
    _webfile_dc_id = dc_id if isinstance(dc_id, int) and not isinstance(dc_id, bool) and dc_id > 0 else None
    return _webfile_dc_id or FALLBACK_WEBFILE_DC


async def _download(client, point: tuple[float, float, int], flood_sleep_threshold: int) -> bytes:
    lat, long, access_hash = point
    location = InputWebFileGeoPointLocation(
        # No accuracy radius, as Telegram Desktop sends it.
        geo_point=InputGeoPoint(lat=lat, long=long),
        access_hash=access_hash,
        w=MAP_WIDTH,
        h=MAP_HEIGHT,
        zoom=MAP_ZOOM,
        scale=MAP_SCALE,
    )
    dc_id = await _webfile_dc(client)
    # The API asks for the request on webfile_dc_id. From the home data
    # centre the client's own sender is it (Telethon's downloads make the
    # same check); from any other, a borrowed exported sender.
    exported = dc_id != getattr(getattr(client, "session", None), "dc_id", None)
    sender = await client._borrow_exported_sender(dc_id) if exported else client._sender
    try:
        parts: list[bytes] = []
        offset = 0
        while True:
            answer = await client._call(
                sender,
                GetWebFileRequest(location=location, offset=offset, limit=MAP_PART_BYTES),
                flood_sleep_threshold=flood_sleep_threshold,
            )
            chunk = getattr(answer, "bytes", None) or b""
            parts.append(chunk)
            offset += len(chunk)
            if len(chunk) < MAP_PART_BYTES:
                return b"".join(parts)
            if offset >= MAP_MAX_BYTES:
                raise ValueError("map picture larger than the cap")
    finally:
        if exported:
            await client._return_exported_sender(sender)


async def fetch_map_preview(client, media: object, chat_dir: str, *, flood_sleep_threshold: int = 60) -> dict:
    """Fetch and keep the map picture of one location; a result dict, never raises.

    ``{"status": "saved", file_path, file_name, file_size, mime_type, width,
    height}`` when the picture is on disk, or ``{"status": reason}`` with
    reason ``no_point`` (nothing Telegram can render, no request made),
    ``not_served`` (Telegram refused this point), ``flood`` (a FloodWait
    longer than ``flood_sleep_threshold``; ``seconds`` says how long) or
    ``error``. A picture already on disk under the same name is reused and
    never written again, with no request.
    """
    point = map_point(media)
    if point is None:
        return {"status": "no_point"}
    stem = map_preview_stem(point[0], point[1])
    existing = _existing_picture(chat_dir, stem)
    if existing is not None:
        return existing
    try:
        data = await _download(client, point, flood_sleep_threshold)
    except (FloodWaitError, FloodPremiumWaitError) as e:
        return {"status": "flood", "seconds": int(getattr(e, "seconds", 0) or 0)}
    except RPCError as e:
        # Telethon raises a known error as its own class, whose message is a
        # description, and an unknown one with Telegram's error text.
        if isinstance(e, LocationInvalidError) or getattr(e, "message", None) in NOT_SERVED_ERRORS:
            return {"status": "not_served"}
        logger.warning(f"Could not fetch a map picture ({type(e).__name__})")
        return {"status": "error"}
    except Exception as e:
        logger.warning(f"Could not fetch a map picture ({type(e).__name__})")
        return {"status": "error"}
    kind = _image_kind(data)
    if kind is None:
        logger.warning("Telegram answered a map picture request with no JPEG or PNG")
        return {"status": "error"}
    extension, mime_type = kind
    try:
        os.makedirs(chat_dir, exist_ok=True)
        path = os.path.join(chat_dir, stem + extension)
        if os.path.exists(path):
            # Written meanwhile by another writer: the same picture.
            return _saved(path, mime_type)
        task_id = id(asyncio.current_task()) if asyncio.current_task() else 0
        temporary = f"{path}.{os.getpid()}.{task_id}.part"
        with open(temporary, "wb") as handle:
            handle.write(data)
        if os.path.exists(path):
            os.remove(temporary)
            return _saved(path, mime_type)
        landed = finalize_atomic_download(None, temporary, path)
    except OSError as e:
        logger.warning(f"Could not store a map picture ({type(e).__name__})")
        return {"status": "error"}
    if landed is None:
        return {"status": "error"}
    return _saved(landed, mime_type)
