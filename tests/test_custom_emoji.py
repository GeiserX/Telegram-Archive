"""Custom (premium) emoji: their ids are recorded, their files fetched once, and served.

A reaction made with a custom emoji is stored as ``custom_<document_id>``. The
adapter adds a pending ``custom_emoji`` row the first time it sees an id; the
backup fetches pending rows 100 per request (custom_emoji.fetch_custom_emoji)
and writes each file once under ``media/_emoji``; the viewer answers what it
knows (``/api/custom-emoji``) and serves the file (``/media/emoji/{id}``).

These run on a real engine, SQLite and PostgreSQL (``real_adapter``), with a
fake Telegram client that answers like Telethon. Every id and byte is fake.
"""

import asyncio
import importlib
import os
import shutil
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Document,
    DocumentAttributeCustomEmoji,
    DocumentAttributeImageSize,
    DocumentAttributeVideo,
    InputStickerSetEmpty,
)

from telegram_archive import custom_emoji, telegram_backup
from telegram_archive.db.models import Account, CustomEmoji

CHAT_ID = -100920
SENT = datetime(2026, 9, 1, 12, 0, 0)
# Above 2**53, as real document ids are.
SUN = 5000000000000000001
MOON = 5000000000000000002
STAR = 5000000000000000003
COMET = 5000000000000000004
WEBP = b"RIFF\x1c\x00\x00\x00WEBPVP8 fake custom emoji bytes"
TGS = b"\x1f\x8b fake animated emoji bytes"


async def _seed(adapter, messages=(1,)) -> None:
    async with adapter.db_manager.async_session_factory() as session:
        await session.merge(Account(id=1, label="Account 1"))
        await session.commit()
    await adapter.upsert_chat({"id": CHAT_ID, "type": "supergroup", "title": "fixture"}, account_id=1)
    for message_id in messages:
        await adapter.insert_message(
            {"id": message_id, "chat_id": CHAT_ID, "date": SENT, "text": "fake text", "raw_data": {}}, account_id=1
        )


async def _rows(adapter) -> dict[int, dict]:
    async with adapter.db_manager.async_session_factory() as session:
        rows = (await session.execute(select(CustomEmoji).order_by(CustomEmoji.document_id))).scalars().all()
        return {
            row.document_id: {
                "downloaded": row.downloaded,
                "attempts": row.attempts,
                "skip_reason": row.skip_reason,
                "file_name": row.file_name,
                "mime_type": row.mime_type,
                "alt": row.alt,
                "width": row.width,
                "text_color": row.text_color,
            }
            for row in rows
        }


# ---------------------------------------------------------------------------
# Recording the id
# ---------------------------------------------------------------------------


class TestRecording:
    async def test_a_custom_reaction_adds_one_pending_row(self, real_adapter):
        await _seed(real_adapter)
        observed = [
            {"emoji": f"custom_{SUN}", "count": 2},
            {"emoji": "👍", "count": 1},
            {"emoji": "custom_abc", "count": 1},
        ]
        assert await real_adapter.reconcile_reactions(1, CHAT_ID, observed, account_id=1) == "reconciled"
        rows = await _rows(real_adapter)
        assert list(rows) == [SUN]
        assert rows[SUN] == {
            "downloaded": 0,
            "attempts": 0,
            "skip_reason": None,
            "file_name": None,
            "mime_type": None,
            "alt": None,
            "width": None,
            "text_color": 0,
        }
        # A later count, and the row a fetch filled meanwhile, stay as they are.
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(CustomEmoji).where(CustomEmoji.document_id == SUN).values(downloaded=1, file_name=f"{SUN}.webp")
            )
            await session.commit()
        observed[0]["count"] = 5
        observed.append({"emoji": f"custom_{MOON}", "count": 1})
        await real_adapter.reconcile_reactions(1, CHAT_ID, observed, account_id=1)
        rows = await _rows(real_adapter)
        assert sorted(rows) == [SUN, MOON]
        assert (rows[SUN]["downloaded"], rows[SUN]["file_name"]) == (1, f"{SUN}.webp")

    async def test_plain_emoji_add_no_row(self, real_adapter):
        await _seed(real_adapter)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "🔥", "count": 3}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": "paid", "count": 1}], account_id=1)
        assert await _rows(real_adapter) == {}

    async def test_a_message_the_archive_does_not_hold_adds_no_row(self, real_adapter):
        await _seed(real_adapter)
        result = await real_adapter.reconcile_reactions(
            99, CHAT_ID, [{"emoji": f"custom_{SUN}", "count": 1}], account_id=1
        )
        assert result == "no_message"
        assert await _rows(real_adapter) == {}


