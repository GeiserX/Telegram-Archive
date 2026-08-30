"""A media row is identified by its message, not by a string that spells its type.

``Media.id`` used to be minted on every capture from ``{chat}_{msg}_{type}``, so
it cached a JUDGEMENT and was then used as the row's identity. The moment the
judgement changed -- which is exactly what round-video classification does to
every archived circular video -- the writer stopped talking about the row it
already had:

* the pending retry inserted a SECOND row under the new type and left the first
  at ``downloaded=0`` with its attempt counter untouched, so it was re-requested
  from Telegram on every cycle and could never reach the attempt cap;
* the gallery showed one file as two tiles while the message timeline kept
  showing the old one.

The id is now an opaque token the row keeps for life. Only ``type`` is
corrected, which is the value every reader consults.
"""

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_identity_"))

from tests.test_telegram_backup_extended import _run  # noqa: E402

CHAT_ID = -1001
MSG_ID = 42
BASE_DATE = datetime(2026, 3, 1, 12, 0, 0)


async def _seed(adapter, chat_id: int, message_id: int) -> None:
    """A media row needs its message: PostgreSQL enforces the foreign key that
    SQLite leaves off, so seeding media alone passes on one backend and fails on
    the other."""
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "fixture"}, account_id=1)
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": 4242,
            "date": BASE_DATE,
            "text": None,
            "raw_data": {},
        },
        account_id=1,
    )


class TestRetypeInPlace:
    """The rescan's write. It must move rows, not create them."""

    async def test_retype_moves_the_existing_row(self, real_adapter):
        await _seed(real_adapter, CHAT_ID, MSG_ID)
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_{MSG_ID}_video",
                "message_id": MSG_ID,
                "chat_id": CHAT_ID,
                "type": "video",
                "file_path": f"{CHAT_ID}/x.mp4",
                "file_name": "x.mp4",
                "file_size": 1,
                "downloaded": True,
            },
            account_id=1,
        )

        moved = await real_adapter.retype_media_for_messages(CHAT_ID, [MSG_ID], "video_note", account_id=1)

        assert moved == 1
        row = await real_adapter.get_media_for_message(CHAT_ID, MSG_ID, "video_note", account_id=1)
        assert row is not None
        # SAME row: the id it was filed under is untouched.
        assert row["id"] == f"{CHAT_ID}_{MSG_ID}_video"
        # and the old type resolves to nothing, so there is exactly one row
        assert await real_adapter.get_media_for_message(CHAT_ID, MSG_ID, "video", account_id=1) is None

    async def test_retype_is_idempotent(self, real_adapter):
        await _seed(real_adapter, CHAT_ID, 7)
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_7_video",
                "message_id": 7,
                "chat_id": CHAT_ID,
                "type": "video_note",
                "file_path": f"{CHAT_ID}/y.mp4",
                "file_name": "y.mp4",
                "file_size": 1,
                "downloaded": True,
            },
            account_id=1,
        )

        assert await real_adapter.retype_media_for_messages(CHAT_ID, [7], "video_note", account_id=1) == 0

    async def test_an_empty_id_list_touches_nothing(self, real_adapter):
        assert await real_adapter.retype_media_for_messages(CHAT_ID, [], "video_note", account_id=1) == 0

    async def test_candidate_chats_are_those_holding_that_type(self, real_adapter):
        for chat, mtype in ((-2001, "video"), (-2002, "photo")):
            await _seed(real_adapter, chat, 1)
            await real_adapter.insert_media(
                {
                    "id": f"{chat}_1_{mtype}",
                    "message_id": 1,
                    "chat_id": chat,
                    "type": mtype,
                    "file_path": f"{chat}/z",
                    "file_name": "z",
                    "file_size": 1,
                    "downloaded": True,
                },
                account_id=1,
            )

        chats = await real_adapter.get_chats_with_media_type("video", account_id=1)

        assert -2001 in chats
        assert -2002 not in chats


