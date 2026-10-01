"""The chat list preview: each row's second line is the chat's newest message.

9.0 shows under each chat's name what Telegram's own list shows there: the
newest message NOT deleted in Telegram, after its sender's first name in a group
("You" for the account's own message), or a word for a message with no text.
``/api/chats`` returns it with every row as ``preview``.

What this file pins, because each part can silently go the wrong way:

* A message deleted in Telegram is skipped, so the preview is the newest one the
  chat still holds in Telegram. A chat whose every message was deleted has none.
* The preview comes from the row's own ``(account_id, id)`` copy, the copy its
  ref opens. Two accounts' copies of one chat never borrow each other's newest
  message, and a restricted principal's preview stays inside its own grant.
* Text is folded onto one line and cut short on the server, so a long post never
  travels whole to fill one line.
* The sender label follows Telegram's list: a first name in a group, "You" for
  the account's own message, and nothing in a channel or for the other person
  in a private chat.

The adapter half runs on ``real_adapter``: SQLite AND PostgreSQL compile and run
the query. The viewer helper that turns a preview into words runs under node.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import pytest
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

from telegram_archive.db.adapter import CHAT_PREVIEW_TEXT_LENGTH, ChatScope

BASE = datetime(2026, 3, 1, 10, 0, 0)

GROUP = -8200001
CHANNEL = -1008200002
PRIVATE = 820000003
SHARED_GROUP = -8200004

ESME = 820000101  # a group member with a first name
NAMELESS = 820000102  # a sender the users table knows only by username
OWNER = 820000199  # the archived account's own user


async def add_user(adapter, user_id: int, first: str | None, last: str | None = None, username: str | None = None):
    await adapter.upsert_user({"id": user_id, "first_name": first, "last_name": last, "username": username})


async def add_chat(adapter, chat_id: int, chat_type: str, account_id: int = 1) -> None:
    await adapter.upsert_chat(
        {"id": chat_id, "type": chat_type, "title": f"preview fixture {chat_id}"}, account_id=account_id
    )


async def add_message(
    adapter,
    chat_id: int,
    message_id: int,
    minutes: int,
    text: str | None = "",
    *,
    sender: int | None = ESME,
    outgoing: bool = False,
    raw: dict | None = None,
    sender_name: str | None = None,
    account_id: int = 1,
) -> None:
    await adapter.insert_message(
        {
            "id": message_id,
            "chat_id": chat_id,
            "sender_id": sender,
            "sender_name": sender_name,
            "date": BASE + timedelta(minutes=minutes),
            "text": text,
            "is_outgoing": 1 if outgoing else 0,
            "raw_data": raw or {},
        },
        account_id=account_id,
    )


async def add_media(adapter, chat_id: int, message_id: int, media_type: str, account_id: int = 1) -> None:
    await adapter.insert_media(
        {
            "id": f"{chat_id}_{message_id}_{media_type}",
            "message_id": message_id,
            "chat_id": chat_id,
            "type": media_type,
            "file_size": 0,
            "downloaded": False,
        },
        account_id=account_id,
    )


async def previews(adapter, **kwargs) -> dict[tuple[int, int], dict | None]:
    rows = await adapter.get_all_chats(with_preview=True, **kwargs)
    return {(row["account_id"], row["id"]): row["preview"] for row in rows}


# ============================================================================
# Which message: the newest one not deleted in Telegram
# ============================================================================


class TestWhichMessage:
    async def test_the_newest_message_is_the_preview(self, real_adapter):
        await add_user(real_adapter, ESME, "Esme", "Lark")
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "older line")
        await add_message(real_adapter, GROUP, 2, 5, "newest line")

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert preview["message_id"] == 2
        assert preview["text"] == "newest line"
        assert preview["kind"] == "text"
        assert preview["date"] == BASE + timedelta(minutes=5)

    async def test_a_message_deleted_in_telegram_is_skipped(self, real_adapter):
        """Option C: the list shows what Telegram shows; the chat keeps the deletion."""
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "still in telegram")
        await add_message(real_adapter, GROUP, 2, 5, "deleted later")
        await real_adapter.mark_message_deleted(GROUP, 2, account_id=1)

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert preview["message_id"] == 1
        assert preview["text"] == "still in telegram"

    async def test_a_chat_whose_every_message_was_deleted_has_no_preview(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        await add_chat(real_adapter, CHANNEL, "channel")
        await add_message(real_adapter, GROUP, 1, 0, "gone")
        await real_adapter.mark_message_deleted(GROUP, 1, account_id=1)

        result = await previews(real_adapter)

        assert result[(1, GROUP)] is None
        assert result[(1, CHANNEL)] is None  # no message at all

    async def test_a_tie_on_the_date_goes_to_the_higher_message_id(self, real_adapter):
        """An album shares one date; Telegram's top message is its last one."""
        await add_chat(real_adapter, GROUP, "group")
        for message_id in (11, 13, 12):
            await add_message(real_adapter, GROUP, message_id, 0, f"album item {message_id}")

        assert (await previews(real_adapter))[(1, GROUP)]["message_id"] == 13

    async def test_the_list_without_previews_is_untouched(self, real_adapter):
        """Only the viewer's chat list asks; the backup's readers pay nothing."""
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "hello")

        rows = await real_adapter.get_all_chats()

        assert all("preview" not in row for row in rows)