# ---------------------------------------------------------------------------
# Fetching the files
# ---------------------------------------------------------------------------


def _document(document_id: int, mime_type="image/webp", size=len(WEBP), alt="☀️", text_color=None, video=False):
    attributes = [DocumentAttributeCustomEmoji(alt=alt, stickerset=InputStickerSetEmpty(), text_color=text_color)]
    attributes.append(
        DocumentAttributeVideo(duration=1.0, w=100, h=100) if video else DocumentAttributeImageSize(w=100, h=100)
    )
    return Document(
        id=document_id,
        access_hash=1,
        file_reference=b"",
        date=SENT,
        mime_type=mime_type,
        size=size,
        dc_id=2,
        attributes=attributes,
    )


class FakeTelegram:
    """Answers getCustomEmojiDocuments and download_media the way Telethon does."""

    def __init__(self, documents: dict[int, Document], data: dict[int, bytes] | None = None):
        self.documents = documents
        self.data = data or {}
        self.asked: list[list[int]] = []
        self.downloaded: list[int] = []
        self.request_errors: list[BaseException] = []
        self.download_errors: list[BaseException] = []

    async def __call__(self, request):
        self.asked.append(list(request.document_id))
        if self.request_errors:
            raise self.request_errors.pop(0)
        return [self.documents[i] for i in request.document_id if i in self.documents]

    async def download_media(self, doc, file):
        self.downloaded.append(doc.id)
        if self.download_errors:
            raise self.download_errors.pop(0)
        with open(file, "wb") as handle:
            handle.write(self.data.get(doc.id, WEBP))
        return file


@pytest.fixture
def quick_sleep(monkeypatch):
    """Every sleep returns at once and is recorded (the flood retry and the pause between batches)."""
    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def fake_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


async def _fetch(adapter, client, media_root, **kwargs):
    return await custom_emoji.fetch_custom_emoji(
        client, adapter, str(media_root), call=telegram_backup.call_with_flood_retry, **kwargs
    )


