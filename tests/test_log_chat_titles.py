"""LOG_CHAT_TITLES (#439): the operator may name the chat on the progress lines.

The project's standing rule is that logs carry no chat identifier at all. This is
the one sanctioned relaxation: opt-in, titles only, never an id, and never the
person behind a private chat. Everything here is about keeping that promise
exact — the default is byte-identical to the line that shipped, and a title is
attacker-controlled input that reaches a terminal, `docker logs` and possibly a
log aggregator, so it is sanitised before it is ever written.

The guard's own tests (tests/test_no_account_pii_in_logs.py) pin the other half:
that this helper is the ONLY route a title has, and that removing its exemption
turns the real call sites red.
"""

import ast
import logging
import os
import unicodedata
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.tl.types import User

from src.message_utils import _LOG_TITLE_MAX_CHARS, chat_title_for_log
from src.telegram_backup import TelegramBackup

OFF = MagicMock()
OFF.log_chat_titles = False
ON = MagicMock()
ON.log_chat_titles = True


class _Titled:
    """A Chat/Channel-shaped entity: it has a title and is not a User."""

    def __init__(self, title):
        self.title = title


class TestTheFlagIsOff(unittest.TestCase):
    def test_the_default_is_off(self):
        """Read from the real Config, not from a mock of it."""
        from src.config import Config

        base = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_PHONE": "+1"}
        with patch.dict(os.environ, base, clear=True), patch("os.makedirs"):
            self.assertFalse(Config().log_chat_titles)

    def test_anything_that_is_not_literally_true_fails_closed(self):
        """A truthy-looking value is not consent. A bare MagicMock is truthy for
        every attribute, which is exactly how a test double would silently switch
        this on for a chat it was never meant to name."""
        for config in (OFF, MagicMock(), object(), None):
            with self.subTest(config=type(config).__name__):
                self.assertEqual("", chat_title_for_log(_Titled("Engineering"), config))
        for value in ("true", "yes", 1, [1]):
            with self.subTest(value=value):
                config = MagicMock()
                config.log_chat_titles = value
                self.assertEqual("", chat_title_for_log(_Titled("Engineering"), config))


class TestTheFlagIsOn(unittest.TestCase):
    def test_a_group_or_channel_is_named_by_its_title(self):
        self.assertEqual(': "Engineering"', chat_title_for_log(_Titled("Engineering"), ON))

    def test_a_private_chat_is_named_by_kind_never_by_person(self):
        """A telethon User has no title. The only stand-ins are first_name,
        last_name, username and phone — the very attributes #272 removed from the
        logs, and the most sensitive metadata the archive holds."""
        user = MagicMock(spec=User)
        user.first_name, user.last_name, user.username = "Ada", "Lovelace", "ada"
        label = chat_title_for_log(user, ON)
        self.assertEqual(": private chat", label)
        for secret in ("Ada", "Lovelace", "ada"):
            self.assertNotIn(secret, label)

    def test_the_kind_marker_is_unquoted_so_a_title_cannot_impersonate_it(self):
        """Quoting is the delimiter: `: private chat` is a kind, `: "private
        chat"` is a channel that chose that name. The reader can tell them apart."""
        self.assertEqual(': "private chat"', chat_title_for_log(_Titled("private chat"), ON))
        self.assertEqual(": private chat", chat_title_for_log(MagicMock(spec=User), ON))

    def test_an_entity_without_a_usable_title_is_untitled_rather_than_guessed(self):
        for entity in (_Titled(None), _Titled(""), _Titled(123), MagicMock(), _Titled("   ")):
            with self.subTest(entity=repr(entity)[:40]):
                self.assertEqual(": untitled chat", chat_title_for_log(entity, ON))