# ============================================================================
# The copy it is read from: the row's own, inside the principal's grant
# ============================================================================


class TestTheCopyAndTheGrant:
    async def seed_shared(self, adapter) -> None:
        """A group two accounts archived; account 2's copy has the newer message."""
        for account_id in (1, 2):
            await add_chat(adapter, SHARED_GROUP, "group", account_id=account_id)
        await add_message(adapter, SHARED_GROUP, 1, 0, "account one copy", account_id=1)
        await add_message(adapter, SHARED_GROUP, 1, 0, "account two copy, older id", account_id=2)
        await add_message(adapter, SHARED_GROUP, 2, 9, "only account two has this", account_id=2)

    async def test_each_copy_previews_its_own_messages(self, real_adapter):
        await self.seed_shared(real_adapter)

        result = await previews(real_adapter)

        assert result[(1, SHARED_GROUP)]["text"] == "account one copy"
        assert result[(2, SHARED_GROUP)]["text"] == "only account two has this"

    async def test_a_folded_row_previews_the_copy_its_ref_opens(self, real_adapter):
        """The folded row opens account 1's copy, so it previews account 1's copy."""
        await self.seed_shared(real_adapter)

        rows = await real_adapter.get_all_chats(fold_shared=True, with_preview=True)

        assert [(row["account_id"], row["preview"]["text"]) for row in rows] == [(1, "account one copy")]

    async def test_a_viewer_restricted_to_one_account_previews_only_that_account(self, real_adapter):
        await self.seed_shared(real_adapter)

        rows = await real_adapter.get_all_chats(
            scope=ChatScope.build(accounts={1}), fold_shared=True, with_preview=True
        )

        assert [(row["account_id"], row["preview"]["text"]) for row in rows] == [(1, "account one copy")]
        assert "only account two has this" not in json.dumps([row["preview"] for row in rows], default=str)

    async def test_a_share_link_previews_only_the_chat_it_opens(self, real_adapter):
        await self.seed_shared(real_adapter)
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 30, "a chat the link does not open")
        shared = next(row for row in await real_adapter.get_all_chats() if row["account_id"] == 2)

        rows = await real_adapter.get_all_chats(
            scope=ChatScope.build(refs={shared["ref"]}), fold_shared=True, with_preview=True
        )

        assert [row["preview"]["text"] for row in rows] == ["only account two has this"]


# ============================================================================
# The text: one line, cut short on the server
# ============================================================================