class TestFetch:
    async def test_each_kind_is_saved_under_its_id_with_what_telegram_says(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN, MOON, STAR])
        client = FakeTelegram(
            {
                SUN: _document(SUN),
                MOON: _document(MOON, mime_type="application/x-tgsticker", size=len(TGS), alt="🌙"),
                STAR: _document(STAR, mime_type="video/webm", alt="⭐", video=True),
            },
            {MOON: TGS},
        )
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["saved"] == 3 and counts["asked"] == 3
        assert client.asked == [[SUN, MOON, STAR]]
        folder = tmp_path / "_emoji"
        assert sorted(os.listdir(folder)) == sorted([f"{SUN}.webp", f"{MOON}.tgs", f"{STAR}.webm"])
        assert (folder / f"{MOON}.tgs").read_bytes() == TGS
        rows = await _rows(real_adapter)
        assert rows[SUN] == {
            "downloaded": 1,
            "attempts": 0,
            "skip_reason": None,
            "file_name": f"{SUN}.webp",
            "mime_type": "image/webp",
            "alt": "☀️",
            "width": 100,
            "text_color": 0,
        }
        assert (rows[STAR]["file_name"], rows[STAR]["width"]) == (f"{STAR}.webm", 100)
        # Nothing pending: a second run asks Telegram nothing.
        await _fetch(real_adapter, client, tmp_path)
        assert len(client.asked) == 1

    async def test_a_short_flood_wait_is_slept_out_and_the_file_saved(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN])
        client = FakeTelegram({SUN: _document(SUN)})
        client.request_errors.append(FloodWaitError(request=None, capture=3))
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["saved"] == 1 and counts["flood_wait_seconds"] == 0
        assert len(client.asked) == 2
        assert (await _rows(real_adapter))[SUN]["downloaded"] == 1

    async def test_a_long_flood_wait_stops_the_step_and_counts_no_attempt(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN, MOON])
        client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)})
        client.request_errors.append(FloodWaitError(request=None, capture=telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1))
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["flood_wait_seconds"] == telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1
        assert counts["deferred"] == 2 and counts["saved"] == 0
        rows = await _rows(real_adapter)
        assert [(r["downloaded"], r["attempts"], r["skip_reason"]) for r in rows.values()] == [(0, 0, None)] * 2
        assert not (tmp_path / "_emoji" / f"{SUN}.webp").exists()

    async def test_a_long_flood_wait_on_a_download_keeps_the_rest_pending(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN, MOON])
        client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)})
        client.download_errors.append(FloodWaitError(request=None, capture=telegram_backup.MAX_FLOOD_WAIT_SECONDS + 1))
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["flood_wait_seconds"] and counts["deferred"] == 2
        assert client.downloaded == [SUN]
        assert [r["attempts"] for r in (await _rows(real_adapter)).values()] == [0, 0]
        assert os.listdir(tmp_path / "_emoji") == []

    async def test_an_id_the_answer_leaves_out_is_asked_three_times(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN])
        client = FakeTelegram({})
        seen = []
        for _run in range(4):
            counts = await _fetch(real_adapter, client, tmp_path)
            row = (await _rows(real_adapter))[SUN]
            seen.append((row["attempts"], row["skip_reason"], counts["unavailable"]))
        assert seen == [(1, None, 0), (2, None, 0), (3, "unavailable", 1), (3, "unavailable", 0)]
        assert len(client.asked) == 3

    async def test_another_kind_and_an_oversize_file_are_skipped_with_a_reason(
        self, real_adapter, tmp_path, quick_sleep
    ):
        await real_adapter.note_custom_emoji([SUN, MOON])
        client = FakeTelegram(
            {
                SUN: _document(SUN, mime_type="image/png"),
                MOON: _document(MOON, size=custom_emoji.CUSTOM_EMOJI_MAX_BYTES + 1),
            }
        )
        counts = await _fetch(real_adapter, client, tmp_path)
        assert (counts["unsupported"], counts["oversize"]) == (1, 1)
        rows = await _rows(real_adapter)
        assert (rows[SUN]["skip_reason"], rows[SUN]["mime_type"], rows[SUN]["alt"]) == ("unsupported", "image/png", "☀️")
        assert (rows[MOON]["skip_reason"], rows[MOON]["downloaded"]) == ("oversize", 0)
        assert client.downloaded == []

    async def test_a_file_already_there_is_marked_and_never_written(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN])
        folder = tmp_path / "_emoji"
        folder.mkdir()
        kept = folder / f"{SUN}.webp"
        kept.write_bytes(b"the bytes a merge brought")
        os.utime(kept, (1_000_000_000, 1_000_000_000))
        client = FakeTelegram({SUN: _document(SUN, size=len(b"the bytes a merge brought"))})
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["present"] == 1 and client.downloaded == []
        assert kept.read_bytes() == b"the bytes a merge brought"
        assert kept.stat().st_mtime == 1_000_000_000
        assert (await _rows(real_adapter))[SUN]["downloaded"] == 1

    async def test_a_download_shorter_than_the_document_is_never_kept(self, real_adapter, tmp_path, quick_sleep):
        # Telethon ends a download at the first short answer without checking
        # the total: the cut file is not the emoji, and the next run asks again.
        await real_adapter.note_custom_emoji([SUN, MOON])
        client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)}, {SUN: WEBP[:10], MOON: b""})
        counts = await _fetch(real_adapter, client, tmp_path)
        assert (counts["saved"], counts["failed"]) == (0, 0)
        rows = await _rows(real_adapter)
        assert [(r["downloaded"], r["attempts"], r["file_name"]) for r in rows.values()] == [(0, 1, None)] * 2
        assert os.listdir(tmp_path / "_emoji") == []
        client.data = {}
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["saved"] == 2
        assert (tmp_path / "_emoji" / f"{SUN}.webp").read_bytes() == WEBP

    async def test_a_short_file_already_there_is_neither_marked_nor_touched(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN])
        folder = tmp_path / "_emoji"
        folder.mkdir()
        kept = folder / f"{SUN}.webp"
        kept.write_bytes(WEBP[:10])
        client = FakeTelegram({SUN: _document(SUN)})
        counts = await _fetch(real_adapter, client, tmp_path)
        assert counts["present"] == 0
        # The archive never overwrites a file: the row stays pending and the cut file stays as it was.
        assert kept.read_bytes() == WEBP[:10]
        assert sorted(os.listdir(folder)) == [f"{SUN}.webp"]
        row = (await _rows(real_adapter))[SUN]
        assert (row["downloaded"], row["attempts"]) == (0, 1)

    async def test_a_file_written_meanwhile_counts_only_when_complete(self, real_adapter, tmp_path):
        folder = tmp_path / "_emoji"
        folder.mkdir()
        path = str(folder / f"{SUN}.webp")

        class Racer(FakeTelegram):
            def __init__(self, other: bytes):
                super().__init__({})
                self.other = other

            async def download_media(self, doc, file):
                with open(path, "wb") as handle:
                    handle.write(self.other)
                return await super().download_media(doc, file)

        async def call(fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        doc = _document(SUN)
        assert await custom_emoji._download(Racer(b""), call, doc, path, len(WEBP)) is None
        os.remove(path)
        assert await custom_emoji._download(Racer(WEBP), call, doc, path, len(WEBP)) == path
        assert sorted(os.listdir(folder)) == [f"{SUN}.webp"]

    async def test_a_failed_download_is_counted_and_capped(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN])
        client = FakeTelegram({SUN: _document(SUN)})
        client.download_errors.extend([ValueError("fake"), ValueError("fake"), ValueError("fake")])
        for _run in range(3):
            await _fetch(real_adapter, client, tmp_path)
        row = (await _rows(real_adapter))[SUN]
        assert (row["attempts"], row["skip_reason"], row["downloaded"]) == (3, "failed", 0)
        assert os.listdir(tmp_path / "_emoji") == []

    async def test_six_hundred_pending_ask_five_hundred_in_five_calls_of_a_hundred(
        self, real_adapter, tmp_path, quick_sleep
    ):
        ids = [SUN + n for n in range(600)]
        await real_adapter.note_custom_emoji(ids)
        client = FakeTelegram({i: _document(i) for i in ids})
        counts = await _fetch(real_adapter, client, tmp_path)
        assert [len(batch) for batch in client.asked] == [100] * 5
        assert (counts["asked"], counts["saved"], counts["deferred"]) == (500, 500, 100)
        # One pause between two calls.
        assert quick_sleep.count(custom_emoji.CUSTOM_EMOJI_PAUSE_SECONDS) == 4

    async def test_the_first_seen_are_fetched_first(self, real_adapter, tmp_path, quick_sleep):
        await real_adapter.note_custom_emoji([SUN, MOON])
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(CustomEmoji).where(CustomEmoji.document_id == MOON).values(first_seen=SENT - timedelta(days=1))
            )
            await session.commit()
        client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)})
        await _fetch(real_adapter, client, tmp_path, limit=1)
        assert client.asked == [[MOON]]


