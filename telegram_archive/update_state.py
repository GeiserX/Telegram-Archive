"""Resume the listener from the update state saved in the session file (#611).

Telethon writes the account's and every channel's pts (Telegram's update
counter) to the session file's ``update_state`` table, but only reads it back
when the client is built with ``catch_up=True``. That flag fetches the missed
updates inside ``connect()``, before the archive attaches its handlers, so the
replay would reach no handler and be lost. Instead the saved rows are read
right after the client is built (the keepalive overwrites them within 60 s of
``connect()``) and loaded into Telethon's in-memory MessageBox by
``listener.connect()``, after the handlers are attached and just before its
``catch_up()`` call.

This touches Telethon's private ``_message_box`` and ``_mb_entity_cache``;
``tests/test_update_state.py`` pins the parts it relies on. Every step falls
back to live updates only on any error, a Telethon that no longer has these
internals included: the imports are guarded, so the rest of the archive still
starts.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import weakref

from telethon import utils

from .message_utils import chat_id_for_log, chat_title_for_log

try:
    from telethon._updates import ChannelState, Entity, EntityType, SessionState
    from telethon._updates.messagebox import ENTRY_ACCOUNT, MessageBox
    from telethon.tl.types.updates import ChannelDifferenceTooLong, DifferenceTooLong
except ImportError:  # a Telethon whose private layout moved: replay off, archive still starts
    MessageBox = None

logger = logging.getLogger(__name__)

_unavailable_warned = False

# How long resume_saved_state() waits for a difference already in flight
# before it gives up for this connect: _DIFF_WAIT_STEPS * _DIFF_WAIT_SECONDS.
_DIFF_WAIT_STEPS = 50
_DIFF_WAIT_SECONDS = 0.2

# When that wait runs out, a background task keeps waiting up to
# _RETRY_WAIT_STEPS * _RETRY_WAIT_SECONDS (10 minutes), then loads the state
# and calls client.catch_up() itself.
_RETRY_WAIT_STEPS = 600
_RETRY_WAIT_SECONDS = 1.0

# Strong references, so a waiting retry task is not garbage collected.
_retries: set = set()

# client -> the update_state rows read before its first connect(). Popped by
# resume_saved_state(), so each client object loads them at most once.
_pending: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

# session file name -> rows a deferred replay could not load before its client
# disconnected. An owned listener builds a new client on its next connect(),
# and the old client's disconnect writes its newer in-memory state to the
# session file, so remember_saved_state() takes these rows instead.
_deferred: dict = {}


def _session_key(client):
    name = getattr(getattr(client, "session", None), "filename", None)
    return name if isinstance(name, str) and name != ":memory:" else None


async def remember_saved_state(client) -> None:
    """Stash the session's saved update state for a later resume_saved_state().

    Call right after ``TelegramClient(...)`` and BEFORE ``connect()``: once
    connected, Telethon's keepalive overwrites ``update_state`` with the live
    state within 60 s.
    """
    global _unavailable_warned
    if MessageBox is None:
        if not _unavailable_warned:
            _unavailable_warned = True
            logger.warning("Telethon internals changed; restart replay disabled, live updates only")
        return
    try:
        # Read, not popped: if this client fails to connect, the next one still
        # needs them. resume_saved_state() clears them once they reach replay.
        deferred = _deferred.get(_session_key(client))
        if deferred is not None:
            _pending[client] = deferred
            return
        _pending[client] = list(await utils.maybe_async(client.session.get_update_states()))
    except Exception as e:
        logger.warning(f"Could not read saved update state ({type(e).__name__}); live updates only")


def _age(delta: datetime.timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# What a dropped replay costs, said once for every warning about one.
_TOO_LONG_TAIL = (
    "new messages arrive with the next backup; edits and deletions in the gap only with "
    "SYNC_DELETIONS_EDITS=true within SYNC_DELETIONS_EDITS_DAYS; a message posted and deleted "
    "in the gap is lost"
)


class _WatchedMessageBox(MessageBox or object):
    """MessageBox that logs a WARNING whenever Telegram answers a difference with TooLong.

    Telethon then jumps pts to the current value and returns no updates, so the
    gap is dropped in silence. MessageBox uses ``__slots__``, so its methods
    cannot be replaced on the instance; an empty-slots subclass keeps the same
    layout and lets ``_watch_too_long`` swap the instance's class.
    """

    __slots__ = ()

    # The listener's config, for the LOG_CHAT_TITLES gate. None names nothing.
    # A class attribute: the slots leave no room on the instance, so
    # _watch_too_long gives each config its own empty-slots subclass.
    _log_config = None

    def apply_difference(self, diff, chat_hashes):
        if isinstance(diff, DifferenceTooLong):
            before = self.map.get(ENTRY_ACCOUNT)
            logger.warning(
                f"Telegram dropped the account replay as too long "
                f"(pts {before.pts if before else '?'}→{diff.pts}); {_TOO_LONG_TAIL}"
            )
        return super().apply_difference(diff, chat_hashes)

    def apply_channel_difference(self, request, diff, chat_hashes):
        if isinstance(diff, ChannelDifferenceTooLong):
            # Names the channel only when the operator opted in (LOG_CHAT_TITLES);
            # off, the line carries no id and no title.
            before = self.map.get(request.channel.channel_id)
            entity = next((c for c in diff.chats if getattr(c, "id", None) == request.channel.channel_id), None)
            logger.warning(
                f"Telegram dropped a channel's replay as too long"
                f"{chat_title_for_log(entity, self._log_config)}"
                f"{chat_id_for_log(utils.get_peer_id(request.channel), self._log_config)} "
                f"(pts {before.pts if before else '?'}→{diff.dialog.pts}); {_TOO_LONG_TAIL}"
            )
        return super().apply_channel_difference(request, diff, chat_hashes)


def _watch_too_long(box, config=None) -> None:
    """Make ``box`` log TooLong answers. Idempotent; leaves anything but a plain MessageBox alone."""
    if MessageBox is not None and type(box) is MessageBox:
        if config is None:
            box.__class__ = _WatchedMessageBox
        else:
            box.__class__ = type("_WatchedMessageBox", (_WatchedMessageBox,), {"__slots__": (), "_log_config": config})


async def resume_saved_state(client, config=None) -> None:
    """Load the stashed update state into the client's MessageBox, once.

    Call after the handlers are attached and before ``client.catch_up()``, so
    the difference Telethon then fetches starts from the saved state and its
    updates reach the handlers. Logs one INFO line, or one WARNING on failure.
    If a difference is still running after the wait, the load moves to a
    background task that waits for it to finish and then catches up itself.
    ``config`` gates whether a channel's TooLong warning names the channel.
    """
    if MessageBox is None:
        return  # remember_saved_state() already warned
    rows = _pending.pop(client, None)
    _deferred.pop(_session_key(client), None)
    try:
        box = getattr(client, "_message_box", None)
        if box is not None:
            _watch_too_long(box, config)
        account = next((state for entity_id, state in rows or () if entity_id == 0), None)
        if account is None or account.pts == 0:
            logger.info("No saved update state, nothing to replay")
            return
        # A difference in flight would overwrite the older pts we load, so wait
        # for it. Loading after it only replays what it fetched a second time,
        # and the handlers are idempotent.
        for _ in range(_DIFF_WAIT_STEPS):
            if not box.getting_diff_for:
                break
            await asyncio.sleep(_DIFF_WAIT_SECONDS)
        else:
            if box.getting_diff_for:
                task = asyncio.create_task(_resume_when_idle(client, account, rows))
                _retries.add(task)
                task.add_done_callback(_retries.discard)
                logger.info("Update difference still running; restart replay deferred until it finishes")
                return
        await _load(client, box, account, rows)
    except Exception as e:
        logger.warning(f"Could not load saved update state ({type(e).__name__}); live updates only")


async def _resume_when_idle(client, account, rows) -> None:
    """Wait for the running difference to finish, then load the saved state and catch up."""
    try:
        for _ in range(_RETRY_WAIT_STEPS):
            if not client.is_connected():
                # The next listener.connect() retries: on this client if it is
                # shared, on a new one built over the same session if it is owned.
                _pending[client] = rows
                key = _session_key(client)
                if key is not None:
                    _deferred[key] = rows
                logger.info(
                    "Client disconnected before the update difference finished; replay waits for the next connect"
                )
                return
            box = client._message_box
            if not box.getting_diff_for:
                await _load(client, box, account, rows)
                await client.catch_up()
                return
            await asyncio.sleep(_RETRY_WAIT_SECONDS)
        logger.warning(f"Update difference still running after 10 minutes; restart replay skipped; {_TOO_LONG_TAIL}")
    except Exception as e:
        logger.warning(f"Could not load saved update state ({type(e).__name__}); live updates only")


async def _load(client, box, account, rows) -> None:
    """Load the saved rows into ``box`` and cache the channels' access hashes. Logs one INFO line."""
    current = box.map.get(ENTRY_ACCOUNT)
    channels = [ChannelState(entity_id, state.pts) for entity_id, state in rows if entity_id != 0]
    box.load(
        SessionState(0, 0, False, account.pts, account.qts, int(account.date.timestamp()), account.seq, None),
        channels,
    )
    # Mirrors telegrambaseclient.connect(): a channel difference needs the
    # channel's access hash.
    missing = 0
    hashed = []
    for channel in channels:
        try:
            entity = await utils.maybe_async(client.session.get_input_entity(channel.channel_id))
            client._mb_entity_cache.put(Entity(EntityType.CHANNEL, entity.channel_id, entity.access_hash))
            hashed.append(channel.channel_id)
        except ValueError, AttributeError:
            # AttributeError: the id resolved to a user, not a channel.
            missing += 1
    # box.load() defers each channel's difference 15 minutes, long enough for a
    # busy channel to go TooLong (#608); start them now, the account first so it
    # still runs before them. Hashless channels are skipped: Telethon would drop
    # their saved pts. If Telethon changed the call, keep its 15-minute default.
    begin = getattr(box, "try_begin_get_diff", None)
    if callable(begin):
        try:
            begin(ENTRY_ACCOUNT, "restored from session")
            for channel_id in hashed:
                begin(channel_id, "restored from session")
        except Exception as e:
            logger.debug(f"Could not start the channel replays at once ({type(e).__name__})")
    age = datetime.datetime.now(datetime.UTC) - account.date
    gap = current.pts - account.pts if current is not None else "?"
    logger.info(
        f"Catch-up: resuming from update state dated {_age(age)} ago, account gap {gap} pts, "
        f"{len(channels)} channels ({missing} without access hash)"
    )
