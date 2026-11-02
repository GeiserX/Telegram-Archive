"""#611: the listener resumes from the update state saved in the session file.

Built on a real TelegramClient over a temporary SQLiteSession that never
connects, so the Telethon internals the helper relies on are the real ones.
"""

import asyncio
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import telethon
from telethon import TelegramClient
from telethon._updates import SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon._updates.messagebox import State as BoxState
from telethon.sessions import SQLiteSession
from telethon.tl import types
from telethon.tl.types.updates import (
    ChannelDifferenceEmpty,
    ChannelDifferenceTooLong,
    DifferenceEmpty,
    DifferenceTooLong,
    State,
)

from telegram_archive import update_state
from telegram_archive.update_state import remember_saved_state, resume_saved_state

CHANNEL_ID = 123
CHANNEL_HASH = 4242
SAVED_DATE = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _state(pts: int) -> State:
    return State(pts=pts, qts=0, date=SAVED_DATE, seq=7, unread_count=0)


def _seed_session(path: str, *, account_pts: int | None = 500) -> None:
    session = SQLiteSession(path)
    if account_pts is not None:
        session.set_update_state(0, _state(account_pts))
        session.set_update_state(CHANNEL_ID, _state(40))
        session.process_entities([types.InputPeerChannel(CHANNEL_ID, CHANNEL_HASH)])
    session.save()
    session.close()


def _client(path: str) -> TelegramClient:
    return TelegramClient(path, 12345, "0123456789abcdef0123456789abcdef")


def _simulate_on_login(client: TelegramClient, pts: int = 900) -> None:
    """What Telethon's _on_login does without catch_up=True: the box jumps to now."""
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    client._message_box.load(SessionState(0, 0, False, pts, 0, now, 0, None), [])


@pytest.fixture
def session_path(tmp_path):
    return str(tmp_path / "account")


class TestRoundTrip:
    async def test_saved_state_is_loaded_after_login_reset(self, session_path, caplog):
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)

        with caplog.at_level(logging.INFO, logger="telegram_archive.update_state"):
            await resume_saved_state(client)

        box = client._message_box
        assert box.map[ENTRY_ACCOUNT].pts == 500
        assert box.map[CHANNEL_ID].pts == 40
        assert client._mb_entity_cache.get(CHANNEL_ID).hash == CHANNEL_HASH
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "account gap 400 pts" in lines[0]
        assert "1 channels (0 without access hash)" in lines[0]
        client.session.close()

    async def test_premise_without_resume_the_box_stays_at_now(self, session_path):
        """Premise: Telethon alone ignores the saved rows."""
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        assert CHANNEL_ID not in client._message_box.map
        client.session.close()

    async def test_resume_is_used_once_per_client(self, session_path):
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)
        await resume_saved_state(client)

        client._message_box.map[ENTRY_ACCOUNT].pts = 700
        await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 700
        client.session.close()

    async def test_state_is_read_before_connect_overwrites_it(self, session_path):
        """The stash survives the keepalive rewriting update_state after connect()."""
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        client.session.set_update_state(0, _state(900))
        _simulate_on_login(client)

        await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 500
        client.session.close()


class TestNothingToLoad:
    @pytest.mark.parametrize("account_pts", [None, 0])
    async def test_fresh_session_leaves_the_box_alone(self, session_path, account_pts, caplog):
        _seed_session(session_path, account_pts=account_pts)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)

        with caplog.at_level(logging.INFO, logger="telegram_archive.update_state"):
            await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        assert [r.getMessage() for r in caplog.records] == ["No saved update state, nothing to replay"]
        client.session.close()

    async def test_never_remembered_client_loads_nothing(self, session_path, caplog):
        _seed_session(session_path)
        client = _client(session_path)
        _simulate_on_login(client)

        with caplog.at_level(logging.INFO, logger="telegram_archive.update_state"):
            await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        assert [r.getMessage() for r in caplog.records] == ["No saved update state, nothing to replay"]
        client.session.close()

    async def test_difference_in_flight_is_waited_for_then_loaded(self, session_path, monkeypatch):
        monkeypatch.setattr(update_state, "_DIFF_WAIT_SECONDS", 0.01)
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)
        box = client._message_box
        box.getting_diff_for = {ENTRY_ACCOUNT}

        async def finish_the_difference():
            await asyncio.sleep(0.03)
            box.map[ENTRY_ACCOUNT].pts = 950  # what the in-flight difference fetched
            box.getting_diff_for = set()

        finisher = asyncio.create_task(finish_the_difference())
        await resume_saved_state(client)
        await finisher

        assert box.map[ENTRY_ACCOUNT].pts == 500
        client.session.close()

    async def test_difference_still_in_flight_keeps_the_stash_and_warns(self, session_path, monkeypatch, caplog):
        monkeypatch.setattr(update_state, "_DIFF_WAIT_SECONDS", 0)
        _seed_session(session_path)
        client = _client(session_path)
        await remember_saved_state(client)
        _simulate_on_login(client)
        client._message_box.getting_diff_for = {ENTRY_ACCOUNT}

        with caplog.at_level(logging.INFO, logger="telegram_archive.update_state"):
            await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (
                logging.WARNING,
                "Update difference still running; restart replay skipped, "
                "only SYNC_DELETIONS_EDITS=true recovers its edits and deletions",
            )
        ]

        # The next listener.connect() on this client retries with the same rows.
        client._message_box.getting_diff_for = set()
        await resume_saved_state(client)
        assert client._message_box.map[ENTRY_ACCOUNT].pts == 500
        client.session.close()