class TestBackupRun:
    async def test_the_backup_fetches_after_its_media_and_skips_a_missing_folder(
        self, real_adapter, tmp_path, quick_sleep
    ):
        await real_adapter.note_custom_emoji([SUN])
        backup = telegram_backup.TelegramBackup.__new__(telegram_backup.TelegramBackup)
        backup.db = real_adapter
        backup.client = FakeTelegram({SUN: _document(SUN)})
        backup.config = type("C", (), {"media_path": str(tmp_path / "missing")})()
        assert await backup._fetch_custom_emoji() is None
        assert backup.client.asked == []
        (tmp_path / "media").mkdir()
        (tmp_path / "media" / "keep").write_bytes(b"x")
        backup.config.media_path = str(tmp_path / "media")
        counts = await backup._fetch_custom_emoji()
        assert counts["saved"] == 1
        assert (tmp_path / "media" / "_emoji" / f"{SUN}.webp").read_bytes() == WEBP


class TestListener:
    """The listener fetches custom emoji it noted, so a live one is drawn within minutes, not after the daily run."""

    @staticmethod
    def _listener(adapter, client, media_path):
        from types import SimpleNamespace

        from telegram_archive.listener import TelegramListener

        holder = SimpleNamespace(config=SimpleNamespace(media_path=media_path), client=client, db=adapter)
        return lambda: TelegramListener._fetch_new_custom_emoji(holder)

    async def test_only_never_tried_ids_are_fetched_and_never_during_a_backup_run(
        self, real_adapter, tmp_path, quick_sleep
    ):
        await real_adapter.note_custom_emoji([SUN, MOON])
        async with real_adapter.db_manager.async_session_factory() as session:
            # MOON was tried once by a backup run: its retries stay with the backup runs.
            await session.execute(update(CustomEmoji).where(CustomEmoji.document_id == MOON).values(attempts=1))
            await session.commit()
        client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)})
        media = tmp_path / "media"
        fetch = self._listener(real_adapter, client, str(tmp_path / "missing"))
        assert await fetch() is None
        media.mkdir()
        (media / "keep").write_bytes(b"x")
        fetch = self._listener(real_adapter, client, str(media))
        await real_adapter.set_metadata("backup_in_progress", "1")
        assert await fetch() is None
        assert client.asked == []
        await real_adapter.set_metadata("backup_in_progress", "0")
        counts = await fetch()
        assert client.asked == [[SUN]]
        assert (counts["saved"], counts["deferred"]) == (1, 0)
        assert (media / "_emoji" / f"{SUN}.webp").read_bytes() == WEBP
        rows = await _rows(real_adapter)
        assert (rows[SUN]["downloaded"], rows[MOON]["downloaded"], rows[MOON]["attempts"]) == (1, 0, 1)

    async def test_a_running_listener_fetches_on_its_timer_and_stops_with_it(self, monkeypatch):
        from test_listener_extended import _make_config, _make_db

        from telegram_archive import listener as listener_mod

        monkeypatch.setattr(listener_mod, "LISTENER_CUSTOM_EMOJI_SECONDS", 0)
        listener = listener_mod.TelegramListener(_make_config(listen_reactions=False), _make_db(), account_id=1)
        fetched = asyncio.Event()

        async def fake_fetch():
            fetched.set()

        listener._fetch_new_custom_emoji = fake_fetch
        disconnected = asyncio.Event()

        async def run_until_disconnected():
            await disconnected.wait()

        listener.client = type("Client", (), {"run_until_disconnected": staticmethod(run_until_disconnected)})()
        task = asyncio.create_task(listener.run())
        await asyncio.wait_for(fetched.wait(), timeout=5)
        loop_task = listener._custom_emoji_task
        disconnected.set()
        await task
        assert loop_task.done() and listener._custom_emoji_task is None


