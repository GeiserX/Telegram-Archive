"""Imported media stays addressable and verifiable (#423, #310).

Two ingest paths write media rows in different shapes and both are permanent:

* the API sweep and the realtime listener mint ``Media.id`` as
  ``{chat}_{message}_{type}`` and store an ABSOLUTE ``file_path``;
* the Telegram Desktop importer mints ``import_{chat}_{message}`` — type-free,
  so adoption can re-key it whichever type each side computed — and stores a
  file_path RELATIVE to the media root.

Every consumer that assumed the sweep's spelling broke on the other one:

* #423 — the viewer derived its URL key by slicing the chat prefix off the
  storage id, so imported media lost its URL, its gallery thumbnail and its
  place in the gallery cursor. The file was on disk; the viewer said
  "Media not found".
* #310 — the capture layer stat()ed the raw file_path, so a media-root-relative
  value resolved against the process CWD, and every imported file was judged
  missing and re-downloaded, or skipped by a delete that still dropped its row.

These tests assert the OUTCOME (a URL, then the right row, then a correct
verdict about the file on disk), never which spelling sits in the database, so
they keep their meaning if the id scheme ever changes again.
"""

import importlib
import os
import sys
import tempfile
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_import_addr_"))

pytest.importorskip("fastapi")

CHAT_ID = -1002176572213
MSG_ID = 1453
IMPORT_ID = f"import_{CHAT_ID}_{MSG_ID}"
SWEEP_ID = f"{CHAT_ID}_{MSG_ID}_video"
CHAT_REF = "importAddrRef1234ABcd"
FNAME = f"{IMPORT_ID}_holiday.Mp4"
REL_PATH = f"{CHAT_ID}/{FNAME}"  # exactly what src/telegram_import.py writes


def _row(media_id: str, media_type: str = "video", file_path: str = REL_PATH, downloaded: int = 1) -> dict:
    return {
        "id": media_id,
        "account_id": 1,
        "chat_id": CHAT_ID,
        "message_id": MSG_ID,
        "type": media_type,
        "file_path": file_path,
        "file_name": FNAME,
        "downloaded": downloaded,
    }


class _MediaTable:
    """A real (tiny) media table, queried the way the adapter queries it.

    Deliberately NOT a blanket ``AsyncMock(return_value=...)``: one of those
    answers the wrong key as happily as the right one, so a test built on it
    could not fail and would prove nothing about #423.
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.asked = []

    async def get_media_for_message(self, chat_id, message_id, media_type, *, account_id):
        self.asked.append((chat_id, message_id, media_type, account_id))
        hits = [
            r
            for r in self.rows
            if (r["chat_id"], r["message_id"], r["type"], r["account_id"])
            == (chat_id, message_id, media_type, account_id)
        ]
        hits.sort(key=lambda r: (-r["downloaded"], r["id"]))
        return hits[0] if hits else None


def _reload_main(media_root=None):
    import src.web.main as main_mod

    importlib.reload(main_mod)
    main_mod.db = AsyncMock()
    if media_root is not None:
        main_mod._media_root = main_mod.Path(media_root).resolve()
    return main_mod


ANON_ENV = {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}


@pytest.fixture
def main_mod(monkeypatch, tmp_path):
    for k, v in ANON_ENV.items():
        monkeypatch.setenv(k, v)
    media_root = tmp_path / "media"
    (media_root / str(CHAT_ID)).mkdir(parents=True)
    (media_root / REL_PATH).write_bytes(b"\x00" * 2048)
    return _reload_main(media_root)


# --------------------------------------------------------------- #423 payload
@pytest.mark.parametrize("storage_id", [IMPORT_ID, SWEEP_ID])
def test_message_payload_gets_a_media_url_whatever_the_storage_id(main_mod, storage_id):
    """The URL key names (message, type). How the row is filed is irrelevant."""
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")
    message = {"id": MSG_ID, "sender_id": None, "media": _row(storage_id)}

    main_mod._attach_message_payload_urls([message], chat)

    assert message["media"]["id"] == f"{MSG_ID}_video"
    assert message["media"]["url"] == f"/media/{CHAT_REF}/{MSG_ID}_video"


def test_payload_never_ships_the_chat_id_to_the_browser(main_mod):
    """The ref design promises no chat id survives in any URL. An unrecognised
    storage id used to be passed through verbatim, which shipped the chat id to
    the client and back as the gallery cursor."""
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")
    message = {"id": MSG_ID, "sender_id": None, "media": _row(IMPORT_ID)}

    main_mod._attach_message_payload_urls([message], chat)

    assert str(CHAT_ID) not in str(message["media"]["id"])
    assert str(CHAT_ID) not in str(message["media"]["url"])


def test_media_with_no_type_gets_no_url_rather_than_a_broken_one(main_mod):
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")
    message = {"id": MSG_ID, "sender_id": None, "media": _row(IMPORT_ID, media_type=None)}

    main_mod._attach_message_payload_urls([message], chat)

    assert message["media"]["url"] is None


# ----------------------------------------------------------------- #423 route
@pytest.mark.parametrize("storage_id", [IMPORT_ID, SWEEP_ID])
async def test_route_resolves_the_row_whatever_the_storage_id(main_mod, storage_id):
    table = _MediaTable([_row(storage_id)])
    main_mod.db.get_media_for_message = table.get_media_for_message
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")

    row = await main_mod._entitled_media_row(chat, f"{MSG_ID}_video")

    assert row["id"] == storage_id
    assert table.asked == [(CHAT_ID, MSG_ID, "video", 1)]


async def test_route_asks_for_the_resolved_chat_never_a_url_supplied_one(main_mod):
    """The chat bound is a SQL predicate built from the resolved chat, so a key
    cannot name another chat's media even though ids embed a chat id."""
    table = _MediaTable([_row(IMPORT_ID)])
    main_mod.db.get_media_for_message = table.get_media_for_message
    other = main_mod.ChatContext(account_id=1, chat_id=-4242, ref=CHAT_REF, type="channel")

    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod._entitled_media_row(other, f"{MSG_ID}_video")

    assert exc.value.status_code == 404
    assert table.asked == [(-4242, MSG_ID, "video", 1)]