class TestText:
    async def test_whitespace_folds_onto_one_line(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "  first line\n\nsecond\tline  ")

        assert (await previews(real_adapter))[(1, GROUP)]["text"] == "first line second line"

    async def test_a_long_text_is_cut_with_an_ellipsis(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "word " * 400)

        text = (await previews(real_adapter))[(1, GROUP)]["text"]

        assert len(text) <= CHAT_PREVIEW_TEXT_LENGTH + 1
        assert text.endswith("…")
        assert text.startswith("word word")

    async def test_a_text_that_fits_is_not_marked_cut(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        exact = "x" * CHAT_PREVIEW_TEXT_LENGTH
        await add_message(real_adapter, GROUP, 1, 0, exact)

        assert (await previews(real_adapter))[(1, GROUP)]["text"] == exact


# ============================================================================
# The kind: a word for a message with no text
# ============================================================================


class TestKind:
    async def test_media_with_no_text_carries_its_kind_and_no_text(self, real_adapter):
        kinds = ["photo", "voice", "video_note", "sticker", "document", "geo", "contact", "venue"]
        for index, kind in enumerate(kinds):
            chat_id = GROUP - index
            await add_chat(real_adapter, chat_id, "group")
            await add_message(real_adapter, chat_id, 1, index, "")
            await add_media(real_adapter, chat_id, 1, kind)

        result = await previews(real_adapter)

        for index, kind in enumerate(kinds):
            preview = result[(1, GROUP - index)]
            assert (preview["kind"], preview["text"]) == (kind, None)

    async def test_a_caption_rides_with_its_media_kind(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "Top of the pass")
        await add_media(real_adapter, GROUP, 1, "photo")

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert (preview["kind"], preview["text"]) == ("photo", "Top of the pass")

    async def test_a_poll_gives_its_question(self, real_adapter):
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "", raw={"poll": {"question": "Which trail next?", "answers": []}})

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert (preview["kind"], preview["text"]) == ("poll", "Which trail next?")

    async def test_a_service_row_keeps_its_sentence_and_names_no_sender(self, real_adapter):
        """The sentence already names its actor; "Esme: Esme joined" would say it twice."""
        await add_user(real_adapter, ESME, "Esme")
        await add_chat(real_adapter, GROUP, "group")
        await add_message(
            real_adapter,
            GROUP,
            1,
            0,
            "Esme joined the group via invite link",
            raw={"service_type": "service", "action_type": "chat_joined_by_link"},
        )

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert preview["kind"] == "service"
        assert preview["text"] == "Esme joined the group via invite link"
        assert preview["sender"] is None
        assert preview["action"] is None

    async def test_a_service_row_with_no_sentence_sends_its_action(self, real_adapter):
        """Rows from before 7.28 stored no sentence; the viewer words them from the action."""
        await add_chat(real_adapter, GROUP, "group")
        await add_message(
            real_adapter,
            GROUP,
            1,
            0,
            "",
            raw={"service_type": "service", "action_type": "chat_edit_title", "new_title": "Trail Crew"},
        )

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert (preview["kind"], preview["text"]) == ("service", None)
        assert (preview["action"], preview["action_title"]) == ("chat_edit_title", "Trail Crew")

    async def test_no_text_and_no_media_row_is_a_bare_message(self, real_adapter):
        """Media capture off leaves a caption-less photo with neither; it is still a message."""
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, None)

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert (preview["kind"], preview["text"]) == ("message", None)


# ============================================================================
# The sender label, as Telegram's list writes it
# ============================================================================


