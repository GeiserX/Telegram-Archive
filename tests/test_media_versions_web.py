"""The viewer serves a message's earlier media through the same chat bound (036).

An earlier photo or file an edit replaced is addressed as ``{message_id}_v{n}``
under the chat's ref, n its number among the message's earlier media. The row
lookup is the authorization, as for the current media: the chat and account
come from the resolved chat and ride into SQL, so a key from another chat
finds nothing. The edit history names each
earlier media with a URL, never with its stored path, and a no-download login
gets no URL. Runs against a real engine on both backends.
"""

import importlib
import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_media_versions_web_"))

pytest.importorskip("fastapi")

from telegram_archive.db.models import MediaTranscript, MediaVersion  # noqa: E402

CHAT_ID = -1009990011
OTHER_CHAT_ID = -1009990012
MESSAGE_ID = 7
CHAT_REF = "mediaVersionsRef0001"
ANON_ENV = {"VIEWER_USERNAME": "", "VIEWER_PASSWORD": "", "ALLOW_ANONYMOUS_VIEWER": "true"}


@pytest.fixture
def main_mod(monkeypatch):
    for key, value in ANON_ENV.items():
        monkeypatch.setenv(key, value)
    import telegram_archive.web.main as module

    importlib.reload(module)
    module.db = AsyncMock()
    return module


async def _replaced(adapter, media_type: str = "photo") -> MediaVersion:
    """A message whose photo 111 (or voice note) an edit replaced with 222."""
    for chat_id in (CHAT_ID, OTHER_CHAT_ID):
        await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Test Group B"}, account_id=1)
        await adapter.insert_message(
            {"id": MESSAGE_ID, "chat_id": chat_id, "date": datetime(2026, 3, 1, 9), "text": "Look", "raw_data": {}},
            account_id=1,
        )
    extension = "jpg" if media_type == "photo" else "ogg"
    # The other chat's message is replaced first, so the table ids of the
    # earlier media (1 there, 2 here) differ from their numbers (1 in each).
    for chat_id in (OTHER_CHAT_ID, CHAT_ID):
        await adapter.insert_media(
            {
                "id": f"{chat_id}_{MESSAGE_ID}_{media_type}",
                "type": media_type,
                "message_id": MESSAGE_ID,
                "chat_id": chat_id,
                "file_name": f"111.{extension}",
                "file_path": f"{chat_id}/111.{extension}",
                "mime_type": "image/jpeg" if media_type == "photo" else "audio/ogg",
                "downloaded": True,
                "telegram_file_id": "111",
            },
            account_id=1,
        )
        await adapter.reconcile_media_row(
            chat_id, MESSAGE_ID, media_type, account_id=1, telegram_file_id="222", edit_date=datetime(2026, 3, 1, 10)
        )
    async with adapter.db_manager.async_session_factory() as session:
        return (await session.execute(select(MediaVersion).where(MediaVersion.chat_id == CHAT_ID))).scalar_one()


async def test_a_version_key_serves_the_earlier_file_of_this_chat_only(main_mod, real_adapter):
    kept = await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    other = main_mod.ChatContext(account_id=1, chat_id=OTHER_CHAT_ID, ref=CHAT_REF, type="group")

    assert kept.id == 2  # the number in the key is not the table id
    row = await main_mod._entitled_media_row(chat, f"{MESSAGE_ID}_v1")
    assert row["file_path"] == f"{CHAT_ID}/111.jpg"
    assert row["id"] == kept.media_id
    # The same key under the other chat's ref is that chat's own earlier media.
    row = await main_mod._entitled_media_row(other, f"{MESSAGE_ID}_v1")
    assert row["file_path"] == f"{OTHER_CHAT_ID}/111.jpg"

    for context, key in (
        (chat, f"{MESSAGE_ID + 1}_v1"),
        (chat, f"{MESSAGE_ID}_v2"),
        (chat, f"{MESSAGE_ID}_v{kept.id + 100}"),
    ):
        with pytest.raises(main_mod.HTTPException) as exc:
            await main_mod._entitled_media_row(context, key)
        assert exc.value.status_code == 404


async def test_control_the_type_key_still_serves_the_current_media(main_mod, real_adapter):
    await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")

    row = await main_mod._entitled_media_row(chat, f"{MESSAGE_ID}_photo")
    assert row["file_path"] is None  # the new photo is not downloaded yet
    assert row["id"].endswith("_v1")


