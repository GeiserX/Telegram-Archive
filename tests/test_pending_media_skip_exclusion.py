"""SKIP_MEDIA_CHAT_IDS is applied before the retry batch's LIMIT (#442).

The pending-media retry pulled at most ``limit`` rows and only then dropped the
ones belonging to skipped chats, so a skipped chat holding more pending rows
than the limit filled the whole batch and every download that could happen
waited behind it: on a real archive, 974 of 1,000 slots.

Skipped rows are deliberately left at ``downloaded=0``, so taking a chat off the
skip list makes its media eligible again. These run on both backends through
``real_adapter``.
"""

from datetime import datetime

SKIPPED_CHAT = -1001234000001
NORMAL_CHAT = -1001234000002
LIMIT = 10


async def _seed(adapter, chat_id, message_ids):
    await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "fixture chat"}, account_id=1)
    for message_id in message_ids:
        await adapter.insert_message(
            {"id": message_id, "chat_id": chat_id, "text": "x", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
            account_id=1,
        )
        await adapter.insert_media(
            {
                "id": f"{chat_id}_{message_id}_photo",
                "message_id": message_id,
                "chat_id": chat_id,
                "type": "photo",
                "file_size": 10,
                "downloaded": False,
            },
            account_id=1,
        )


def _chats(rows):
    return sorted({row["chat_id"] for row in rows})


async def test_skipped_rows_no_longer_fill_the_batch(real_adapter):
    """More skipped rows than the limit, the shape of the report."""
    await _seed(real_adapter, SKIPPED_CHAT, range(1, 31))
    await _seed(real_adapter, NORMAL_CHAT, range(101, 104))

    # The control: without the exclusion this fixture really does starve.
    unfiltered = await real_adapter.get_pending_media_downloads(None, 5, limit=LIMIT, account_id=1)
    assert len(unfiltered) == LIMIT
    assert _chats(unfiltered) == [SKIPPED_CHAT]

    batch = await real_adapter.get_pending_media_downloads(
        None, 5, limit=LIMIT, exclude_chat_ids={SKIPPED_CHAT}, account_id=1
    )
    assert _chats(batch) == [NORMAL_CHAT]
    assert len(batch) == 3


async def test_skipped_rows_stay_pending_and_return_when_unskipped(real_adapter):
    await _seed(real_adapter, SKIPPED_CHAT, range(1, 4))
    await _seed(real_adapter, NORMAL_CHAT, range(101, 103))

    excluded = await real_adapter.get_pending_media_downloads(None, 5, exclude_chat_ids={SKIPPED_CHAT}, account_id=1)
    assert _chats(excluded) == [NORMAL_CHAT]

    unskipped = await real_adapter.get_pending_media_downloads(None, 5, exclude_chat_ids=set(), account_id=1)
    assert _chats(unskipped) == sorted([SKIPPED_CHAT, NORMAL_CHAT])
    assert len(unskipped) == 5
    assert not any(row["downloaded"] for row in unskipped)


async def test_no_exclusion_is_the_query_it_always_was(real_adapter):
    await _seed(real_adapter, SKIPPED_CHAT, range(1, 4))
    await _seed(real_adapter, NORMAL_CHAT, range(101, 103))

    for nothing in (None, set()):
        rows = await real_adapter.get_pending_media_downloads(None, 5, exclude_chat_ids=nothing, account_id=1)
        assert len(rows) == 5
