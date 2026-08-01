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

Genuinely out of scope: chat ids, topic ids and titles. That is a separate
documented rule — "never log chat IDs, topic IDs, or topic titles" — which is
equally unenforced and has live violations today, so covering it here would turn
this guard red on pre-existing work unrelated to #272. Deliberate follow-up, not
a false-positive problem; anyone widening this scan should expect to fix those
sites in the same change.

One honest limit: the scan sees a banned attribute only when a logging call
reads it DIRECTLY. Hoisting it to a local first defeats it. That is the same
manoeuvre ``config.py`` uses legitimately to log ``bool(config.phone)``, so the
two cannot be told apart without real taint analysis. This catches the shape the
nine original leaks actually had, not every conceivable one.

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
        elif child.attr == "id" and isinstance(child.value, ast.Name) and child.value.id in account_names:
            found.append("id")
    return found


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


if __name__ == "__main__":
    unittest.main()