class TestSanitisation(unittest.TestCase):
    """Every payload is asserted to be hostile BEFORE its output is asserted safe.

    Without that precondition the test passes when a payload is accidentally
    flattened — by an editor stripping invisibles, or a copy-paste — and then it
    pins nothing at all.
    """

    UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

    HOSTILE = {
        "newline forges a record": "Ops\n2026-01-01 00:00:00 - src - CRITICAL - disk lost",
        "carriage return overwrites": "Ops\rBackup complete",
        "ansi clear screen": "Ops\x1b[2J\x1b[H",
        "osc 8 hyperlink": "Ops\x1b]8;;http://evil.example\x07click\x1b]8;;\x07",
        "c1 control": "Ops\x9bJ",
        "nul": "Ops\x00Engineering",
        "backspace rewrite": "Ops\x08\x08\x08Safe",
        "rtl override": "Ops‮gnirts desrever",
        "isolate": "Ops⁦hidden⁩",
        "zero width space": "Ops​Engineering",
        "byte order mark": "﻿Ops",
        "line separator": "Ops next line",
        "paragraph separator": "Ops next para",
        "lone surrogate": "Ops\ud800Engineering",
    }

    def test_no_hostile_title_reaches_the_log(self):
        for name, title in self.HOSTILE.items():
            with self.subTest(payload=name):
                self.assertTrue(
                    [c for c in title if unicodedata.category(c) in self.UNSAFE],
                    f"{name}: the payload itself is no longer hostile, so this proves nothing",
                )
                label = chat_title_for_log(_Titled(title), ON)
                self.assertNotIn("\n", label)
                self.assertNotIn("\r", label)
                self.assertEqual([], [c for c in label if unicodedata.category(c) in self.UNSAFE])
                label.encode("utf-8")  # a lone surrogate here would delete the whole record

    def test_a_lone_surrogate_would_otherwise_destroy_the_record(self):
        """Shown as a precondition: without the Cs rule the handler raises while
        formatting and the log line disappears entirely — the title deletes its
        own evidence."""
        with self.assertRaises(UnicodeEncodeError):
            "Ops\ud800Engineering".encode()
        self.assertEqual(': "Ops Engineering"', chat_title_for_log(_Titled("Ops\ud800Engineering"), ON))

    def test_control_characters_become_spaces_rather_than_vanishing(self):
        """Tampering must read as tampering. Deleting the characters would
        reflow into a different, plausible-looking name."""
        self.assertEqual(': "Ops Safe"', chat_title_for_log(_Titled("Ops\x08\x08\x08Safe"), ON))
        self.assertEqual(': "Fo o"', chat_title_for_log(_Titled("Fo\x00o"), ON))

    def test_double_quotes_fold_so_the_delimiters_still_delimit(self):
        label = chat_title_for_log(_Titled('a" (archived): "b'), ON)
        self.assertEqual(": \"a' (archived): 'b\"", label)
        self.assertEqual(2, label.count('"'), "only the pair this function added")

    def test_a_long_title_is_capped_but_still_identifies_the_chat(self):
        label = chat_title_for_log(_Titled("Engineering " + "x" * 4096), ON)
        self.assertEqual(_LOG_TITLE_MAX_CHARS + 4, len(label), 'the cap plus `: ""`')
        self.assertTrue(label.endswith('…"'))
        self.assertIn("Engineering", label)

    def test_padding_is_stripped_rather_than_hiding_the_name(self):
        for pad in (" " * 300, "​" * 300, "\x00" * 300):
            with self.subTest(pad=repr(pad[:1])):
                self.assertEqual(': "Engineering"', chat_title_for_log(_Titled(pad + "Engineering"), ON))

    def test_private_use_and_unassigned_codepoints_survive(self):
        """Neither can end a line or drive a terminal, and stripping them would
        mangle ordinary titles."""
        self.assertEqual("Co", unicodedata.category(""))
        self.assertEqual(': "Ops "', chat_title_for_log(_Titled("Ops "), ON))


def _user():
    """A real telethon User. `bot` is set in __init__, so it is absent from
    dir(User) and MagicMock(spec=User) refuses it — which backup_all needs."""
    return User(id=7, first_name="Ada", last_name="Lovelace", username="ada")


def _dialog(entity):
    dialog = MagicMock()
    dialog.entity = entity
    # A real datetime: dialog_sort_key calls .timestamp() on it, and two MagicMocks
    # are not orderable, so a multi-dialog run raises without this.
    dialog.date = datetime(2026, 1, 1, tzinfo=UTC)
    return dialog


def _sweep_backup(*, log_titles, main_dialogs, archived_dialogs=()):
    """A real backup_all run over stubbed dialogs, modelled on
    tests/test_chat_migration.py::_make_sweep_backup."""
    backup = TelegramBackup.__new__(TelegramBackup)
    backup.account_id = 1
    cfg = MagicMock()
    cfg.log_chat_titles = log_titles  # a real bool: the gate demands `is True`
    cfg.whitelist_mode = False
    cfg.chat_ids = set()
    cfg.phone = "+1234567890"
    cfg.priority_chat_ids = set()
    cfg.verify_media = False
    cfg.follow_chat_migrations = False
    cfg.should_backup_chat = MagicMock(return_value=True)
    backup.config = cfg

    db = AsyncMock()
    db.get_last_message_id = AsyncMock(return_value=0)
    db.calculate_and_store_statistics = AsyncMock(
        return_value={"chats": 1, "messages": 1, "media_files": 0, "total_size_mb": 0}
    )
    backup.db = db

    client = AsyncMock()
    me = MagicMock()
    me.first_name, me.id = "T", 1
    client.get_me = AsyncMock(return_value=me)
    client.start = AsyncMock()
    backup.client = client

    counter = iter(range(-1000, 0))
    backup._get_marked_id = MagicMock(side_effect=lambda e: getattr(e, "mid", None) or next(counter))
    backup._get_dialogs = AsyncMock(
        side_effect=lambda archived=False: list(archived_dialogs) if archived else list(main_dialogs)
    )
    backup._followed_migration_ids = set()
    backup._load_resweep_cycle = AsyncMock()
    backup._load_followed_migrations = AsyncMock()
    backup._finalize_resweep_cycle = AsyncMock()
    backup._reconcile_migrations = AsyncMock()
    backup._backup_folders = AsyncMock()
    backup._retry_pending_media_downloads = AsyncMock()
    backup._backup_dialog = AsyncMock(return_value=0)
    return backup


