"""Streaming media verification: keyset batches on real engines.

`iter_media_for_verification` replaced a full-table materialization that
OOM-killed the 256m backup container. The keyset walk (string ids, unique per
account) must return every row exactly once across batch boundaries, project
the columns verification consumes, and stay account-scoped.
"""

from datetime import datetime


async def _seed(adapter, chat_id: int, media_ids: list[str], *, account_id: int = 1) -> None:
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "vs"}, account_id=account_id)
    for n, media_id in enumerate(media_ids, start=1):
        await adapter.insert_message(
            {
                "id": n,
                "chat_id": chat_id,
                "sender_id": 4242,
                "date": datetime(2026, 1, 1, 12, 0, 0),
                "text": "seed",
                "is_outgoing": 0,
                "sender_name": "Fixture Sender",
                "raw_data": {},
            },
            account_id=account_id,
        )
        await adapter.insert_media(
            {
                "id": media_id,
                "message_id": n,
                "chat_id": chat_id,
                "type": "photo",
                "file_path": f"media/{media_id}.jpg",
                "file_size": 100 + n,
                "downloaded": True,
            },
            account_id=account_id,
        )


class TestIterMediaForVerification:
    async def test_keyset_batches_cover_every_row_exactly_once(self, real_adapter):
        ids = [f"m{n}" for n in range(1, 6)]  # lexicographic == insertion order
        await _seed(real_adapter, 930001, ids)

        batches = [b async for b in real_adapter.iter_media_for_verification(account_id=1, batch_size=2)]

        assert [len(b) for b in batches] == [2, 2, 1]
        flat = [m["id"] for b in batches for m in b]
        assert flat == ids  # no duplicates, no gaps, ordered by id
        sample = batches[0][0]
        assert set(sample) == {
            "id",
            "message_id",
            "chat_id",
            "type",
            "file_path",
            "file_name",
            "file_size",
            "downloaded",
        }
        assert sample["file_path"] == "media/m1.jpg"
        assert sample["file_size"] == 101

    async def test_scoping_and_exact_batch_multiple(self, real_adapter):
        await _seed(real_adapter, 930002, ["a1", "a2"])

        # Exact multiple of batch_size: the follow-up probe must return cleanly.
        batches = [b async for b in real_adapter.iter_media_for_verification(account_id=1, batch_size=2)]
        assert [m["id"] for b in batches for m in b] == ["a1", "a2"]

        # Another account sees nothing.
        assert [b async for b in real_adapter.iter_media_for_verification(account_id=9, batch_size=2)] == []