class TestFallback:
    async def test_read_failure_warns_with_the_type_name_only(self, caplog):
        client = MagicMock()
        client.session.get_update_states.side_effect = OSError("disk /secret/path")

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            await remember_saved_state(client)

        assert [r.getMessage() for r in caplog.records] == [
            "Could not read saved update state (OSError); live updates only"
        ]

    async def test_load_failure_warns_and_listener_still_catches_up(self, caplog):
        from test_listener import _catch_up_config, _catch_up_db, _RecordingClient

        from telegram_archive.listener import TelegramListener

        calls: list = []
        client = _RecordingClient(calls)
        client.session = MagicMock()
        client.session.get_update_states.return_value = [(0, _state(500))]
        client._message_box = MagicMock(getting_diff_for=set(), map={})
        client._message_box.load.side_effect = RuntimeError("boom")
        await remember_saved_state(client)
        listener = TelegramListener(_catch_up_config(), _catch_up_db(), client=client, account_id=1)

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            await listener.connect()

        assert calls[-1] == "catch_up"
        assert [r.getMessage() for r in caplog.records if r.name == "telegram_archive.update_state"] == [
            "Could not load saved update state (RuntimeError); live updates only"
        ]


class TestTooLongWarning:
    def _client_with_channel(self, session_path):
        client = _client(session_path)
        _simulate_on_login(client)
        client._message_box.map[CHANNEL_ID] = BoxState(pts=40, deadline=0)
        update_state._watch_too_long(client._message_box)
        return client

    def _request(self):
        return MagicMock(channel=types.InputChannel(CHANNEL_ID, CHANNEL_HASH))

    def test_channel_too_long_logs_one_warning_without_the_channel_id(self, session_path, caplog):
        client = self._client_with_channel(session_path)
        diff = ChannelDifferenceTooLong(
            dialog=MagicMock(pts=400), messages=[], chats=[], users=[], final=True, timeout=None
        )

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            client._message_box.apply_channel_difference(self._request(), diff, client._mb_entity_cache)

        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "pts 40→400" in lines[0]
        assert str(CHANNEL_ID) not in lines[0]
        assert client._message_box.map[CHANNEL_ID].pts == 400
        client.session.close()

    def test_control_an_ordinary_difference_logs_nothing(self, session_path, caplog):
        client = self._client_with_channel(session_path)
        client._message_box.getting_diff_for.add(CHANNEL_ID)
        diff = ChannelDifferenceEmpty(pts=41, final=True)

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            client._message_box.apply_channel_difference(self._request(), diff, client._mb_entity_cache)

        assert caplog.records == []
        client.session.close()

    def _client_with_account_diff(self, session_path):
        client = _client(session_path)
        _simulate_on_login(client, pts=500)
        update_state._watch_too_long(client._message_box)
        client._message_box.getting_diff_for = {ENTRY_ACCOUNT}
        return client

    def test_account_too_long_logs_one_warning(self, session_path, caplog):
        client = self._client_with_account_diff(session_path)

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            client._message_box.apply_difference(DifferenceTooLong(pts=900), client._mb_entity_cache)

        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1
        assert "account replay as too long (pts 500→900)" in lines[0]
        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        client.session.close()

    def test_control_an_ordinary_account_difference_logs_nothing(self, session_path, caplog):
        client = self._client_with_account_diff(session_path)

        with caplog.at_level(logging.WARNING, logger="telegram_archive.update_state"):
            client._message_box.apply_difference(DifferenceEmpty(date=SAVED_DATE, seq=8), client._mb_entity_cache)

        assert caplog.records == []
        assert client._message_box.map[ENTRY_ACCOUNT].pts == 500
        client.session.close()

    def test_wraps_only_once(self, session_path):
        client = self._client_with_channel(session_path)
        wrapped = client._message_box.apply_channel_difference
        update_state._watch_too_long(client._message_box)
        assert type(client._message_box) is update_state._WatchedMessageBox
        assert client._message_box.apply_channel_difference == wrapped
        client.session.close()