class TestReconcileKeepsOneRow:
    async def test_a_changed_judgement_does_not_create_a_second_row(self, real_adapter):
        """The whole point. Before this, a reclassified round video became a new
        row and the original stayed pending forever."""
        await _seed(real_adapter, CHAT_ID, 9)
        await real_adapter.insert_media(
            {
                "id": f"{CHAT_ID}_9_video",
                "message_id": 9,
                "chat_id": CHAT_ID,
                "type": "video",
                "file_path": f"{CHAT_ID}/a.mp4",
                "file_name": "a.mp4",
                "file_size": 1,
                "downloaded": False,
            },
            account_id=1,
        )

        reconciled = await real_adapter.reconcile_media_row(CHAT_ID, 9, "video_note", account_id=1)
        # the writer now upserts under THIS id, so the row is updated not twinned
        await real_adapter.insert_media({**reconciled, "downloaded": True, "file_size": 2}, account_id=1)

        counts = await real_adapter.get_media_counts(CHAT_ID, account_id=1)
        assert counts == {"video_note": 1}, counts


class TestReclassifyRoundVideos:
    """The rescan. It asks Telegram which messages are round and corrects those
    rows in place: no download, no re-key, no deletion."""

    def _backup(self, *, chats, found):
        from unittest.mock import AsyncMock, MagicMock

        from src.telegram_backup import TelegramBackup

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.account_id = 1
        backup.config = MagicMock()
        backup.db = AsyncMock()
        backup.db.get_chats_with_media_type = AsyncMock(return_value=list(chats))
        backup.db.retype_media_for_messages = AsyncMock(side_effect=lambda c, ids, t, **kw: len(ids))
        backup.client = MagicMock()
        backup.client.get_entity = AsyncMock(side_effect=lambda c: f"entity{c}")

        async def _iter(client, entity, **kwargs):
            for mid in found.get(entity, []):
                yield MagicMock(id=mid)

        self._iter = _iter
        return backup

    def _run_it(self, backup, **kwargs):
        import src.telegram_backup as mod

        original = mod.iter_messages_with_flood_retry
        mod.iter_messages_with_flood_retry = self._iter
        try:
            return _run(backup.reclassify_round_videos(**kwargs))
        finally:
            mod.iter_messages_with_flood_retry = original

    def test_it_retypes_only_the_messages_telegram_calls_round(self):
        backup = self._backup(chats=[-10, -20], found={"entity-10": [5, 9], "entity-20": []})

        summary = self._run_it(backup)

        assert summary["chats_scanned"] == 2
        assert summary["round_videos_found"] == 2
        assert summary["rows_retyped"] == 2
        backup.db.retype_media_for_messages.assert_awaited_once_with(-10, [5, 9], "video_note", account_id=1)

    def test_a_chat_with_no_round_videos_writes_nothing(self):
        """The cheap case, and the common one: a filtered search that matches
        nothing costs one request and must not touch the database."""
        backup = self._backup(chats=[-20], found={"entity-20": []})

        summary = self._run_it(backup)

        assert summary["round_videos_found"] == 0
        backup.db.retype_media_for_messages.assert_not_awaited()

    def test_dry_run_reports_without_writing(self):
        backup = self._backup(chats=[-10], found={"entity-10": [5]})

        summary = self._run_it(backup, dry_run=True)

        assert summary["round_videos_found"] == 1
        assert summary["rows_retyped"] == 0
        backup.db.retype_media_for_messages.assert_not_awaited()

    def test_one_unreachable_chat_does_not_abort_the_rest(self):
        from unittest.mock import AsyncMock

        backup = self._backup(chats=[-10, -20], found={"entity-20": [7]})
        backup.client.get_entity = AsyncMock(
            side_effect=lambda c: (_ for _ in ()).throw(ValueError("nope")) if c == -10 else f"entity{c}"
        )

        summary = self._run_it(backup)

        assert summary["errors"] == 1
        assert summary["rows_retyped"] == 1  # the reachable chat still ran

    def test_a_chat_id_scopes_the_run_and_skips_the_candidate_query(self):
        backup = self._backup(chats=[-10, -20], found={"entity-99": [1]})

        summary = self._run_it(backup, chat_id=-99)

        assert summary["chats_scanned"] == 1
        backup.db.get_chats_with_media_type.assert_not_awaited()


