"""DOWNLOAD_MEDIA_TYPES is applied before the retry batch's LIMIT.

Same failure mode #442 fixed for SKIP_MEDIA_CHAT_IDS, and a worse one. The
pending-media retry is ordered by ``download_attempts`` and a filtered row is
never charged an attempt, so it stays at 0 forever and sorts ahead of every
genuine failure. Filtering in Python after the query therefore hands the whole
batch to rows that can never download, on every run, for good.

Filtered rows are deliberately left at ``downloaded=0``, so relaxing the filter
makes them eligible again. These run on both backends through ``real_adapter``.
"""

from datetime import datetime

import pytest

from src.config import Config

CHAT = -1001234000003
LIMIT = 10

PDF = "application/pdf"


async def _seed(adapter, message_id, media_type, *, mime_type=None, file_name=None, attempts=0):
    await adapter.upsert_chat({"id": CHAT, "type": "group", "title": "fixture chat"}, account_id=1)
    await adapter.insert_message(
        {"id": message_id, "chat_id": CHAT, "text": "x", "date": datetime(2026, 9, 1, 12), "raw_data": {}},
        account_id=1,
    )
    media_id = f"{CHAT}_{message_id}_{media_type}"
    await adapter.insert_media(
        {
            "id": media_id,
            "message_id": message_id,
            "chat_id": CHAT,
            "type": media_type,
            "mime_type": mime_type,
            "file_name": file_name,
            "file_size": 10,
            "downloaded": False,
        },
        account_id=1,
    )
    if attempts:
        for _ in range(attempts):
            await adapter.increment_media_download_attempts(media_id, account_id=1)
    return media_id


def _ids(rows):
    return sorted(row["id"] for row in rows)


async def test_filtered_rows_no_longer_fill_the_batch(real_adapter):
    """More filtered rows than the limit, and a real failure waiting behind them."""
    for message_id in range(1, 31):
        await _seed(real_adapter, message_id, "photo")
    # A genuine failed download: it has already been charged an attempt, so it
    # sorts BEHIND every filtered row.
    genuine = await _seed(real_adapter, 101, "document", mime_type=PDF, file_name="a.pdf", attempts=1)

    # The control: without the exclusion this fixture really does starve.
    unfiltered = await real_adapter.get_pending_media_downloads(None, 5, limit=LIMIT, account_id=1)
    assert len(unfiltered) == LIMIT
    assert genuine not in _ids(unfiltered)
    assert {row["type"] for row in unfiltered} == {"photo"}

    batch = await real_adapter.get_pending_media_downloads(None, 5, limit=LIMIT, account_id=1, media_types={"document"})
    assert _ids(batch) == [genuine]


async def test_document_mime_narrows_the_batch_too(real_adapter):
    """A type whitelist alone is not enough when the filtered type IS document."""
    for message_id in range(1, 31):
        await _seed(real_adapter, message_id, "document", mime_type="video/mp4", file_name="clip.mp4")
    wanted = await _seed(real_adapter, 101, "document", mime_type=PDF, file_name="report.pdf", attempts=1)

    control = await real_adapter.get_pending_media_downloads(
        None, 5, limit=LIMIT, account_id=1, media_types={"document"}
    )
    assert wanted not in _ids(control)

    batch = await real_adapter.get_pending_media_downloads(
        None,
        5,
        limit=LIMIT,
        account_id=1,
        media_types={"document"},
        document_mime_types={PDF},
        document_mime_extensions={".pdf"},
    )
    assert _ids(batch) == [wanted]


async def test_rows_the_query_cannot_judge_are_kept(real_adapter):
    """Mislabeled names, and rows that stored no metadata at all."""
    mislabeled = await _seed(real_adapter, 1, "document", mime_type="application/octet-stream", file_name="scan.PDF")
    bare = await _seed(real_adapter, 2, "document")
    parameterized = await _seed(real_adapter, 3, "document", mime_type="APPLICATION/PDF; charset=binary")
    wrong = await _seed(real_adapter, 4, "document", mime_type="application/zip", file_name="archive.zip")
    other_type = await _seed(real_adapter, 5, "photo")

    batch = await real_adapter.get_pending_media_downloads(
        None,
        5,
        account_id=1,
        document_mime_types={PDF},
        document_mime_extensions={".pdf"},
    )

    assert _ids(batch) == sorted([mislabeled, bare, parameterized, other_type])
    assert wrong not in _ids(batch)


async def test_filtered_rows_return_when_the_filter_is_relaxed(real_adapter):
    filtered = await _seed(real_adapter, 1, "photo")
    allowed = await _seed(real_adapter, 2, "document", mime_type=PDF, file_name="a.pdf")

    narrow = await real_adapter.get_pending_media_downloads(None, 5, account_id=1, media_types={"document"})
    assert _ids(narrow) == [allowed]

    relaxed = await real_adapter.get_pending_media_downloads(None, 5, account_id=1, media_types=set())
    assert _ids(relaxed) == sorted([filtered, allowed])
    assert not any(row["downloaded"] for row in relaxed)


async def test_no_filter_is_the_query_it_always_was(real_adapter):
    await _seed(real_adapter, 1, "photo")
    await _seed(real_adapter, 2, "document", mime_type="video/mp4", file_name="clip.mp4")

    for nothing in (None, set()):
        rows = await real_adapter.get_pending_media_downloads(
            None, 5, account_id=1, media_types=nothing, document_mime_types=nothing
        )
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# The SQL and the Python predicate must agree, or one of them starves a row
# ---------------------------------------------------------------------------

ROWS = [
    ("document", PDF, "report.pdf"),
    ("document", "APPLICATION/PDF", "report.pdf"),
    ("document", "application/octet-stream", "scan.PDF"),
    ("document", "application/octet-stream", "archive.zip"),
    ("document", "application/zip", None),
    ("document", None, "notes.pdf"),
    ("document", "text/plain", "readme.txt"),
    ("video", "video/mp4", "clip.mp4"),
    ("photo", None, "IMG_1.jpg"),
]


@pytest.fixture
def pdf_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_PATH", str(tmp_path))
    monkeypatch.setenv("DATABASE_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOAD_DOCUMENT_MIME_TYPES", PDF)
    return Config()


async def test_sql_never_drops_a_row_the_predicate_would_download(real_adapter, pdf_config):
    """The one direction that loses data: SQL stricter than document_mime_allowed."""
    expected_kept = set()
    for index, (media_type, mime_type, file_name) in enumerate(ROWS, start=1):
        media_id = await _seed(real_adapter, index, media_type, mime_type=mime_type, file_name=file_name)
        if media_type != "document" or pdf_config.document_mime_allowed(mime_type, file_name):
            expected_kept.add(media_id)

    batch = await real_adapter.get_pending_media_downloads(
        None,
        5,
        account_id=1,
        document_mime_types=pdf_config.download_document_mime_types,
        document_mime_extensions=pdf_config.download_document_mime_extensions,
    )

    assert expected_kept <= set(_ids(batch))
