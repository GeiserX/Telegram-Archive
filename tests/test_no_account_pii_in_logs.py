"""#272: the account holder's identity must never reach the logs.

The project already forbids logging chat ids, topic ids and titles, and message
content. Nine call sites wrote the account's own first name, last name,
username, phone number and Telegram user id instead, and survived for months
because the rule was written down (2026-04-18) long after those lines were
(2025-11-25 through 2026-01-18). Nothing checked, so nothing caught it.

This is that check. It is deliberately a scan of whole trees rather than
assertions about the known lines: pinning the known sites would not have
prevented the original drift, because every one of them predated the rule.

It covers ``src`` AND ``scripts``. The first draft scanned only ``src`` and was
green while ``scripts/restore_chat.py`` logged the same name and phone the fix
had just removed — a scanner whose blind spot contains a live violation is worse
than none, because it certifies the gap. ``scripts`` is not incidental: the
documented session-recovery path runs those files under ``docker run``, so their
output lands in the container log stream like everything else.

``print`` counts as a logging call here for the same reason: these scripts print
to stdout, which is captured identically. Anything unrecognised is treated as a
log call rather than waved through — for a guard, the safe default is to ask.

The Telegram user id IS covered, but only on the account. #272 lists it, and
``telegram_backup.py`` did log ``me.id`` — yet banning ``.id`` outright would
also catch ``listener.py``'s failed-download ``message.id``, which is both a
different rule and legitimate debugging detail. So the scan first works out
which locals hold the result of ``get_me()`` and bans ``.id`` on those alone.
The same id is still stored as ``owner_id``, which is storage, not logging.

Chat ids, topic ids and titles are the separate documented rule — "never log
chat IDs, topic IDs, or topic titles" — enforced lower in this file by
``TestNoChatIdentifiersInLogs`` (#274). It was added after the account rule, once
its own backlog of pre-existing violations had been cleared.

One honest limit, shared by both scans: they see an identifier only when a
logging call reads it DIRECTLY — as a banned attribute, a chat/topic-ish
variable name, or a chat/topic object's ``id``/``name``/``title`` field. What
they CANNOT see is a value laundered through another call before logging:
``config.py``'s legitimate ``bool(config.phone)``, or ``migrate_media_paths``'s
``folder = str(chat_id)`` where the chat id becomes a generically-named string.
Telling those apart needs real taint analysis; the ``migrate_media_paths`` folder
logs were found and redacted by review, not by this scan. It catches the shapes
the leaks actually had, not every conceivable one.

The other blind spot is EXCEPTION TEXT. ``OSError`` stringifies as
``[Errno 66] Directory not empty: '/media/-1001234'`` — the path, and a media
path carries the chat-id folder — so ``logger.error(f"...: {e}")`` on a
filesystem operation re-leaks an id that the surrounding message no longer
names. Those sites log ``type(e).__name__`` instead. The scan cannot see this
either: ``{e}`` is opaque to it. Reviewers found these; when touching a log line
near a chat-derived path, check what the exception itself would print.

Requires Python 3.14: ``ast.parse`` here must read the repo's own sources, which
use PEP 758 unparenthesized ``except A, B:``. On an older interpreter this fails
with a raw SyntaxError rather than a meaningful assertion.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (REPO / "src", REPO / "scripts")

# Attributes that identify the account holder. Reading any of these into a log
# message publishes the operator's identity into a stream that is routinely
# shipped to aggregators and pasted into bug reports.
BANNED_ATTRIBUTES = frozenset({"first_name", "last_name", "phone", "username"})

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception", "log"})


def _is_logging_call(node: ast.Call) -> bool:
    """True for anything that puts text somewhere a human or aggregator reads.

    ``logger.info(...)``, ``self.logger.warning(...)``, ``logger.log(INFO, ...)``,
    ``logging.getLogger(__name__).info(...)`` and bare ``print(...)``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_METHODS:
        return False
    target = func.value
    if isinstance(target, ast.Name):
        return "log" in target.id.lower()
    if isinstance(target, ast.Attribute):
        return "log" in target.attr.lower()
    # Unrecognised receiver — a logger built inline by a call, an alias that does
    # not say "log", something else entirely. Treat it as a log call: for a guard
    # the cost of a false positive is a comment, and the cost of a false negative
    # is the leak this test exists to stop.
    return True


