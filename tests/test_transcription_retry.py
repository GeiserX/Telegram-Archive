"""When the drain tries a failed media again: the reason of each failed row decides.

A failure about the file's content or the request counts, and three end the
retries. A failure about the archive's copy of the file (missing,
unreadable) or about the server (engine unavailable, models missing, a job
lost or expired, an answer that is not a job) does not count: once the disk
or the server is repaired, the next drain picks the media up on its own,
whatever rows it already holds. While the cause lasts nothing grows on
every drain: a file still missing writes no row, and a server failure with
no transcript finished since is retried by one probe a drain, not by every
media. Ten failed rows of any reason end a file the server keeps failing on.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select, update

from telegram_archive.db.models import MediaTranscript
from telegram_archive.message_utils import utcnow_naive
from telegram_archive.transcription import STALE_QUEUED, drain_transcriptions

sys.path.insert(0, os.path.dirname(__file__))

from test_transcription import (  # noqa: E402
    AUDIO,
    CHAT,
    FakeServer,
    _client,
    _config,
    _media,
    _rows,
)

OTHER = -100500600007
DAY = timedelta(days=1)


async def _fail(adapter, media_id: str, error: str | None, *, ago: timedelta = DAY) -> None:
    """One more failed row for ``media_id`` with ``error``, finished ``ago``."""
    row = await adapter.enqueue_media_transcript(media_id, account_id=1)
    await adapter.fill_media_transcript(
        row["id"], status="failed", error=error or "", completed_at=utcnow_naive() - ago
    )
    if error is None:
        # A row from before reasons were stored: its error stays NULL.
        async with adapter.db_manager.async_session_factory() as session:
            await session.execute(update(MediaTranscript).where(MediaTranscript.id == row["id"]).values(error=None))
            await session.commit()


async def _done(adapter, media_id: str, *, ago: timedelta = timedelta(0), copied_from_id: int | None = None) -> None:
    """A finished transcript for ``media_id``: the server answered, or with ``copied_from_id`` a copy."""
    row = await adapter.enqueue_media_transcript(media_id, account_id=1)
    await adapter.fill_media_transcript(
        row["id"], status="done", text="", completed_at=utcnow_naive() - ago, copied_from_id=copied_from_id
    )


async def _age(adapter, by: timedelta) -> None:
    """Move every row back by ``by``, as if that much time had passed."""
    async with adapter.db_manager.async_session_factory() as session:
        rows = (await session.execute(select(MediaTranscript))).scalars().all()
        for row in rows:
            row.requested_at -= by
            if row.completed_at is not None:
                row.completed_at -= by
        await session.commit()


async def _other_chat_media(adapter, tmp_path, media_id: str, *, download_date: datetime) -> None:
    path = tmp_path / str(OTHER) / f"{media_id}.ogg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(AUDIO)
    await adapter.upsert_chat({"id": OTHER, "type": "group", "title": "fixture chat"}, account_id=1)
    message_id = int(media_id.split("_")[1])
    await adapter.insert_message(
        {"id": message_id, "chat_id": OTHER, "text": "", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=1,
    )
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": OTHER,
            "type": "voice",
            "file_path": str(path),
            "downloaded": True,
            "duration": 12,
            "download_date": download_date,
        },
        account_id=1,
    )


async def _awaiting(adapter, per_run: int = 50, priority=()) -> list[str]:
    rows = await adapter.get_media_awaiting_transcription(
        account_id=1,
        types={"voice"},
        per_run=per_run,
        stale_before=utcnow_naive() - STALE_QUEUED,
        priority_chat_ids=priority,
    )
    return [row["id"] for row in rows]


async def _drain(config, adapter, server) -> dict:
    return await drain_transcriptions(
        config, adapter, account_id=1, notifier=AsyncMock(), client=_client(config, server)
    )


class BrokenServer(FakeServer):
    """A server that answers every upload with a page that is not JSON: ``invalid_json``, a server failure."""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/audio/transcriptions"):
            self.requests.append(request)
            return httpx.Response(200, text="<html>proxy error</html>")
        return super()._handle(request)


def _statuses(rows: list[dict]) -> list[tuple[str, str | None]]:
    return [(r["status"], r["error"]) for r in rows]


class TestRepairedEnvironment:
    async def test_three_file_missing_rows_are_sent_once_the_file_is_back(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        for _ in range(3):
            await _fail(real_adapter, "m_1_voice", "file_missing")
        server = FakeServer()
        stats = await _drain(_config(str(tmp_path)), real_adapter, server)
        assert stats["done"] == 1
        assert len(server.transcribe_requests) == 1
        # Newest first.
        assert _statuses(await _rows(real_adapter, "m_1_voice")) == [("done", None)] + [("failed", "file_missing")] * 3

    async def test_three_engine_unavailable_rows_are_sent_once_the_server_answers(self, real_adapter, tmp_path):
        """No transcript finished since they failed, so the media goes as the drain's probe."""
        await _media(real_adapter, tmp_path, "m_1_voice")
        for _ in range(3):
            await _fail(real_adapter, "m_1_voice", "engine_unavailable")
        server = FakeServer()
        stats = await _drain(_config(str(tmp_path)), real_adapter, server)
        assert stats["done"] == 1
        assert [r["status"] for r in await _rows(real_adapter, "m_1_voice")] == ["done"] + ["failed"] * 3

    async def test_every_server_failure_is_sent_once_the_server_finished_a_transcript_since(
        self, real_adapter, tmp_path
    ):
        """not_found, expired and the others: a finished transcript after them shows the server works."""
        reasons = (
            "engine_unavailable",
            "not_found",
            "expired",
            "models_missing",
            "model_download_failed",
            "invalid_job",
            "invalid_json",
        )
        for n, reason in enumerate(reasons, start=1):
            await _media(real_adapter, tmp_path, f"m_{n}_voice", content_hash=f"{n}" * 64)
            for _ in range(3):
                await _fail(real_adapter, f"m_{n}_voice", reason)
        await _media(real_adapter, tmp_path, "m_9_voice", content_hash="9" * 64)
        await _done(real_adapter, "m_9_voice")
        assert sorted(await _awaiting(real_adapter)) == [f"m_{n}_voice" for n in range(1, len(reasons) + 1)]

    async def test_three_content_failures_are_not_sent_again(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        await _media(real_adapter, tmp_path, "m_2_voice")
        for _ in range(3):
            await _fail(real_adapter, "m_1_voice", "decode_failed")
            await _fail(real_adapter, "m_2_voice", None)
        await _media(real_adapter, tmp_path, "m_9_voice", content_hash="9" * 64)
        await _done(real_adapter, "m_9_voice")
        server = FakeServer()
        stats = await _drain(_config(str(tmp_path)), real_adapter, server)
        assert stats["done"] == 0
        assert server.transcribe_requests == []
        assert len(await _rows(real_adapter, "m_1_voice")) == 3
        assert len(await _rows(real_adapter, "m_2_voice")) == 3


class TestStillBroken:
    async def test_a_file_still_missing_adds_no_row_on_repeated_drains(self, real_adapter, tmp_path):
        """An entry this process cannot read keeps its one file_missing row, drain after drain."""
        row = await _media(real_adapter, tmp_path, "m_1_voice", on_disk=False)
        os.makedirs(os.path.dirname(row["file_path"]), exist_ok=True)
        os.symlink("/nonexistent-object-store/m_1_voice.ogg", row["file_path"])
        for _ in range(3):
            await _fail(real_adapter, "m_1_voice", "file_missing")
        server = FakeServer()
        config = _config(str(tmp_path))
        for _ in range(3):
            stats = await _drain(config, real_adapter, server)
            assert stats["noop"] == 1
            assert stats["failed"] == 0
        assert server.transcribe_requests == []
        assert len(await _rows(real_adapter, "m_1_voice")) == 3
        # Once the link reaches a file again, the next drain sends it.
        os.unlink(row["file_path"])
        with open(row["file_path"], "wb") as handle:
            handle.write(AUDIO)
        assert (await _drain(config, real_adapter, server))["done"] == 1

    async def test_a_file_still_unreadable_adds_no_row(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice")
        await _fail(real_adapter, "m_1_voice", "file_unreadable")

        def denied(path):
            raise PermissionError("denied")

        server = FakeServer()
        config = _config(str(tmp_path))
        with patch("telegram_archive.transcription._file_sha256", side_effect=denied):
            for _ in range(2):
                assert (await _drain(config, real_adapter, server))["noop"] == 1
        assert len(await _rows(real_adapter, "m_1_voice")) == 1
        assert (await _drain(config, real_adapter, server))["done"] == 1

    async def test_a_still_broken_server_gets_one_probe_a_drain_not_one_row_per_media(self, real_adapter, tmp_path):
        for n in range(1, 4):
            await _media(real_adapter, tmp_path, f"m_{n}_voice", content_hash=f"{n}" * 64)
            for _ in range(3):
                await _fail(real_adapter, f"m_{n}_voice", "engine_unavailable")
        config = _config(str(tmp_path))
        broken = BrokenServer()
        probed = []
        for _ in range(3):
            assert (await _drain(config, real_adapter, broken))["failed"] == 1
            rows = {n: await _rows(real_adapter, f"m_{n}_voice") for n in range(1, 4)}
            probed.append(next(n for n, r in rows.items() if len(r) == 4 and n not in probed))
            assert sum(len(r) for r in rows.values()) == 9 + len(probed)
        assert sorted(probed) == [1, 2, 3]  # the probe goes to the one waiting longest
        # Every probe just failed, its fourth: the next waits four hours.
        assert (await _drain(config, real_adapter, broken))["failed"] == 0
        assert len(broken.transcribe_requests) == 3
        # Repaired: once the wait is over the probe finishes, and the drain after it sends the rest.
        await _age(real_adapter, timedelta(hours=5))
        server = FakeServer()
        assert (await _drain(config, real_adapter, server))["done"] == 1
        assert (await _drain(config, real_adapter, server))["done"] == 2

    async def test_a_copy_is_not_a_sign_the_server_works(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64)
        for media_id in ("m_1_voice", "m_2_voice"):
            await _fail(real_adapter, media_id, "engine_unavailable")
        await _media(real_adapter, tmp_path, "m_9_voice", content_hash="9" * 64)
        await _done(real_adapter, "m_9_voice", copied_from_id=1)
        assert len(await _awaiting(real_adapter)) == 1  # the probe only
        await _media(real_adapter, tmp_path, "m_8_voice", content_hash="8" * 64)
        await _done(real_adapter, "m_8_voice")
        assert sorted(await _awaiting(real_adapter)) == ["m_1_voice", "m_2_voice"]

    async def test_a_server_failure_newer_than_the_last_finished_transcript_waits_for_the_probe(
        self, real_adapter, tmp_path
    ):
        await _media(real_adapter, tmp_path, "m_9_voice", content_hash="9" * 64)
        await _done(real_adapter, "m_9_voice", ago=2 * DAY)
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _fail(real_adapter, "m_1_voice", "not_found", ago=timedelta(minutes=5))
        # One failure: the probe goes at once, as the old retry did.
        assert await _awaiting(real_adapter) == ["m_1_voice"]
        await _fail(real_adapter, "m_1_voice", "not_found", ago=timedelta(minutes=5))
        assert await _awaiting(real_adapter) == []  # two failures: one hour first
        await _age(real_adapter, timedelta(hours=2))
        assert await _awaiting(real_adapter) == ["m_1_voice"]


class TestBudgetAndOrder:
    async def test_a_file_failure_goes_after_its_priority_tier_and_the_tiers_still_hold(self, real_adapter, tmp_path):
        # In the priority chat: a restored file, newest download, and a new file.
        await _media(real_adapter, tmp_path, "m_1_voice", download_date=datetime(2026, 1, 5))
        await _fail(real_adapter, "m_1_voice", "file_missing")
        await _media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        # Elsewhere: the newest download of all.
        await _other_chat_media(real_adapter, tmp_path, "m_3_voice", download_date=datetime(2026, 1, 9))
        assert await _awaiting(real_adapter, priority=[CHAT]) == ["m_2_voice", "m_1_voice", "m_3_voice"]
        config = _config(str(tmp_path), transcription_backfill_per_run=1, transcription_priority_chat_ids=[CHAT])
        server = FakeServer()
        sent = []
        for _ in range(3):
            assert (await _drain(config, real_adapter, server))["done"] == 1
            for media_id in ("m_1_voice", "m_2_voice", "m_3_voice"):
                rows = await _rows(real_adapter, media_id)
                if media_id not in sent and rows and rows[0]["status"] == "done":
                    sent.append(media_id)
        assert sent == ["m_2_voice", "m_1_voice", "m_3_voice"]

    async def test_the_probe_stays_inside_the_per_run_budget(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64, download_date=datetime(2026, 1, 1))
        for _ in range(3):
            await _fail(real_adapter, "m_1_voice", "engine_unavailable")
        await _media(real_adapter, tmp_path, "m_2_voice", content_hash="2" * 64, download_date=datetime(2026, 1, 2))
        assert await _awaiting(real_adapter, per_run=1) == ["m_2_voice"]
        assert await _awaiting(real_adapter, per_run=2) == ["m_2_voice", "m_1_voice"]

    async def test_the_probe_follows_the_priority_order(self, real_adapter, tmp_path):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        await _fail(real_adapter, "m_1_voice", "engine_unavailable", ago=DAY)
        await _other_chat_media(real_adapter, tmp_path, "m_2_voice", download_date=datetime(2026, 1, 1))
        await _fail(real_adapter, "m_2_voice", "engine_unavailable", ago=2 * DAY)
        assert await _awaiting(real_adapter) == ["m_2_voice"]  # longest waiting
        assert await _awaiting(real_adapter, priority=[CHAT]) == ["m_1_voice"]


class TestMixedReasons:
    @pytest.mark.parametrize(
        ("history", "evidence", "sent"),
        [
            # Two that count, three that do not: retried.
            (["decode_failed", "decode_failed"] + ["file_missing"] * 3, False, True),
            # Three that count, then an environmental one: not retried.
            (["decode_failed"] * 3 + ["file_missing"], False, False),
            (["decode_failed"] * 2 + [None] + ["engine_unavailable"], True, False),
            # Nine rows in all, two that count: one more try.
            (["decode_failed"] * 2 + ["engine_unavailable"] * 7, True, True),
            # Ten rows in all: the server failed it while it finished others, so it ends.
            (["decode_failed"] * 2 + ["engine_unavailable"] * 8, True, False),
            (["engine_unavailable"] * 10, True, False),
            # An environmental failure between content ones does not reset the count.
            (["decode_failed", "not_found", "decode_failed", "expired", "decode_failed"], True, False),
            (["decode_failed", "not_found", "decode_failed", "expired"], True, True),
        ],
    )
    async def test_the_reasons_of_every_failed_row_decide(self, real_adapter, tmp_path, history, evidence, sent):
        await _media(real_adapter, tmp_path, "m_1_voice", content_hash="1" * 64)
        for n, reason in enumerate(history):
            await _fail(real_adapter, "m_1_voice", reason, ago=DAY + timedelta(minutes=len(history) - n))
        if evidence:
            await _media(real_adapter, tmp_path, "m_9_voice", content_hash="9" * 64)
            await _done(real_adapter, "m_9_voice")
        assert (await _awaiting(real_adapter) == ["m_1_voice"]) is sent