async def test_a_wrong_type_in_the_key_does_not_reach_the_import_row(main_mod):
    """The import id carries no type, so a lookup that ignored type would let
    ``{msg}_anything`` serve it. The type is part of the predicate."""
    table = _MediaTable([_row(IMPORT_ID)])
    main_mod.db.get_media_for_message = table.get_media_for_message
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")

    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod._entitled_media_row(chat, f"{MSG_ID}_zzz")

    assert exc.value.status_code == 404


async def test_a_duplicate_pair_serves_the_row_the_message_list_showed(main_mod):
    """#310's re-download bug left archives holding both an import row and a
    sweep row for one message. get_messages attaches (downloaded desc, id asc);
    the byte route must land on that same row or the gallery and the player
    disagree about which file they are showing."""
    table = _MediaTable([_row(SWEEP_ID, downloaded=0), _row(IMPORT_ID, downloaded=1)])
    main_mod.db.get_media_for_message = table.get_media_for_message
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="channel")

    row = await main_mod._entitled_media_row(chat, f"{MSG_ID}_video")

    assert row["id"] == IMPORT_ID  # downloaded wins over the pending sweep twin


# ================================================================== #310
# The capture layer's file_path contract.
#
# Every test below carries its positive control in the same class: a fix that
# merely skipped relative paths, or trusted them blindly, would pass the happy
# case and fail the controls. Watch them fail before trusting them.

import shutil  # noqa: E402
import unittest  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from tests.test_telegram_backup_extended import _make_backup, _run  # noqa: E402