async def test_the_history_names_earlier_media_by_url_never_by_path(main_mod, real_adapter):
    await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    user = main_mod.UserContext(username="viewer", role="master")

    (version,) = await main_mod.get_message_versions(MESSAGE_ID, chat=chat, user=user, limit=100)

    (media,) = version["media"]
    # The number of the earlier media within the message, never a table id.
    assert media["url"] == f"/media/{CHAT_REF}/{MESSAGE_ID}_v1"
    assert "file_path" not in media
    assert "id" not in media and "number" not in media
    assert str(CHAT_ID) not in str(media)


async def test_a_no_download_login_gets_no_url(main_mod, real_adapter):
    await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    user = main_mod.UserContext(username="viewer", role="viewer", no_download=True)

    (version,) = await main_mod.get_message_versions(MESSAGE_ID, chat=chat, user=user, limit=100)

    (media,) = version["media"]
    assert (media["url"], media["downloaded"], media["no_download"]) == (None, False, True)


async def test_a_writing_route_refuses_an_earlier_media(main_mod, real_adapter, monkeypatch):
    """Asking for a transcript of an earlier voice note answers 404 and writes nothing."""
    await _replaced(real_adapter, "voice")
    main_mod.db = real_adapter
    monkeypatch.setattr(main_mod, "_transcription_on", lambda: True)
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    user = main_mod.UserContext(username="viewer", role="master")

    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod.ask_chat_media_transcript(f"{MESSAGE_ID}_v1", chat=chat, user=user)
    assert exc.value.status_code == 404
    async with real_adapter.db_manager.async_session_factory() as session:
        assert (await session.execute(select(MediaTranscript))).scalars().all() == []

    # The earlier voice note itself is still served to a reader.
    row = await main_mod._entitled_media_row(chat, f"{MESSAGE_ID}_v1")
    assert row["type"] == "voice"


async def test_a_number_past_the_key_is_the_uniform_404(main_mod, real_adapter):
    """Never a database error: the key's number is bounded before any query."""
    await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")

    for key in ("1_v99999999999", f"{MESSAGE_ID}_v3000000000", f"{MESSAGE_ID}_v0", f"{MESSAGE_ID}_v01"):
        with pytest.raises(main_mod.HTTPException) as exc:
            await main_mod._entitled_media_row(chat, key)
        assert exc.value.status_code == 404


async def test_the_current_media_url_changes_when_an_edit_replaces_it(main_mod, real_adapter):
    """A browser that cached the old photo's bytes (the thumbnail for a day)
    must not show them for the new photo: the URL itself changes."""
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")

    async def current_url() -> str:
        media = await real_adapter.get_media_for_message(CHAT_ID, MESSAGE_ID + 1, "photo", account_id=1)
        messages = [{"id": MESSAGE_ID + 1, "sender_id": None, "media": dict(media)}]
        main_mod._attach_message_payload_urls(messages, chat)
        return messages[0]["media"]["url"]

    await _replaced(real_adapter)
    # A second message, archived with its photo, that an edit then replaces.
    await real_adapter.insert_message(
        {"id": MESSAGE_ID + 1, "chat_id": CHAT_ID, "date": datetime(2026, 3, 1, 9), "text": "Two", "raw_data": {}},
        account_id=1,
    )
    await real_adapter.insert_media(
        {
            "id": f"{CHAT_ID}_{MESSAGE_ID + 1}_photo",
            "type": "photo",
            "message_id": MESSAGE_ID + 1,
            "chat_id": CHAT_ID,
            "file_name": "444.jpg",
            "file_path": f"{CHAT_ID}/444.jpg",
            "downloaded": True,
            "telegram_file_id": "444",
        },
        account_id=1,
    )
    before = await current_url()

    replaced = await real_adapter.reconcile_media_row(
        CHAT_ID, MESSAGE_ID + 1, "photo", account_id=1, telegram_file_id="333", edit_date=datetime(2026, 3, 1, 10)
    )
    assert replaced["replaced"] is True
    await real_adapter.insert_media(
        {
            "id": replaced["id"],
            "type": "photo",
            "message_id": MESSAGE_ID + 1,
            "chat_id": CHAT_ID,
            "file_name": "333.jpg",
            "file_path": f"{CHAT_ID}/333.jpg",
            "downloaded": True,
            "telegram_file_id": "333",
        },
        account_id=1,
    )
    after = await current_url()

    assert before == f"/media/{CHAT_REF}/{MESSAGE_ID + 1}_photo"
    assert after.startswith(f"/media/{CHAT_REF}/{MESSAGE_ID + 1}_photo?v=")
    assert after != before


def test_only_a_v_and_a_number_names_a_version(main_mod):
    assert main_mod._media_version_number("v12") == 12
    for type_part in ("video", "video_note", "v", "v1x", "12", "V12", "v0", "v1234567890"):
        assert main_mod._media_version_number(type_part) is None
