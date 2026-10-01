"""A poll's and a link preview's newest state in the bubble (message_snapshots, 037).

The bubble draws the newest state the archive kept over the first capture in
``raw_data``: the poll's results, its total and "Final results" once closed,
and the preview card's fields. When that state differs from the first capture
a quiet "updated 10:12" follows the poll's kind line or closes the card, with
a tooltip saying when the archive saw it.

The helpers are EXECUTED under node, lifted verbatim from the template with the
real vendored moment, so a broken rule fails here and not only in a browser.
"""

import json
import re
import unittest

from test_deleted_messages_frontend import _MOMENT
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

HTML = INDEX_HTML.read_text(encoding="utf-8")

_PRELUDE = _MOMENT + "\nconst viewerTimezone = { value: 'UTC' }\n"

_DECLARATIONS = (
    "const formatStamp = (iso, withZone = false) =>",
    "const noticedStamp = (sentIso, noticedIso) =>",
    "const pollKindLabel = (poll) =>",
    "const currentPoll = (msg) =>",
    "const currentPreview = (msg) =>",
    "const snapshotNote = (msg, kind) =>",
    "const snapshotTitle = (msg, kind) =>",
    "const pollVotesText = (msg) =>",
    "const getPollPercentage = (msg, answer) =>",
)

_FIRST_POLL = {
    "question": "Demo question?",
    "answers": [{"text": "Answer A", "option": "AA=="}, {"text": "Answer B", "option": "AQ=="}],
    "closed": False,
    "public_voters": False,
    "quiz": False,
    "results": {"total_voters": 4, "results": [{"option": "AA==", "voters": 3}, {"option": "AQ==", "voters": 1}]},
}
_NEWEST_POLL = {
    **_FIRST_POLL,
    "closed": True,
    "results": {"total_voters": 10, "results": [{"option": "AA==", "voters": 2}, {"option": "AQ==", "voters": 8}]},
}
_MSG = {
    "id": 5,
    "date": "2026-03-01T09:00:00",
    "raw_data": {"poll": _FIRST_POLL, "webpage": {"url": "https://example.com", "title": "Old title"}},
    "snapshots": {
        "poll": {
            "payload": _NEWEST_POLL,
            "observed_at": "2026-03-01T10:12:00",
            "source": "listener",
            "count": 2,
            "differs_from_first": True,
        },
        "preview": {
            "payload": {"url": "https://example.com", "title": "New title"},
            "observed_at": "2026-03-02T08:05:00",
            "source": "sync",
            "count": 1,
            "differs_from_first": True,
        },
    },
}


def _run(expression: str) -> object:
    return _run_setup_program(HTML, _DECLARATIONS, _PRELUDE, f"console.log(JSON.stringify({expression}))")


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheNewestState(unittest.TestCase):
    def test_the_poll_shows_its_newest_results_and_reads_final_once_closed(self) -> None:
        msg = json.dumps(_MSG)
        result = _run(
            f"(() => {{ const m = {msg}; const p = currentPoll(m); return [pollKindLabel(p), pollVotesText(m),"
            f" p.answers.map(a => getPollPercentage(m, a))] }})()"
        )
        self.assertEqual(result, ["Final results", "10 votes", [20, 80]])

    def test_without_snapshots_the_first_capture_is_drawn_as_before(self) -> None:
        msg = json.dumps({**_MSG, "snapshots": {}})
        result = _run(
            f"(() => {{ const m = {msg}; const p = currentPoll(m); return [pollKindLabel(p), pollVotesText(m),"
            f" p.answers.map(a => getPollPercentage(m, a)), currentPreview(m).title, snapshotNote(m, 'poll')] }})()"
        )
        self.assertEqual(result, ["Anonymous poll", "4 votes", [75, 25], "Old title", ""])

    def test_a_state_holding_the_results_alone_keeps_the_question_and_answers(self) -> None:
        only_results = {"results": {"total_voters": 1, "results": [{"option": "AQ==", "voters": 1}]}}
        msg = json.dumps({**_MSG, "snapshots": {"poll": {**_MSG["snapshots"]["poll"], "payload": only_results}}})
        result = _run(
            f"(() => {{ const m = {msg}; const p = currentPoll(m); return [p.question, p.answers.length,"
            f" pollVotesText(m), pollKindLabel(p)] }})()"
        )
        self.assertEqual(result, ["Demo question?", 2, "1 vote", "Anonymous poll"])

    def test_the_preview_card_shows_the_newest_fields(self) -> None:
        self.assertEqual(_run(f"currentPreview({json.dumps(_MSG)}).title"), "New title")
        no_first = {**_MSG, "raw_data": {}}
        self.assertEqual(_run(f"currentPreview({json.dumps(no_first)}).title"), "New title")
        self.assertIsNone(_run("currentPreview({raw_data: {}, snapshots: {}})"))


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheQuietNote(unittest.TestCase):
    def test_the_note_names_when_the_archive_saw_the_state(self) -> None:
        msg = json.dumps(_MSG)
        self.assertEqual(
            _run(f"[snapshotNote({msg}, 'poll'), snapshotNote({msg}, 'preview')]"),
            ["updated 10:12", "updated Mar 2, 08:05"],
        )

    def test_no_note_when_the_newest_state_matches_the_first_capture(self) -> None:
        same = {**_MSG["snapshots"]["poll"], "differs_from_first": False}
        msg = json.dumps({**_MSG, "snapshots": {"poll": same}})
        self.assertEqual(_run(f"[snapshotNote({msg}, 'poll'), snapshotTitle({msg}, 'poll')]"), ["", ""])

    def test_the_tooltip_gives_the_full_time_and_keeps_the_first_capture(self) -> None:
        self.assertEqual(
            _run(f"snapshotTitle({json.dumps(_MSG)}, 'poll')"),
            "These results as the archive saw it on March 1, 2026 at 10:12 (UTC). The first capture is kept and differs.",
        )


class TestTheTemplate(unittest.TestCase):
    def _block(self, start: str, end: str) -> str:
        begin = HTML.index(start)
        return HTML[begin : HTML.index(end, begin)]

    def test_the_bubble_reads_the_newest_state_and_never_raw_data_directly(self) -> None:
        poll = self._block('<div v-if="currentPoll(msg)" class="poll', "</ul>")
        preview = self._block('<div v-if="currentPreview(msg)" class="link-preview', "<img")
        for block in (poll, preview):
            self.assertNotIn("raw_data", block)
            self.assertIn("snapshotNote(msg,", block)
            self.assertNotIn("v-html", block)
        self.assertNotRegex(HTML, re.compile(r"msg\.raw_data\??\.(poll|webpage)"))

    def test_the_note_uses_the_time_colour_token(self) -> None:
        preview = self._block('<div v-if="currentPreview(msg)" class="link-preview', "<img")
        self.assertIn('class="snapshot-note text-tg-meta"', preview)
        poll = self._block('<div v-if="currentPoll(msg)" class="poll', "</ul>")
        self.assertIn('<div class="poll-kind text-tg-meta">', poll)
