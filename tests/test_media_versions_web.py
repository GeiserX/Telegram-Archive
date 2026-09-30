"""The viewer serves a message's earlier media through the same chat bound (036).

An earlier photo or file an edit replaced is addressed as ``{message_id}_v{id}``
under the chat's ref. The row lookup is the authorization, as for the current
media: the chat and account come from the resolved chat and ride into SQL, so
a version id from another chat finds nothing. The edit history names each
earlier media with a URL, never with its stored path, and a no-download login
gets no URL. Runs against a real engine on both backends.
"""

import importlib
import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_media_versions_web_"))

pytest.importorskip("fastapi")

from telegram_archive.db.models import MediaVersion  # noqa: E402

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


async def _replaced(adapter) -> MediaVersion:
    """A message whose photo 111 an edit replaced with photo 222."""
    from datetime import datetime

    for chat_id in (CHAT_ID, OTHER_CHAT_ID):
        await adapter.upsert_chat({"id": chat_id, "type": "group", "title": "Test Group B"}, account_id=1)
        await adapter.insert_message(
            {"id": MESSAGE_ID, "chat_id": chat_id, "date": datetime(2026, 3, 1, 9), "text": "Look", "raw_data": {}},
            account_id=1,
        )
    await adapter.insert_media(
        {
            "id": f"{CHAT_ID}_{MESSAGE_ID}_photo",
            "type": "photo",
            "message_id": MESSAGE_ID,
            "chat_id": CHAT_ID,
            "file_name": "111.jpg",
            "file_path": f"{CHAT_ID}/111.jpg",
            "downloaded": True,
            "telegram_file_id": "111",
        },
        account_id=1,
    )
    await adapter.reconcile_media_row(CHAT_ID, MESSAGE_ID, "photo", account_id=1, telegram_file_id="222")
    async with adapter.db_manager.async_session_factory() as session:
        return (await session.execute(select(MediaVersion))).scalar_one()


async def test_a_version_key_serves_the_earlier_file_of_this_chat_only(main_mod, real_adapter):
    kept = await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    other = main_mod.ChatContext(account_id=1, chat_id=OTHER_CHAT_ID, ref=CHAT_REF, type="group")

    row = await main_mod._entitled_media_row(chat, f"{MESSAGE_ID}_v{kept.id}")
    assert row["file_path"] == f"{CHAT_ID}/111.jpg"

    for context, key in (
        (other, f"{MESSAGE_ID}_v{kept.id}"),
        (chat, f"{MESSAGE_ID + 1}_v{kept.id}"),
        (chat, f"{MESSAGE_ID}_v{kept.id + 1}"),
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
    kept = await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    user = main_mod.UserContext(username="viewer", role="master")

    (version,) = await main_mod.get_message_versions(MESSAGE_ID, chat=chat, user=user, limit=100)

    (media,) = version["media"]
    assert media["url"] == f"/media/{CHAT_REF}/{MESSAGE_ID}_v{kept.id}"
    assert "file_path" not in media
    assert "id" not in media
    assert str(CHAT_ID) not in str(media)


async def test_a_no_download_login_gets_no_url(main_mod, real_adapter):
    await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")
    user = main_mod.UserContext(username="viewer", role="viewer", no_download=True)

    (version,) = await main_mod.get_message_versions(MESSAGE_ID, chat=chat, user=user, limit=100)

    (media,) = version["media"]
    assert (media["url"], media["downloaded"], media["no_download"]) == (None, False, True)


async def test_a_writing_route_refuses_an_earlier_media(main_mod, real_adapter):
    kept = await _replaced(real_adapter)
    main_mod.db = real_adapter
    chat = main_mod.ChatContext(account_id=1, chat_id=CHAT_ID, ref=CHAT_REF, type="group")

    with pytest.raises(main_mod.HTTPException) as exc:
        await main_mod._entitled_media_row(chat, f"{MESSAGE_ID}_v{kept.id}", earlier_media=False)
    assert exc.value.status_code == 404


def test_only_a_v_and_digits_names_a_version(main_mod):
    assert main_mod._media_version_id("v12") == 12
    for type_part in ("video", "video_note", "v", "v1x", "12", "V12"):
        assert main_mod._media_version_id(type_part) is None