class TestSender:
    async def test_a_group_names_the_sender_by_first_name(self, real_adapter):
        await add_user(real_adapter, ESME, "Esme", "Lark", "esme_l")
        await add_chat(real_adapter, GROUP, "group")
        await add_message(real_adapter, GROUP, 1, 0, "hello", sender_name="Esme Lark")

        preview = (await previews(real_adapter))[(1, GROUP)]

        assert (preview["sender"], preview["outgoing"]) == ("Esme", False)

    async def test_a_sender_with_no_first_name_falls_back_to_the_stored_name(self, real_adapter):
        """A channel posting in a group has no user row; its title is the name."""
        await add_user(real_adapter, NAMELESS, None, None, "nameless_one")
        await add_chat(real_adapter, GROUP, "group")
        await add_chat(real_adapter, GROUP - 1, "group")
        await add_message(real_adapter, GROUP, 1, 0, "hi", sender=NAMELESS)
        await add_message(real_adapter, GROUP - 1, 1, 0, "posted", sender=-1008299999, sender_name="Harbor Notices")

        result = await previews(real_adapter)

        assert result[(1, GROUP)]["sender"] == "nameless_one"
        assert result[(1, GROUP - 1)]["sender"] == "Harbor Notices"

    async def test_the_account_s_own_message_is_you_in_a_group_and_a_private_chat(self, real_adapter):
        await add_user(real_adapter, OWNER, "Owner")
        await add_chat(real_adapter, GROUP, "group")
        await add_chat(real_adapter, PRIVATE, "private")
        await add_message(real_adapter, GROUP, 1, 0, "mine", sender=OWNER, outgoing=True)
        await add_message(real_adapter, PRIVATE, 1, 0, "mine too", sender=OWNER, outgoing=True)

        result = await previews(real_adapter)

        assert result[(1, GROUP)]["sender"] == "You"
        assert result[(1, PRIVATE)]["sender"] == "You"

    async def test_the_other_person_in_a_private_chat_and_a_channel_name_no_one(self, real_adapter):
        await add_user(real_adapter, ESME, "Esme")
        await add_chat(real_adapter, PRIVATE, "private")
        await add_chat(real_adapter, CHANNEL, "channel")
        await add_message(real_adapter, PRIVATE, 1, 0, "from them")
        await add_message(real_adapter, CHANNEL, 1, 0, "a post", sender=None)
        await add_message(real_adapter, CHANNEL, 2, 1, "an admin's post", sender=OWNER, outgoing=True)

        result = await previews(real_adapter)

        assert result[(1, PRIVATE)]["sender"] is None
        assert result[(1, CHANNEL)]["sender"] is None


# ============================================================================
# The route: every principal gets previews through its own scope
# ============================================================================


pytest.importorskip("fastapi")
pytest.importorskip("httpx")
os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_preview_"))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from telegram_archive.web import main as web_main  # noqa: E402


@pytest.fixture
async def app_on(real_adapter):
    saved = (web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, web_main.config.display_chat_ids)
    web_main.db = real_adapter
    web_main.AUTH_ENABLED = False
    web_main.ALLOW_ANONYMOUS_VIEWER = True
    web_main.config.display_chat_ids = set()
    try:
        yield real_adapter
    finally:
        web_main.app.dependency_overrides.clear()
        (web_main.db, web_main.AUTH_ENABLED, web_main.ALLOW_ANONYMOUS_VIEWER, web_main.config.display_chat_ids) = saved


def as_principal(**fields) -> None:
    user = web_main.UserContext(username="preview-test", role=fields.pop("role", "viewer"), **fields)
    web_main.app.dependency_overrides[web_main.require_auth] = lambda: user


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")