class TestTelethonContract:
    """Fails loudly if Telethon changes the private API this module relies on."""

    def test_private_api_is_still_there(self, session_path):
        import inspect

        import telethon._updates as updates

        client = _client(session_path)
        box = client._message_box
        assert callable(box.load)
        assert isinstance(box.getting_diff_for, set)
        assert callable(client._mb_entity_cache.put)
        assert list(inspect.signature(box.apply_channel_difference).parameters) == [
            "request",
            "diff",
            "chat_hashes",
        ]
        assert list(inspect.signature(box.apply_difference).parameters) == ["diff", "chat_hashes"]
        for name in ("SessionState", "ChannelState", "Entity", "EntityType"):
            assert hasattr(updates, name)
        client.session.close()

    def test_telethon_version_is_the_one_this_was_checked_against(self):
        assert telethon.__version__ == "1.45.0", (
            "Telethon changed: re-read telegram_archive/update_state.py against the new "
            "connect(), _on_login and MessageBox.load before bumping this pin (#611)"
        )


class TestTelethonInternalsMissing:
    async def test_replay_is_disabled_with_one_warning(self, session_path, monkeypatch, caplog):
        """A Telethon without the private names: no crash, one WARNING, nothing loaded."""
        monkeypatch.setattr(update_state, "MessageBox", None)
        monkeypatch.setattr(update_state, "_unavailable_warned", False)
        _seed_session(session_path)
        client = _client(session_path)

        with caplog.at_level(logging.INFO, logger="telegram_archive.update_state"):
            await remember_saved_state(client)
            await remember_saved_state(client)
            _simulate_on_login(client)
            await resume_saved_state(client)

        assert client._message_box.map[ENTRY_ACCOUNT].pts == 900
        assert [r.getMessage() for r in caplog.records] == [
            "Telethon internals changed; restart replay disabled, live updates only"
        ]
        client.session.close()


class TestOnlyListenerClientsLoad:
    def test_client_kwargs_never_ask_telethon_to_catch_up(self):
        from telegram_archive.config import Config, build_telegram_client_kwargs

        config = MagicMock(telegram_device_model="Test Device A", telegram_proxy=None)
        assert "catch_up" not in Config.get_telegram_client_kwargs(config)
        assert "catch_up" not in build_telegram_client_kwargs()

    async def test_connection_remembers_on_build_not_on_revive(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from test_connection import _wire_default_account

        from telegram_archive.connection import TelegramConnection

        config = MagicMock()
        config.session_path = str(tmp_path / "s")
        (tmp_path / "s.session").touch()
        config.api_id = 12345
        config.api_hash = "abcdef"
        config.get_telegram_client_kwargs.return_value = {}
        _wire_default_account(config)
        conn = TelegramConnection(config)

        calls: list[str] = []
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock(side_effect=lambda: calls.append("client.connect"))
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.get_me = AsyncMock(return_value=MagicMock())
        mock_client.session = MagicMock()
        mock_client.session._conn = None

        with (
            patch("telegram_archive.connection.TelegramClient", return_value=mock_client),
            patch(
                "telegram_archive.connection.remember_saved_state",
                new_callable=AsyncMock,
                side_effect=lambda client: calls.append("remember"),
            ) as remember,
            patch.object(TelegramConnection, "_session_has_auth", return_value=False),
        ):
            await conn.connect()
            # The saved state is read before connect() lets the keepalive overwrite it.
            assert calls[:2] == ["remember", "client.connect"]
            conn._connected = False  # the socket died; the next connect revives in place
            await conn.connect()

        remember.assert_awaited_once_with(mock_client)
        assert calls.count("remember") == 1
