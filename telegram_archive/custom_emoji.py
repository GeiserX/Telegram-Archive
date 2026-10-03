"""The files of custom (premium) emoji, fetched once per document id.

A reaction made with a custom emoji is stored as ``custom_<document_id>`` and a
custom emoji in text keeps its ``document_id`` in the message entities. The
adapter adds a pending ``custom_emoji`` row the first time it sees an id
(``DatabaseAdapter._note_custom_emoji``); this module fetches the pending rows.

What it copies from the official apps:

* one store keyed by document id, shared by every chat and every account
  (Telegram Android keeps the Document by id in its own table, Web A in
  ``customEmojis.byId``);
* only unknown ids are asked for, 100 per ``messages.getCustomEmojiDocuments``
  call (``kMaxPerRequest`` in Telegram Desktop, ``min(100, remaining)`` in
  Telegram iOS);
* a file already stored is never written again (iOS ``storeMediaIfNotPresent``);
* an id the answer leaves out is asked again, but at most
  ``CUSTOM_EMOJI_MAX_ATTEMPTS`` times (Android asks without a bound).

The file goes to ``<media>/_emoji/<document_id>.<webp|tgs|webm>``. Nothing is
logged but counts and error type names.
"""

import asyncio
import logging
import os

from telethon.errors import FloodPremiumWaitError, FloodWaitError

from .message_utils import (
    CUSTOM_EMOJI_DIR,
    custom_emoji_file_name,
    finalize_atomic_download,
    utcnow_naive,
)

logger = logging.getLogger(__name__)

# Ids per getCustomEmojiDocuments call: Telegram Desktop's and Telegram iOS's number.
CUSTOM_EMOJI_BATCH = 100
# Files per run and account, the map picture backfill's number.
CUSTOM_EMOJI_MAX_PER_RUN = 500
# Fetches that find nothing before a row gets its skip reason.
CUSTOM_EMOJI_MAX_ATTEMPTS = 3
# A custom emoji is a 100x100 sticker: tens of KB. The cap only stops a file that is not one.
CUSTOM_EMOJI_MAX_BYTES = 512 * 1024
CUSTOM_EMOJI_DOWNLOAD_TIMEOUT_SECONDS = 60
CUSTOM_EMOJI_PAUSE_SECONDS = 1.0


def document_meta(doc: object) -> dict:
    """What a Document says about the emoji: mime type, size, width, height, alt and text colour."""
    meta: dict = {"mime_type": None, "size": None, "width": None, "height": None, "alt": None, "text_color": 0}
    mime_type = getattr(doc, "mime_type", None)
    if isinstance(mime_type, str):
        meta["mime_type"] = mime_type.lower()
    size = getattr(doc, "size", None)
    if isinstance(size, int) and not isinstance(size, bool):
        meta["size"] = size
    for attribute in getattr(doc, "attributes", None) or []:
        name = type(attribute).__name__
        if name in ("DocumentAttributeImageSize", "DocumentAttributeVideo"):
            width, height = getattr(attribute, "w", None), getattr(attribute, "h", None)
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                meta["width"], meta["height"] = width, height
        elif name == "DocumentAttributeCustomEmoji":
            alt = getattr(attribute, "alt", None)
            if isinstance(alt, str) and alt:
                meta["alt"] = alt[:64]
            meta["text_color"] = 1 if getattr(attribute, "text_color", False) is True else 0
    return meta


def _empty_counts() -> dict:
    return {
        "asked": 0,
        "saved": 0,
        "present": 0,
        "unavailable": 0,
        "unsupported": 0,
        "oversize": 0,
        "failed": 0,
        "deferred": 0,
        "flood_wait_seconds": 0,
    }


class _Flood(Exception):
    def __init__(self, seconds: int) -> None:
        super().__init__(seconds)
        self.seconds = seconds


async def _download(client, call, doc: object, path: str) -> str | None:
    """Download one document to ``path`` without ever replacing a file; the landed path or None."""
    task = asyncio.current_task()
    temporary = f"{path}.{os.getpid()}.{id(task) if task else 0}.part"
    try:
        result = await asyncio.wait_for(
            call(client.download_media, doc, file=temporary), timeout=CUSTOM_EMOJI_DOWNLOAD_TIMEOUT_SECONDS
        )
    except (FloodWaitError, FloodPremiumWaitError) as e:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise _Flood(int(getattr(e, "seconds", 0) or 0)) from None
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    actual = result if isinstance(result, str) else None
    if os.path.exists(path):
        # Written meanwhile by another writer: the same file. Ours goes.
        for leftover in {temporary, actual}:
            if leftover and leftover != path and os.path.exists(leftover):
                os.remove(leftover)
        return path
    landed = finalize_atomic_download(actual, temporary, path)
    if landed is None or os.path.getsize(landed) <= 0:
        return None
    return landed