def _account_variable_names(tree: ast.AST) -> frozenset[str]:
    """Locals holding the result of ``get_me()``, however it was called.

    Covers ``me = await client.get_me()`` and the wrapped
    ``me = await call_with_flood_retry(self.client.get_me)`` alike, by looking
    for the name anywhere in the assigned expression.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(child, ast.Attribute) and child.attr == "get_me" for child in ast.walk(node.value)):
            continue
        names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return frozenset(names)


def _banned_attributes_in(node: ast.AST, account_names: frozenset[str] = frozenset()) -> list[str]:
    """Every banned attribute name read anywhere inside ``node``.

    ``.id`` is banned only on a variable known to hold the account, never
    generally: #272 lists the Telegram user id as protected, but a bare ban
    would also catch ``message.id``, which is a different rule and legitimate
    debugging detail.
    """
    found: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute):
            continue
        if child.attr in BANNED_ATTRIBUTES:
            found.append(child.attr)
        elif child.attr == "id" and _reads_the_account(child.value, account_names):
            found.append("id")
    return found


def _reads_the_account(value: ast.AST, account_names: frozenset[str]) -> bool:
    """Is this expression the account — by name, or fetched on the spot?

    A name covers ``me.id``. The inline form ``(await client.get_me()).id``
    never binds a local, so matching names alone would let it through.
    """
    if isinstance(value, ast.Name):
        return value.id in account_names
    return any(isinstance(child, ast.Attribute) and child.attr == "get_me" for child in ast.walk(value))


def _scan_source_tree() -> list[str]:
    violations: list[str] = []
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            account_names = _account_variable_names(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_logging_call(node):
                    continue
                for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                    for attribute in _banned_attributes_in(argument, account_names):
                        violations.append(f"{path.relative_to(REPO)}:{node.lineno} reads .{attribute}")
    return violations


class TestNoAccountPiiInLogs(unittest.TestCase):
    def test_no_logging_call_reads_an_identifying_attribute(self) -> None:
        violations = _scan_source_tree()
        self.assertEqual(
            [],
            violations,
            "Logging statements must not read the account holder's identity. "
            "If the point is to confirm WHICH account is in play, compare it to the "
            "configured value and log the boolean, as setup_auth.py does — the session "
            "path cannot answer that, it is a constant by default. Offending call sites:\n  " + "\n  ".join(violations),
        )

    def test_the_scan_actually_detects_a_violation(self) -> None:
        """Guard the guard: a scan that silently matches nothing proves nothing."""
        tree = ast.parse('logger.info(f"Connected as {me.first_name} ({me.phone})")')
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
        self.assertTrue(_is_logging_call(call))
        found = [attr for argument in call.args for attr in _banned_attributes_in(argument)]
        self.assertEqual(["first_name", "phone"], found)

    def test_the_scan_reaches_the_files_it_claims_to_cover(self) -> None:
        """A wrong root would make the scan vacuously green.

        ``restore_chat.py`` and ``auth_noninteractive.py`` are named explicitly:
        they lived outside the first draft's single root and were still leaking
        while it reported success.
        """
        scanned = {path.name for root in SCANNED_ROOTS for path in root.rglob("*.py")}
        for expected in (
            "config.py",
            "setup_auth.py",
            "listener.py",
            "telegram_backup.py",
            "connection.py",
            "restore_chat.py",
            "auth_noninteractive.py",
        ):
            self.assertIn(expected, scanned)

    def test_a_bare_print_is_scanned_too(self) -> None:
        """The scripts announce themselves with print, not logger."""
        tree = ast.parse('print(f"Authenticated as {me.first_name} (@{me.username})")')
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
        self.assertTrue(_is_logging_call(call))
        found = [attr for argument in call.args for attr in _banned_attributes_in(argument)]
        self.assertEqual(["first_name", "username"], found)

    def test_the_account_id_is_banned_but_a_message_id_is_not(self) -> None:
        """#272 lists the Telegram user id, but ``.id`` alone is too broad.

        ``telegram_backup.py`` logged ``me.id`` and still legitimately stores it
        as owner_id; ``listener.py`` logs ``message.id`` on a failed download,
        which is a different rule. The distinction is where the value came from.
        """
        source = (
            "me = await client.get_me()\n"
            'logger.info(f"Logged in as {me.id}")\n'
            'logger.warning(f"Failed to download media for message {message.id}: {e}")\n'
        )
        tree = ast.parse(source)
        account_names = _account_variable_names(tree)
        self.assertEqual({"me"}, set(account_names))

        found = [
            attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_logging_call(node)
            for argument in node.args
            for attr in _banned_attributes_in(argument, account_names)
        ]
        self.assertEqual(["id"], found)

    def test_an_inline_get_me_cannot_dodge_the_account_id_rule(self) -> None:
        """``(await client.get_me()).id`` never binds a local to match against."""
        tree = ast.parse('logger.info(f"Logged in as {(await client.get_me()).id}")')
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node))
        found = [attr for argument in call.args for attr in _banned_attributes_in(argument, frozenset())]
        self.assertEqual(["id"], found)

    def test_an_unrelated_inline_id_is_still_allowed(self) -> None:
        """Only the account is in scope; a message id fetched inline is not."""
        tree = ast.parse('logger.warning(f"Failed for {client.get_message(n).id}")')
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node))
        found = [attr for argument in call.args for attr in _banned_attributes_in(argument, frozenset())]
        self.assertEqual([], found)

    def test_the_wrapped_get_me_call_still_marks_the_account(self) -> None:
        """The repo also calls it through a retry wrapper."""
        tree = ast.parse("me = await call_with_flood_retry(self.client.get_me)\n")
        self.assertEqual({"me"}, set(_account_variable_names(tree)))

    def test_an_unrecognised_receiver_fails_closed(self) -> None:
        """A logger built inline must not slip past by being unfamiliar."""
        tree = ast.parse('logging.getLogger(__name__).info(f"{me.phone}")')
        call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "info"
        )
        self.assertTrue(_is_logging_call(call))

    def test_a_bare_boolean_check_is_not_a_violation(self) -> None:
        """The rule is about publishing the value, not naming the concept.

        ``config.py`` reports whether a phone number is configured, which is
        legitimate. It resolves that to a local before logging, so the logging
        statement itself never reads the attribute — which is what keeps this
        scan strict enough to be worth having.
        """
        tree = ast.parse('phone_configured = bool(config.phone)\nlogger.info(f"Phone configured: {phone_configured}")')
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node)]
        self.assertEqual(1, len(calls))
        self.assertEqual([], [a for arg in calls[0].args for a in _banned_attributes_in(arg)])


# ---------------------------------------------------------------------------
# The chat-id / topic-id / title rule (#274)
#
# CLAUDE.md: "Never log chat IDs, topic IDs, or topic titles." This survived
# unenforced even longer than the account-identity rule. A targeted grep found 6
# sites; matching only the literal name `chat_id` missed `source_chat_id`,
# `dest_chat_id` and the config collection dumps. So this matches any name /
# attribute / subscript-key whose tail is a chat-id-ish token, and — because
# logging HOW MANY chats is fine while logging WHICH is not — excludes anything
# sitting inside a `len(...)` call.
#
# A path allow-list carries the deliberate exceptions, where the identifier is
# the answer the operator asked for rather than incidental noise. Listing them by
# path keeps the exemption visible instead of an accident of the matcher.
# ---------------------------------------------------------------------------

_ID_SUBJECTS = ("chat", "topic")
_ID_FIELDS = frozenset({"id", "name", "title"})


def _is_identifier_name(name: str) -> bool:
    """A variable whose components pair a subject (chat/topic) with a field.

    Component-based, not a tail regex: catches ``chat_id``, ``chat_ids``,
    ``source_chat_id``, ``display_chat_ids`` and ``chat_id_str`` (a real-leak
    shape the first tail-anchored version missed), while sparing ``chat_idx`` —
    a loop counter whose second component is ``idx``, not ``id``.
    """
    parts = name.lower().split("_")
    norm = [p[:-1] if p.endswith("s") and p[:-1] in _ID_FIELDS else p for p in parts]
    return any(a in _ID_SUBJECTS and b in _ID_FIELDS for a, b in zip(norm, norm[1:], strict=False))


def _is_chat_object(value: ast.AST) -> bool:
    """A Name/Attribute whose own name mentions a chat or topic.

    This is what keeps ``chat.get("id")`` and ``chat["title"]`` in scope while
    leaving ``msg.get("id")`` (a message id) and ``token_record["id"]`` (a share
    token) out — the field name alone is ambiguous, the object is not.
    """
    obj = value.id if isinstance(value, ast.Name) else value.attr if isinstance(value, ast.Attribute) else None
    return bool(obj) and any(s in obj.lower() for s in _ID_SUBJECTS)


def _chat_object_field_reads(node: ast.AST) -> list[str]:
    """id / name / title pulled from a chat/topic OBJECT.

    ``chat.id``, ``chat["id"]``, ``chat.get("id")`` — the shapes the name scan
    cannot see because the identifier is a dict key or attribute, not a variable.
    A live ``chat.get("id")`` leak in the viewer got through the first version.
    """
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in _ID_FIELDS and _is_chat_object(child.value):
            found.append(f".{child.attr}")
        elif (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Constant)
            and child.slice.value in _ID_FIELDS
            and _is_chat_object(child.value)
        ):
            found.append(f"[{child.slice.value!r}]")
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and child.args[0].value in _ID_FIELDS
            and _is_chat_object(child.func.value)
        ):
            found.append(f".get({child.args[0].value!r})")
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value in _ID_FIELDS
            and _is_chat_object(child.args[0])
        ):
            found.append(f"getattr(..., {child.args[1].value!r})")
    return found


_FS_CALLS = frozenset(
    {
        "remove",
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
        "makedirs",
        "mkdir",
        "copy",
        "copy2",
        "move",
        "symlink",
        "link",
        "listdir",
        "getsize",
        "scandir",
        "download_media",
        "download_profile_photo",
    }
)


def _try_calls_a_filesystem_op(try_node: ast.Try) -> bool:
    """Does the try body do something whose OSError would name a path?

    Includes the Telethon download helpers: they take a ``file=`` destination,
    so an OSError from them carries the media path too.
    """
    for child in ast.walk(try_node):
        if isinstance(child, ast.Call):
            name = child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", None)
            if name in _FS_CALLS or name == "open":
                return True
    return False


def _interpolates_raw_exception(node: ast.AST, handler_name: str) -> bool:
    """True for ``{e}``; False for ``type(e).__name__`` and ``describe_exception(e)``."""
    wrapped: set[int] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "__name__"
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Name)
            and child.value.func.id == "type"
        ):
            wrapped.update(id(a) for a in ast.walk(child.value))
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "describe_exception":
            wrapped.update(id(a) for a in ast.walk(child))
    return any(isinstance(c, ast.Name) and c.id == handler_name and id(c) not in wrapped for c in ast.walk(node))


CHAT_ID_LOG_ALLOWLIST = frozenset(
    {
        "src/__main__.py",  # CLI gap-fill / import summaries, printed to the operator who ran the command
        "src/export_backup.py",  # the `list-chats` table — the chat id IS the requested output
        "scripts/restore_chat.py",  # interactive destructive tool; ids are the operator's own arguments
    }
)


def _chat_identifier_hits(node: ast.AST) -> list[str]:
    """Chat-id-ish names read in ``node``, excluding those inside ``len(...)``.

    ``len(chat_ids)`` is a count, which the rule explicitly permits; the raw
    collection or a single id is what must not be logged.
    """
    inside_len: set[int] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "len":
            inside_len.update(id(d) for d in ast.walk(child))

    found: list[str] = []
    for child in ast.walk(node):
        if id(child) in inside_len:
            continue
        name = None
        if isinstance(child, ast.Name):
            name = child.id
        elif isinstance(child, ast.Attribute):
            name = child.attr
        elif isinstance(child, ast.Subscript):
            key = child.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                name = key.value
        if name and _is_identifier_name(name):
            found.append(name)
    return found


def _scan_for_chat_identifiers() -> list[str]:
    violations: list[str] = []
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(REPO))
            if rel in CHAT_ID_LOG_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_logging_call(node):
                    continue
                for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                    for name in _chat_identifier_hits(argument):
                        violations.append(f"{rel}:{node.lineno} logs {name}")
                    for read in _chat_object_field_reads(argument):
                        violations.append(f"{rel}:{node.lineno} logs a chat/topic {read}")
    return violations


class TestNoChatIdentifiersInLogs(unittest.TestCase):
    def test_no_logging_call_reads_a_chat_identifier(self) -> None:
        violations = _scan_for_chat_identifiers()
        self.assertEqual(
            [],
            violations,
            "Logging must not read a chat id, topic id or title (CLAUDE.md). Log a count, "
            "or nothing. If the identifier is genuinely the operator-facing answer (a list "
            "command, an interactive destructive tool), add the file to CHAT_ID_LOG_ALLOWLIST "
            "with a reason. Offending call sites:\n  " + "\n  ".join(violations),
        )

    def test_a_bare_variable_named_source_chat_id_is_caught(self) -> None:
        """The literal-name scan missed these; the tail match is why they are covered now."""
        tree = ast.parse('logger.error(f"Chat {source_chat_id} not found")')
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertEqual(["source_chat_id"], [h for a in call.args for h in _chat_identifier_hits(a)])

    def test_a_count_of_chats_is_allowed(self) -> None:
        tree = ast.parse('logger.info(f"backing up {len(self.chat_ids)} chats")')
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertEqual([], [h for a in call.args for h in _chat_identifier_hits(a)])

    def test_a_loop_index_that_merely_contains_chat_id_is_not_matched(self) -> None:
        """`chat_idx` is a counter, not an id — the tail anchor must exclude it."""
        tree = ast.parse('logger.info(f"{chat_idx}/{total}")')
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertEqual([], [h for a in call.args for h in _chat_identifier_hits(a)])

    def test_a_subscript_key_is_detected(self) -> None:
        tree = ast.parse("logger.info(f\"{detail['chat_id']}\")")
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertEqual(["chat_id"], [h for a in call.args for h in _chat_identifier_hits(a)])

    def test_an_id_or_title_read_from_a_chat_object_is_detected(self) -> None:
        """A live ``chat.get('id')`` leak got through the name-only first version."""
        for src in (
            "logger.error(f\"avatar for {chat.get('id')}\")",
            "logger.info(f\"{chat['title']}\")",
            'logger.info(f"{topic.title}")',
        ):
            tree = ast.parse(src)
            call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
            self.assertTrue(
                any(_chat_object_field_reads(a) for a in call.args), f"missed a chat/topic field read in: {src}"
            )

    def test_a_getattr_read_of_a_chat_field_is_detected(self) -> None:
        """``getattr(chat, "id")`` is the .get() bypass in attribute form."""
        tree = ast.parse('logger.info("%s", getattr(chat, "id"))')
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertTrue(any(_chat_object_field_reads(a) for a in call.args))
        # ...but not on a non-chat object.
        tree = ast.parse('logger.info("%s", getattr(msg, "id"))')
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
        self.assertEqual([], [h for a in call.args for h in _chat_object_field_reads(a)])

    def test_a_message_or_token_id_is_not_treated_as_a_chat_id(self) -> None:
        """The object, not the field name, is what scopes this — ``id`` is ambiguous."""
        for src in (
            "logger.error(f\"{msg.get('id')}\")",
            'logger.warning("denying %s", token_record["id"])',
        ):
            tree = ast.parse(src)
            call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
            self.assertEqual([], [h for a in call.args for h in _chat_object_field_reads(a)], src)

    def test_a_chat_id_str_name_is_detected_but_chat_idx_is_not(self) -> None:
        """Component match, not tail regex: ``chat_id_str`` is an id, ``chat_idx`` a counter."""
        self.assertTrue(_is_identifier_name("chat_id_str"))
        self.assertTrue(_is_identifier_name("source_chat_id"))
        self.assertFalse(_is_identifier_name("chat_idx"))

    def test_no_filesystem_handler_logs_a_raw_exception(self) -> None:
        """Exception text is the third leak route, and it shipped in v7.33.3.

        ``OSError`` stringifies with the offending path, and a media path
        carries the chat-id folder — so ``logger.error(f"...: {e}")`` in a
        handler wrapping a filesystem call re-leaks an id the message itself no
        longer names. That is exactly how ``message_utils.py`` line 443 survived
        the #274 sweep while its sibling at 389 was fixed: a replace-all edit
        matched only one indentation.

        Handlers over filesystem operations must use ``describe_exception(e)``,
        which drops the message for ``OSError`` and keeps it for everything
        else, or ``type(e).__name__``.
        """
        violations = []
        for root in SCANNED_ROOTS:
            for path in sorted(root.rglob("*.py")):
                rel = str(path.relative_to(REPO))
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Try) or not _try_calls_a_filesystem_op(node):
                        continue
                    for handler in node.handlers:
                        if not handler.name:
                            continue
                        for call in ast.walk(handler):
                            if not isinstance(call, ast.Call) or not _is_logging_call(call):
                                continue
                            args = [*call.args, *(k.value for k in call.keywords)]
                            if any(_interpolates_raw_exception(a, handler.name) for a in args):
                                violations.append(f"{rel}:{call.lineno} logs raw '{handler.name}'")
        self.assertEqual(
            [],
            violations,
            "A handler wrapping a filesystem call must not interpolate the raw exception: "
            "OSError carries the path, and media paths carry the chat id. Use "
            "describe_exception(e) — it keeps the message for non-OSError, where the "
            "diagnostic value is. Offending sites:\n  " + "\n  ".join(violations),
        )

    def test_the_raw_exception_detector_discriminates(self) -> None:
        """Guard the guard: the safe wrappers must NOT read as violations."""
        raw = ast.parse('logger.error(f"x: {e}")')
        safe_type = ast.parse('logger.error(f"x: {type(e).__name__}")')
        safe_helper = ast.parse('logger.error(f"x: {describe_exception(e)}")')
        for tree, expected in ((raw, True), (safe_type, False), (safe_helper, False)):
            call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_logging_call(n))
            self.assertEqual(expected, any(_interpolates_raw_exception(a, "e") for a in call.args))

    def test_the_allowlisted_paths_all_exist(self) -> None:
        """A stale allow-list entry silently widens the exemption."""
        for rel in CHAT_ID_LOG_ALLOWLIST:
            self.assertTrue((REPO / rel).is_file(), f"allowlisted path missing: {rel}")


if __name__ == "__main__":
    unittest.main()