class TestBackfill:
    async def test_backfill_collects_reactions_rearms_and_fetches(self, real_adapter, tmp_path, quick_sleep):
        await _seed(real_adapter, messages=(1, 2))
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": f"custom_{SUN}", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(2, CHAT_ID, [{"emoji": f"custom_{MOON}", "count": 1}], account_id=1)
        # SUN gave up after three answers without it; MOON's row is missing (a
        # merge before 040 brought the reaction and no row).
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(
                update(CustomEmoji).where(CustomEmoji.document_id == SUN).values(attempts=3, skip_reason="unavailable")
            )
            await session.execute(CustomEmoji.__table__.delete().where(CustomEmoji.document_id == MOON))
            await session.commit()
        (tmp_path / "keep").write_bytes(b"x")
        backup = telegram_backup.TelegramBackup.__new__(telegram_backup.TelegramBackup)
        backup.db = real_adapter
        backup.account_id = 1
        backup.client = FakeTelegram({SUN: _document(SUN), MOON: _document(MOON)})
        summary = telegram_backup._empty_backfill_summary()

        await backup._backfill_custom_emoji(None, False, str(tmp_path), summary)
        assert summary["emoji"] == {"collected": 2, "saved": 2, "unavailable": 0, "deferred": 0}
        assert backup.client.asked == []
        assert list(await _rows(real_adapter)) == [SUN]  # the dry run writes nothing

        summary = telegram_backup._empty_backfill_summary()
        await backup._backfill_custom_emoji(None, True, str(tmp_path), summary)
        assert summary["emoji"] == {"collected": 2, "saved": 2, "unavailable": 0, "deferred": 0}
        rows = await _rows(real_adapter)
        assert [(i, r["downloaded"], r["skip_reason"]) for i, r in rows.items()] == [(SUN, 1, None), (MOON, 1, None)]

    async def test_rearming_leaves_a_row_still_counting_its_attempts(self, real_adapter):
        """Only a row that gave up is marked again: repeated runs cannot reset a count and step past the cap."""
        await _seed(real_adapter)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": f"custom_{SUN}", "count": 1}], account_id=1)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": f"custom_{MOON}", "count": 1}], account_id=1)
        async with real_adapter.db_manager.async_session_factory() as session:
            await session.execute(update(CustomEmoji).where(CustomEmoji.document_id == SUN).values(attempts=2))
            await session.execute(
                update(CustomEmoji).where(CustomEmoji.document_id == MOON).values(attempts=3, skip_reason="failed")
            )
            await session.commit()

        assert await real_adapter.rearm_custom_emoji([SUN, MOON]) == 1

        rows = await _rows(real_adapter)
        assert (rows[SUN]["attempts"], rows[SUN]["skip_reason"]) == (2, None)
        assert (rows[MOON]["attempts"], rows[MOON]["skip_reason"]) == (0, None)

    async def test_an_attempt_is_counted_in_the_database_and_caps_once(self, real_adapter):
        """The count is one UPDATE, so two writers cannot both read the old value; the cap is reported once."""
        await _seed(real_adapter)
        await real_adapter.reconcile_reactions(1, CHAT_ID, [{"emoji": f"custom_{SUN}", "count": 1}], account_id=1)

        answers = [await real_adapter.count_custom_emoji_attempt(SUN, "unavailable", 3) for _ in range(4)]

        assert answers == [False, False, True, False]
        row = (await _rows(real_adapter))[SUN]
        assert (row["attempts"], row["skip_reason"]) == (4, "unavailable")
        assert await real_adapter.count_custom_emoji_attempt(MOON, "unavailable", 3) is False