class TestReclassifyRunnerAccountHandling:
    """The CLI runner. It resolves an account per configured account, exactly
    like run_backup and run_fill_gaps -- passing neither an account_id nor a
    resolver is what made the command die before it reached Telegram, and no
    unit test caught it because they all build the backup with __new__."""

    def _config(self, n_accounts):
        from unittest.mock import MagicMock

        config = MagicMock()
        config.accounts = [MagicMock(index=i) for i in range(1, n_accounts + 1)]
        config.for_account = MagicMock(side_effect=lambda i: config)
        return config

    def _patch(self, monkeypatch, summaries):
        """Stand in for TelegramBackup.create so the account plumbing is what is
        under test, not Telethon."""
        from unittest.mock import AsyncMock, MagicMock

        import src.telegram_backup as mod

        calls = []

        async def _create(cfg, **kwargs):
            calls.append(kwargs)
            backup = MagicMock()
            backup.connect = AsyncMock()
            backup.disconnect = AsyncMock()
            backup.db = MagicMock(close=AsyncMock())
            outcome = summaries[len(calls) - 1]
            if isinstance(outcome, Exception):
                backup.reclassify_round_videos = AsyncMock(side_effect=outcome)
            else:
                backup.reclassify_round_videos = AsyncMock(return_value=outcome)
            return backup

        monkeypatch.setattr(mod.TelegramBackup, "create", _create)
        return calls

    def test_each_account_gets_a_resolver(self, monkeypatch):
        from src.telegram_backup import run_reclassify_round_videos

        calls = self._patch(
            monkeypatch, [{"chats_scanned": 1, "round_videos_found": 2, "rows_retyped": 2, "errors": 0}]
        )

        summary = _run(run_reclassify_round_videos(self._config(1)))

        assert summary["rows_retyped"] == 2
        # The bug this test exists for: neither of these may be missing.
        assert calls[0]["account"] is not None
        assert calls[0]["account_resolver"] is not None

    def test_summaries_are_summed_across_accounts(self, monkeypatch):
        from src.telegram_backup import run_reclassify_round_videos

        self._patch(
            monkeypatch,
            [
                {"chats_scanned": 1, "round_videos_found": 2, "rows_retyped": 2, "errors": 0},
                {"chats_scanned": 3, "round_videos_found": 1, "rows_retyped": 1, "errors": 0},
            ],
        )

        summary = _run(run_reclassify_round_videos(self._config(2)))

        assert summary == {"chats_scanned": 4, "round_videos_found": 3, "rows_retyped": 3, "errors": 0}

    def test_one_failing_account_does_not_take_the_other_down(self, monkeypatch):
        from src.telegram_backup import run_reclassify_round_videos

        self._patch(
            monkeypatch,
            [RuntimeError("boom"), {"chats_scanned": 1, "round_videos_found": 1, "rows_retyped": 1, "errors": 0}],
        )

        summary = _run(run_reclassify_round_videos(self._config(2)))

        assert summary["errors"] == 1
        assert summary["rows_retyped"] == 1

    def test_a_single_account_failure_propagates(self, monkeypatch):
        """With one account there is nothing to shield, so the caller sees it --
        the same rule run_backup and run_fill_gaps follow."""
        import pytest

        from src.telegram_backup import run_reclassify_round_videos

        self._patch(monkeypatch, [RuntimeError("boom")])

        with pytest.raises(RuntimeError, match="boom"):
            _run(run_reclassify_round_videos(self._config(1)))