class TestRoute:
    async def seed(self, adapter) -> None:
        await add_user(adapter, ESME, "Esme")
        for account_id, chat_id, text in ((1, GROUP, "account one says hi"), (2, GROUP - 1, "account two says hi")):
            await add_chat(adapter, chat_id, "group", account_id=account_id)
            await add_message(adapter, chat_id, 1, 0, text, account_id=account_id)
            await add_message(adapter, chat_id, 2, 5, "deleted in telegram", account_id=account_id)
            await adapter.mark_message_deleted(chat_id, 2, account_id=account_id)

    async def test_every_row_carries_the_newest_kept_message(self, app_on):
        await self.seed(app_on)
        as_principal(role="master", allowed_accounts=None)

        async with client() as http:
            resp = await http.get("/api/chats?limit=50")

        assert resp.status_code == 200
        rows = {row["id"]: row["preview"] for row in resp.json()["chats"]}
        assert rows[GROUP]["text"] == "account one says hi"
        assert rows[GROUP]["sender"] == "Esme"
        assert rows[GROUP]["date"] == "2026-03-01T10:00:00"
        assert "deleted in telegram" not in resp.text

    async def test_a_restricted_viewer_sees_only_its_accounts_previews(self, app_on):
        await self.seed(app_on)
        as_principal(allowed_accounts={2})

        async with client() as http:
            resp = await http.get("/api/chats?limit=50")

        assert [row["preview"]["text"] for row in resp.json()["chats"]] == ["account two says hi"]
        assert "account one says hi" not in resp.text

    async def test_a_no_download_login_still_sees_the_text(self, app_on):
        """No-download hides files and transcripts, not the text the chat shows."""
        await self.seed(app_on)
        as_principal(allowed_accounts=None, no_download=True)

        async with client() as http:
            resp = await http.get("/api/chats?limit=50")

        assert {row["preview"]["text"] for row in resp.json()["chats"]} == {
            "account one says hi",
            "account two says hi",
        }


# ============================================================================
# The viewer: a preview in words, executed under node
# ============================================================================

HTML = INDEX_HTML.read_text(encoding="utf-8")

_DECLARATIONS = (
    "const SERVICE_PREDICATES = {",
    "const SERVICE_TITLE_PREDICATES = {",
    "const SERVICE_UNKNOWN_SUBJECT = {",
    "const serviceRawData = (msg) =>",
    "const serviceActionType = (msg) =>",
    "const serviceActorIsSender = (msg) =>",
    "const serviceMessagePredicate = (msg) =>",
    "const serviceMessageView = (msg) =>",
    "const CHAT_PREVIEW_WORDS = {",
    "const chatPreviewText = (preview) =>",
)


def _words(previews: list) -> list:
    return _run_setup_program(
        HTML,
        _DECLARATIONS,
        "const getSenderName = () => { throw new Error('a preview has no sender to look up') }\n",
        f"console.log(JSON.stringify({json.dumps(previews)}.map(chatPreviewText)))",
    )


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestViewerWords(unittest.TestCase):
    def test_text_shows_as_sent(self) -> None:
        self.assertEqual(_words([{"kind": "text", "text": "See you at seven"}]), ["See you at seven"])

    def test_a_caption_wins_over_the_media_word(self) -> None:
        self.assertEqual(_words([{"kind": "photo", "text": "Top of the pass"}]), ["Top of the pass"])

    def test_media_with_no_text_is_named_by_a_word(self) -> None:
        kinds = ["photo", "video", "voice", "video_note", "document", "sticker", "geo", "contact", "venue"]
        self.assertEqual(
            _words([{"kind": kind, "text": None} for kind in kinds]),
            ["Photo", "Video", "Voice message", "Video message", "File", "Sticker", "Location", "Contact", "Location"],
        )

    def test_a_poll_shows_its_question_after_the_chart_mark(self) -> None:
        self.assertEqual(
            _words([{"kind": "poll", "text": "Which trail next?"}, {"kind": "poll", "text": None}]),
            ["\U0001f4ca Which trail next?", "Poll"],
        )

    def test_a_service_row_without_a_sentence_is_worded_from_its_action(self) -> None:
        self.assertEqual(
            _words(
                [
                    {"kind": "service", "text": None, "action": "chat_edit_photo"},
                    {"kind": "service", "text": None, "action": "chat_edit_title", "action_title": "Trail Crew"},
                    {"kind": "service", "text": None, "action": "something_new"},
                    {"kind": "service", "text": "Esme left the group"},
                ]
            ),
            [
                "Someone changed the group photo",
                'Someone changed the group name to "Trail Crew"',
                "Service message",
                "Esme left the group",
            ],
        )

    def test_an_unknown_or_hostile_kind_never_reads_the_prototype(self) -> None:
        self.assertEqual(
            _words([{"kind": "constructor", "text": None}, {"kind": "message", "text": None}, None]),
            ["Message", "Message", ""],
        )