# ---------------------------------------------------------------------------
# The viewer
# ---------------------------------------------------------------------------


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    for key, value in {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module._media_root = tmp_path.resolve()
    return module


async def _viewer_seed(adapter, media_root) -> None:
    await adapter.note_custom_emoji([SUN, MOON, STAR, 4242, 4343])
    folder = media_root / "_emoji"
    folder.mkdir()
    (folder / f"{SUN}.webp").write_bytes(WEBP)
    (folder / f"{MOON}.tgs").write_bytes(TGS)
    (folder / f"{STAR}.webm").write_bytes(b"fake webm bytes")
    (folder / "4242.webp").write_bytes(WEBP)
    (folder / "4343.html").write_bytes(b"<script>fake</script>")
    for document_id, name, alt in (
        (SUN, f"{SUN}.webp", "☀️"),
        (MOON, f"{MOON}.tgs", "🌙"),
        (STAR, f"{STAR}.webm", "⭐"),
    ):
        await adapter.update_custom_emoji(document_id, {"file_name": name, "downloaded": 1, "alt": alt})
    # Rows whose stored name is not an emoji file of their own: never served.
    await adapter.update_custom_emoji(4242, {"file_name": "../4242.webp", "downloaded": 1})
    await adapter.update_custom_emoji(4343, {"file_name": "4343.html", "downloaded": 1})


def _users(main_mod) -> dict:
    UserContext = main_mod.UserContext
    return {
        "master": UserContext(username="admin", role="master"),
        "one chat": UserContext(username="viewer", role="viewer", allowed_chat_refs={"fakeRefChatA000000001"}),
        "share token": UserContext(username="token:1", role="token", allowed_chat_refs={"fakeRefChatA000000001"}),
        "no download": UserContext(username="viewer", role="viewer", no_download=True),
    }


class TestViewerRoutes:
    async def test_the_info_route_answers_with_string_keys_for_every_grant(self, main_mod, real_adapter, tmp_path):
        await _viewer_seed(real_adapter, tmp_path)
        await real_adapter.note_custom_emoji([7777])
        main_mod.db = real_adapter
        ids = f"{SUN},{MOON},{STAR},7777,4242,4343,123"
        for user in _users(main_mod).values():
            answer = await main_mod.get_custom_emoji_info(ids=ids, user=user)
            assert answer == {
                str(SUN): {"kind": "image", "alt": "☀️", "text_color": False, "ready": True},
                str(MOON): {"kind": "tgs", "alt": "🌙", "text_color": False, "ready": True},
                str(STAR): {"kind": "webm", "alt": "⭐", "text_color": False, "ready": True},
                "7777": {"kind": None, "alt": None, "text_color": False, "ready": False},
                "4242": {"kind": None, "alt": None, "text_color": False, "ready": False},
                "4343": {"kind": None, "alt": None, "text_color": False, "ready": False},
            }

    async def test_the_info_route_refuses_bad_ids_and_an_empty_grant(self, main_mod, real_adapter):
        main_mod.db = real_adapter
        master = _users(main_mod)["master"]
        for ids in ("", "abc", "1,,2", "-5", "1.5", "99999999999999999999", ",".join(["1"] * 101), "0"):
            with pytest.raises(main_mod.HTTPException) as exc:
                await main_mod.get_custom_emoji_info(ids=ids, user=master)
            assert exc.value.status_code == 400, ids
        for empty in (
            main_mod.UserContext(username="viewer", role="viewer", allowed_chat_refs=set()),
            main_mod.UserContext(username="viewer", role="viewer", allowed_accounts=set()),
        ):
            with pytest.raises(main_mod.HTTPException) as exc:
                await main_mod.get_custom_emoji_info(ids=str(SUN), user=empty)
            assert exc.value.status_code == 403

    async def test_the_file_route_serves_each_kind_for_every_grant(self, main_mod, real_adapter, tmp_path):
        await _viewer_seed(real_adapter, tmp_path)
        main_mod.db = real_adapter
        kinds = {SUN: "image/webp", MOON: "application/x-tgsticker", STAR: "video/webm"}
        for user in _users(main_mod).values():
            for document_id, media_type in kinds.items():
                response = await main_mod.serve_custom_emoji(str(document_id), user=user)
                assert response.media_type == media_type
                assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
                assert os.path.samefile(response.path, next((tmp_path / "_emoji").glob(f"{document_id}.*")))

    async def test_the_file_route_refuses_what_it_must_not_serve(self, main_mod, real_adapter, tmp_path):
        await _viewer_seed(real_adapter, tmp_path)
        await real_adapter.note_custom_emoji([7777])
        main_mod.db = real_adapter
        master = _users(main_mod)["master"]
        # Not downloaded, unknown, a bad stored name, not an id.
        for document_id in ("7777", "123", "4242", "4343", "abc", "../x"):
            with pytest.raises(main_mod.HTTPException) as exc:
                await main_mod.serve_custom_emoji(document_id, user=master)
            assert exc.value.status_code == 404, document_id
        # Downloaded but gone from disk.
        (tmp_path / "_emoji" / f"{SUN}.webp").unlink()
        with pytest.raises(main_mod.HTTPException) as exc:
            await main_mod.serve_custom_emoji(str(SUN), user=master)
        assert exc.value.status_code == 404
        empty = main_mod.UserContext(username="viewer", role="viewer", allowed_chat_refs=set())
        with pytest.raises(main_mod.HTTPException) as exc:
            await main_mod.serve_custom_emoji(str(MOON), user=empty)
        assert exc.value.status_code == 403


def test_both_routes_ask_for_a_login_when_auth_is_on(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    monkeypatch.setenv("BACKUP_PATH", str(tmp_path))
    monkeypatch.setenv("VIEWER_USERNAME", "admin")
    monkeypatch.setenv("VIEWER_PASSWORD", "test@value/here")
    import telegram_archive.web.main as module

    importlib.reload(module)
    module.db = AsyncMock()
    client = TestClient(module.app, raise_server_exceptions=False)
    assert client.get(f"/api/custom-emoji?ids={SUN}").status_code == 401
    assert client.get(f"/media/emoji/{SUN}").status_code == 401
    module.db.get_custom_emoji.assert_not_called()


# ---------------------------------------------------------------------------
# Merging archives
# ---------------------------------------------------------------------------

from test_merge_command import MergeCase, sqlite  # noqa: E402

from telegram_archive import merge  # noqa: E402

_INSERT_EMOJI = (
    "INSERT INTO custom_emoji (document_id, file_name, mime_type, alt, downloaded, attempts, text_color, first_seen) "
    "VALUES (?, ?, ?, ?, ?, 0, 0, '2026-01-01 00:00:00')"
)


class TestMerge(MergeCase):
    def setUp(self):
        super().setUp()
        with sqlite(self.source_db) as conn:
            conn.execute(_INSERT_EMOJI, (SUN, f"{SUN}.webp", "image/webp", "☀️", 1))
            # Downloaded in the source, but its file is not in the source folder.
            conn.execute(_INSERT_EMOJI, (MOON, f"{MOON}.tgs", "application/x-tgsticker", "🌙", 1))
            conn.execute(_INSERT_EMOJI, (STAR, f"{STAR}.webp", "image/webp", "⭐", 1))
        with sqlite(self.target_db) as conn:
            conn.execute(_INSERT_EMOJI, (STAR, None, None, "target alt", 0))
        source_folder = self.source_media / "_emoji"
        source_folder.mkdir()
        (source_folder / f"{SUN}.webp").write_bytes(WEBP)
        (source_folder / f"{STAR}.webp").write_bytes(b"source star bytes")
        (source_folder / "notes.txt").write_bytes(b"not an emoji file")
        target_folder = self.target_media / "_emoji"
        target_folder.mkdir()
        (target_folder / f"{STAR}.webp").write_bytes(b"target star bytes")

    def emoji_rows(self) -> list[tuple]:
        return self.target_rows("SELECT document_id, file_name, alt, downloaded FROM custom_emoji ORDER BY document_id")

    def test_rows_and_files_the_target_lacks_are_added_and_nothing_is_overwritten(self):
        report = self.run_merge()
        self.assertEqual(2, report.rows["custom_emoji"])
        self.assertEqual(
            [(SUN, f"{SUN}.webp", "☀️", 1), (MOON, f"{MOON}.tgs", "🌙", 0), (STAR, None, "target alt", 0)],
            self.emoji_rows(),
        )
        folder = self.target_media / "_emoji"
        self.assertEqual(WEBP, (folder / f"{SUN}.webp").read_bytes())
        self.assertEqual(b"target star bytes", (folder / f"{STAR}.webp").read_bytes())
        self.assertFalse((folder / "notes.txt").exists())
        self.assertEqual((1, 1), (report.media.emoji, report.media.emoji_present))
        self.assertIn(
            "  Custom emoji files copied: 1 (already there: 1)",
            merge.format_report(report),
        )

    def test_the_dry_run_plans_the_same_and_writes_nothing(self):
        before = self.emoji_rows()
        plan = self.run_merge(dry_run=True)
        self.assertEqual(before, self.emoji_rows())
        self.assertFalse((self.target_media / "_emoji" / f"{SUN}.webp").exists())
        real = self.run_merge()
        self.assertEqual(plan.rows["custom_emoji"], real.rows["custom_emoji"])
        self.assertEqual((plan.media.emoji, plan.media.emoji_present), (real.media.emoji, real.media.emoji_present))

    def test_without_the_source_folder_every_row_arrives_pending(self):
        shutil.rmtree(self.source_media / "_emoji")
        self.run_merge()
        self.assertEqual([0, 0, 0], [row[3] for row in self.emoji_rows()])

    def test_a_link_and_an_empty_file_in_the_source_are_never_copied(self):
        """A link in an untrusted source could point at any readable file; an empty file is not an emoji."""
        outside = self.source_media.parent / "outside-secret.webp"
        outside.write_bytes(b"bytes from outside the archive")
        source_folder = self.source_media / "_emoji"
        (source_folder / f"{SUN}.webp").unlink()
        (source_folder / f"{SUN}.webp").symlink_to(outside)
        with sqlite(self.source_db) as conn:
            conn.execute(_INSERT_EMOJI, (COMET, f"{COMET}.webp", "image/webp", "☄️", 1))
        (source_folder / f"{COMET}.webp").write_bytes(b"")

        report = self.run_merge()

        folder = self.target_media / "_emoji"
        self.assertFalse((folder / f"{SUN}.webp").exists())
        self.assertFalse((folder / f"{COMET}.webp").exists())
        self.assertEqual(0, report.media.emoji)
        downloaded = {row[0]: row[3] for row in self.emoji_rows()}
        self.assertEqual((0, 0), (downloaded[SUN], downloaded[COMET]))