class TestImportedMediaPathResolution(unittest.TestCase):
    """#310: a media-root-relative file_path must resolve against the media root."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="ta_310_media_")
        os.makedirs(os.path.join(self.media_root, str(CHAT_ID)))
        self.backup = _make_backup()
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_root  # a real path, not a MagicMock
        self.backup.config.skip_media_chat_ids = set()

    def tearDown(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _write(self, size=2048):
        target = os.path.join(self.media_root, REL_PATH)
        with open(target, "wb") as fh:
            fh.write(b"\x00" * size)
        return target

    def _seed(self, file_path, file_size=2048):
        self.backup.db.get_media_for_verification.return_value = [
            {
                "id": IMPORT_ID,
                "file_path": file_path,
                "file_size": file_size,
                "chat_id": CHAT_ID,
                "message_id": MSG_ID,
            }
        ]

    def test_an_imported_file_on_disk_is_not_re_downloaded(self):
        """THE BUG: the raw relative path resolved against the process CWD, so
        every imported file was judged missing. On a 1TB imported archive one
        VERIFY_MEDIA run meant re-downloading the lot."""
        self._write()
        self._seed(REL_PATH)

        _run(self.backup._verify_and_redownload_media())

        self.backup.client.get_messages.assert_not_awaited()

    def test_control_a_genuinely_missing_imported_file_is_still_re_downloaded(self):
        """POSITIVE CONTROL. Without this, a fix that simply skipped every
        relative path would pass the test above while verifying nothing."""
        self._seed(REL_PATH)  # nothing written to disk

        _run(self.backup._verify_and_redownload_media())

        self.backup.client.get_messages.assert_awaited()

    def test_control_a_truncated_imported_file_is_still_flagged(self):
        """POSITIVE CONTROL. Resolution must not become 'trust anything
        relative': a resolved file whose size disagrees is still corrupt."""
        self._write(size=10)
        self._seed(REL_PATH, file_size=512)

        _run(self.backup._verify_and_redownload_media())

        self.backup.client.get_messages.assert_awaited()

    def test_control_an_absolute_path_behaves_exactly_as_before(self):
        """Swept archives must not move. An absolute path is passed through
        untouched, so nothing about the majority case changes."""
        absolute = self._write()
        self._seed(absolute)

        _run(self.backup._verify_and_redownload_media())

        self.backup.client.get_messages.assert_not_awaited()

    def test_a_path_escaping_the_media_root_resolves_to_nothing(self):
        """The resolver's output is deleted and replaced by callers, so a value
        that climbs out of the archive must resolve to None, not to a real
        file elsewhere on the host."""
        from src.web.media_utils import resolve_stored_media_path

        self.assertIsNone(resolve_stored_media_path("../../etc/passwd", self.media_root))
        self.assertIsNone(resolve_stored_media_path(f"{CHAT_ID}/../../x", self.media_root))
        # A sibling directory sharing the root's name prefix is not inside it.
        self.assertIsNone(resolve_stored_media_path("../media-evil/x", self.media_root))

    def test_an_intact_file_is_never_flipped_to_not_downloaded(self):
        """Unreported half of #310: with the file unresolvable nothing was
        sidestepped, so a failed re-download fell through to
        mark_media_for_redownload and NULLed the pointer of a file that was
        sitting on disk the whole time."""
        self._write()
        record = {
            "id": IMPORT_ID,
            "file_path": REL_PATH,
            "file_size": 2048,
            "chat_id": CHAT_ID,
            "message_id": MSG_ID,
        }

        _run(self.backup._recover_failed_verification(record, None))

        self.backup.db.mark_media_for_redownload.assert_not_awaited()

    def test_control_a_missing_file_is_still_flipped_to_not_downloaded(self):
        """POSITIVE CONTROL for the guard above: a row that really has no file
        must still be handed to the pending-download retry."""
        record = {
            "id": IMPORT_ID,
            "file_path": REL_PATH,  # never written
            "chat_id": CHAT_ID,
            "message_id": MSG_ID,
        }

        _run(self.backup._recover_failed_verification(record, None))

        self.backup.db.mark_media_for_redownload.assert_awaited_once()


class TestImportedMediaCleanup(unittest.TestCase):
    """#310, deleting direction: the row went, the file stayed, forever."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="ta_310_clean_")
        os.makedirs(os.path.join(self.media_root, str(CHAT_ID)))
        self.backup = _make_backup()
        self.backup.config = MagicMock()
        self.backup.config.media_path = self.media_root
        self.target = os.path.join(self.media_root, REL_PATH)
        with open(self.target, "wb") as fh:
            fh.write(b"\x00" * 128)

    def tearDown(self):
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_an_imported_file_is_actually_deleted_with_its_row(self):
        """delete_media_for_chat always removed the row; the unlink silently
        missed, and nothing in the codebase ever reclaims a file with no row."""
        self.backup.db.get_media_for_chat = AsyncMock(
            return_value=[{"id": IMPORT_ID, "file_path": REL_PATH, "chat_id": CHAT_ID}]
        )
        self.backup.db.delete_media_for_chat = AsyncMock(return_value=1)

        _run(self.backup._cleanup_existing_media(CHAT_ID))

        self.assertFalse(os.path.exists(self.target), "imported file outlived its row")