async def fetch_custom_emoji(client, db, media_root: str, *, call, limit: int = CUSTOM_EMOJI_MAX_PER_RUN) -> dict:
    """Fetch the files of up to ``limit`` pending custom emoji; counts, never raises.

    ``call`` is the flood-retry wrapper (``telegram_backup.call_with_flood_retry``):
    the client sleeps no FloodWait itself. A FloodWait longer than it sleeps out
    stops the step for this run (``flood_wait_seconds``) and counts no attempt.
    """
    from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

    counts = _empty_counts()
    try:
        pending = await db.get_pending_custom_emoji(limit, CUSTOM_EMOJI_MAX_ATTEMPTS)
        if not pending:
            return counts
        counts["deferred"] = max(0, await db.count_pending_custom_emoji(CUSTOM_EMOJI_MAX_ATTEMPTS) - len(pending))
        folder = os.path.join(media_root, CUSTOM_EMOJI_DIR)
        os.makedirs(folder, exist_ok=True)
        for start in range(0, len(pending), CUSTOM_EMOJI_BATCH):
            chunk = pending[start : start + CUSTOM_EMOJI_BATCH]
            if start:
                await asyncio.sleep(CUSTOM_EMOJI_PAUSE_SECONDS)
            counts["asked"] += len(chunk)
            try:
                documents = await call(client, GetCustomEmojiDocumentsRequest(document_id=chunk))
            except (FloodWaitError, FloodPremiumWaitError) as e:
                counts["asked"] -= len(chunk)
                counts["deferred"] += len(pending) - start
                counts["flood_wait_seconds"] = int(getattr(e, "seconds", 0) or 0) or 1
                break
            except Exception as e:
                logger.warning(f"Could not ask for custom emoji ({type(e).__name__})")
                for document_id in chunk:
                    if await db.count_custom_emoji_attempt(document_id, "failed", CUSTOM_EMOJI_MAX_ATTEMPTS):
                        counts["failed"] += 1
                continue
            answered = {getattr(doc, "id", None): doc for doc in documents or [] if type(doc).__name__ == "Document"}
            handled = 0
            try:
                for document_id in chunk:
                    doc = answered.get(document_id)
                    if doc is None:
                        if await db.count_custom_emoji_attempt(document_id, "unavailable", CUSTOM_EMOJI_MAX_ATTEMPTS):
                            counts["unavailable"] += 1
                    else:
                        await _store_one(client, call, db, folder, document_id, doc, counts)
                    handled += 1
            except _Flood as flood:
                # The id that met the FloodWait and every later one stay pending.
                counts["deferred"] += len(pending) - start - handled
                counts["flood_wait_seconds"] = flood.seconds or 1
                break
    except Exception as e:
        logger.warning(f"Custom emoji fetch stopped ({type(e).__name__}); the rest wait for the next run")
    return counts


async def _store_one(client, call, db, folder: str, document_id: int, doc: object, counts: dict) -> None:
    meta = document_meta(doc)
    known = {
        "mime_type": meta["mime_type"],
        "width": meta["width"],
        "height": meta["height"],
        "alt": meta["alt"],
        "text_color": meta["text_color"],
    }
    file_name = custom_emoji_file_name(document_id, meta["mime_type"])
    if file_name is None:
        await db.update_custom_emoji(document_id, {**known, "skip_reason": "unsupported"})
        counts["unsupported"] += 1
        return
    if meta["size"] is not None and meta["size"] > CUSTOM_EMOJI_MAX_BYTES:
        await db.update_custom_emoji(document_id, {**known, "skip_reason": "oversize"})
        counts["oversize"] += 1
        return
    path = os.path.join(folder, file_name)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        # Kept by an earlier run or a merge: marked, never written again.
        await db.update_custom_emoji(
            document_id, {**known, "file_name": file_name, "downloaded": 1, "download_date": utcnow_naive()}
        )
        counts["present"] += 1
        return
    try:
        landed = await _download(client, call, doc, path)
    except _Flood:
        raise
    except Exception as e:  # a timeout, a network error, a full disk
        logger.warning(f"Could not download a custom emoji ({type(e).__name__})")
        landed = None
    if landed is None:
        await db.update_custom_emoji(document_id, known)
        if await db.count_custom_emoji_attempt(document_id, "failed", CUSTOM_EMOJI_MAX_ATTEMPTS):
            counts["failed"] += 1
        return
    await db.update_custom_emoji(
        document_id, {**known, "file_name": file_name, "downloaded": 1, "download_date": utcnow_naive()}
    )
    counts["saved"] += 1