class TestTheRealProgressLines(unittest.IsolatedAsyncioTestCase):
    """Drives the real backup_all so the two call sites are proven wired."""

    async def _run(self, *, log_titles, main, archived=()):
        backup = _sweep_backup(log_titles=log_titles, main_dialogs=main, archived_dialogs=archived)
        with self.assertLogs("src.telegram_backup", level="INFO") as captured:
            await backup.backup_all()
        return [record.getMessage() for record in captured.records]

    async def test_off_is_byte_identical_to_the_line_that_shipped(self):
        messages = await self._run(log_titles=False, main=[_dialog(_Titled("Engineering"))])
        self.assertIn("[1/1] Backing up", messages)
        self.assertNotIn("Engineering", " ".join(messages))

    async def test_on_names_the_chat_on_the_main_progress_line(self):
        messages = await self._run(log_titles=True, main=[_dialog(_Titled("Engineering"))])
        self.assertIn('[1/1] Backing up: "Engineering"', messages)

    async def test_on_counts_correctly_across_several_chats(self):
        messages = await self._run(
            log_titles=True,
            main=[_dialog(_Titled("Alpha")), _dialog(_user()), _dialog(_Titled("Ops\nforged"))],
        )
        joined = "\n".join(messages)
        self.assertIn('[1/3] Backing up: "Alpha"', joined)
        self.assertIn("Backing up: private chat", joined)
        self.assertIn('Backing up: "Ops forged"', joined, "the newline is neutralised, not the text")

    async def test_on_never_puts_a_chat_id_on_a_progress_line(self):
        entity = _Titled("Engineering")
        entity.mid = -1002001563966
        messages = await self._run(log_titles=True, main=[_dialog(entity)])
        self.assertNotIn("1002001563966", " ".join(messages))

    async def test_on_never_names_the_person_behind_a_private_chat(self):
        messages = await self._run(log_titles=True, main=[_dialog(_user())])
        joined = " ".join(messages)
        self.assertIn("Backing up: private chat", joined)
        for secret in ("Ada", "Lovelace"):
            self.assertNotIn(secret, joined)


class TestTheRelaxationAnnouncesItself(unittest.TestCase):
    """An operator who turned this on months ago should be reminded every start."""

    def _messages_while_building(self, **env):
        from src.config import Config

        base = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "h", "TELEGRAM_PHONE": "+1", **env}
        with (
            patch.dict(os.environ, base, clear=True),
            patch("os.makedirs"),
            self.assertLogs("src.config", level="DEBUG") as captured,
        ):
            Config()
        return [r.getMessage() for r in captured.records]

    def test_on_warns_once_and_names_the_variable(self):
        warnings = [m for m in self._messages_while_building(LOG_CHAT_TITLES="true") if "LOG_CHAT_TITLES" in m]
        self.assertEqual(1, len(warnings))
        self.assertIn("chat titles will appear", warnings[0])

    def test_off_says_nothing_new(self):
        self.assertNotIn("LOG_CHAT_TITLES", " ".join(self._messages_while_building()))


class TestTheGateIsTheOnlyRoute(unittest.TestCase):
    """A structural pin that complements the AST guard: the helper the backup
    imports is the one in message_utils, not a local rebinding."""

    def test_the_backup_imports_the_real_helper(self):
        import src.telegram_backup as backup_module
        from src import message_utils

        self.assertIs(backup_module.chat_title_for_log, message_utils.chat_title_for_log)

    def test_the_progress_lines_call_it_inline(self):
        source = (
            backup_path := __import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "telegram_backup.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(2, source.count("chat_title_for_log(entity, self.config)"), str(backup_path))
        tree = ast.parse(source)
        hoisted = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "chat_title_for_log"
        ]
        self.assertEqual([], hoisted, "the label must not be hoisted into a local and reused on other lines")


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
