"""Reactions taken back, in the bubble's reactions row.

After the live chips, one quiet chip (no fill, a dashed edge, the time colour)
holds an undo arrow and how many reactions were taken back. It is a button:
it opens the list, each emoji with the count it had and when the archive
noticed it gone ("1 · 10:28", "Sep 2, 14:00" on a later day), and closes it
again. A live reaction frame moves an emoji that left the live set into the
list, and one that came back out of it, as the server does.

The helpers are EXECUTED under node, lifted verbatim from the template with the
real vendored moment, so a broken rule fails here and not only in a browser.
"""

import json
import unittest

from test_deleted_messages_frontend import _MOMENT
from test_frontend_bootstrap import INDEX_HTML, NODE, _run_setup_program

HTML = INDEX_HTML.read_text(encoding="utf-8")

_PRELUDE = (
    _MOMENT
    + """
const viewerTimezone = { value: 'UTC' }
const selectedChat = { value: { id: 7, ref: 'r7' } }
const openRemovedReactions = { value: {} }
"""
)

_DECLARATIONS = (
    "const formatCount = (n) =>",
    "const formatStamp = (iso, withZone = false) =>",
    "const noticedStamp = (sentIso, noticedIso) =>",
    "const removedReactions = (msg) =>",
    "const hasReactionRow = (msg) =>",
    "const removedReactionCount = (msg) =>",
    "const removedReactionsLabel = (msg) =>",
    "const removedReactionsKey = (msg) =>",
    "const isRemovedReactionsOpen = (msg) =>",
    "const toggleRemovedReactions = (msg) =>",
    "const removedReactionText = (msg, removed) =>",
    "const removedReactionTitle = (removed) =>",
    "const applyLiveReactions = (msg, reactions) =>",
    "const formatReactionEmoji = (emoji) =>",
)

_SENT = "2026-09-01T10:20:00"
_MSG = {
    "id": 5,
    "chat_id": 7,
    "date": _SENT,
    "reactions": [{"emoji": "❤️", "count": 5, "user_ids": []}],
    "removed_reactions": [
        {"emoji": "😮", "count": 1, "removed_at": "2026-09-01T10:28:00"},
        {"emoji": "👍", "count": 2, "removed_at": "2026-08-31T09:00:00"},
    ],
}


def _run(expression: str) -> object:
    return _run_setup_program(HTML, _DECLARATIONS, _PRELUDE, f"console.log(JSON.stringify({expression}))")


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestTheChipAndItsList(unittest.TestCase):
    def test_the_row_shows_for_live_or_removed_reactions(self) -> None:
        rows = [
            _MSG,
            {"id": 1, "reactions": [], "removed_reactions": [{"emoji": "👍", "count": 1, "removed_at": _SENT}]},
            {"id": 2, "reactions": [{"emoji": "👍", "count": 1}]},
            {"id": 3, "reactions": [], "removed_reactions": []},
            {"id": 4},
        ]
        self.assertEqual(_run(f"{json.dumps(rows)}.map(hasReactionRow)"), [True, True, True, False, False])

    def test_the_toggle_counts_every_reaction_taken_back(self) -> None:
        one = {"id": 1, "removed_reactions": [{"emoji": "👍", "count": 1}]}
        self.assertEqual(
            _run(f"[removedReactionsLabel({json.dumps(_MSG)}), removedReactionsLabel({json.dumps(one)})]"),
            ["3 reactions taken back", "1 reaction taken back"],
        )

    def test_each_chip_gives_the_count_and_when_the_archive_noticed(self) -> None:
        out = _run(
            f"(() => {{ const msg = {json.dumps(_MSG)}; return {{"
            " texts: msg.removed_reactions.map(r => removedReactionText(msg, r)),"
            " titles: msg.removed_reactions.map(removedReactionTitle),"
            " madrid: (viewerTimezone.value = 'Europe/Madrid', removedReactionText(msg, msg.removed_reactions[0])),"
            " bare: removedReactionText(msg, { emoji: '🎉', count: 4 }) } })()"
        )
        # Same day as the message: the time; another day: the date too.
        self.assertEqual(out["texts"], ["1 · 10:28", "2 · Aug 31, 09:00"])
        self.assertEqual(
            out["titles"],
            [
                "😮 1, taken back. The archive noticed on September 1, 2026 at 10:28.",
                "👍 2, taken back. The archive noticed on August 31, 2026 at 09:00.",
            ],
        )
        self.assertEqual(out["madrid"], "1 · 12:28")
        self.assertEqual(out["bare"], "4")

    def test_the_toggle_opens_and_closes_one_message(self) -> None:
        out = _run(
            f"(() => {{ const msg = {json.dumps(_MSG)}; const other = {{ id: 6, chat_id: 7 }}; const seen = []"
            "; seen.push(isRemovedReactionsOpen(msg)); toggleRemovedReactions(msg)"
            "; seen.push(isRemovedReactionsOpen(msg), isRemovedReactionsOpen(other)); toggleRemovedReactions(msg)"
            "; seen.push(isRemovedReactionsOpen(msg)); return seen })()"
        )
        self.assertEqual(out, [False, True, False, False])


@unittest.skipUnless(NODE, "node is required to execute the helpers")
class TestALiveFrame(unittest.TestCase):
    def _apply(self, msg: dict, reactions: object) -> dict:
        return _run(
            f"(() => {{ const msg = {json.dumps(msg)}; applyLiveReactions(msg, {json.dumps(reactions)})"
            "; return { live: msg.reactions.map(r => [r.emoji, r.count]),"
            " removed: msg.removed_reactions.map(r => [r.emoji, r.count, typeof r.removed_at]) } })()"
        )

    def test_an_emoji_that_leaves_the_live_set_joins_the_list_first(self) -> None:
        out = self._apply(_MSG, [])
        self.assertEqual(out["live"], [])
        self.assertEqual(out["removed"], [["❤️", 5, "string"], ["😮", 1, "string"], ["👍", 2, "string"]])

    def test_an_emoji_that_comes_back_leaves_the_list(self) -> None:
        out = self._apply(_MSG, [{"emoji": "❤️", "count": 5}, {"emoji": "😮", "count": 3}])
        self.assertEqual(out["live"], [["❤️", 5], ["😮", 3]])
        self.assertEqual(out["removed"], [["👍", 2, "string"]])

    def test_a_message_without_a_list_gets_one(self) -> None:
        out = self._apply({"id": 1, "reactions": [{"emoji": "👍", "count": 1}]}, None)
        self.assertEqual(out, {"live": [], "removed": [["👍", 1, "string"]]})


class TestTheTemplate(unittest.TestCase):
    def _row_markup(self) -> str:
        start = HTML.index('<span v-if="hasReactionRow(msg)" class="message-reactions">')
        return HTML[start : HTML.index("<!-- Metadata -->", start)]

    def test_the_toggle_is_a_button_reachable_by_keyboard_and_touch(self) -> None:
        row = self._row_markup()
        toggle = row[row.index("<button") : row.index("</button>")]
        self.assertIn('type="button"', toggle)
        self.assertIn('@click.stop="toggleRemovedReactions(msg)"', toggle)
        self.assertIn(":aria-expanded=", toggle)
        self.assertIn(':aria-label="removedReactionsLabel(msg)"', toggle)
        # 40px hit area on a touch screen, a focus ring on the keyboard.
        self.assertIn("hit-40", toggle)
        self.assertIn("focus-visible:ring-2", toggle)

    def test_the_list_follows_the_toggle_and_speaks_each_chip(self) -> None:
        row = self._row_markup()
        self.assertLess(row.index("reaction-removed-toggle"), row.index("reaction-removed-list"))
        self.assertIn('v-for="removed in removedReactions(msg)"', row)
        self.assertIn('<span class="sr-only">{{ removedReactionTitle(removed) }}</span>', row)

    def test_every_layout_rule_that_asked_for_reactions_asks_for_the_row(self) -> None:
        # A bubble whose only reactions were taken back still draws the row, so
        # it is never a picture-only or a compact audio bubble.
        self.assertNotIn("msg.reactions?.length", HTML)
        self.assertNotIn("msg.reactions && msg.reactions.length", HTML)
        self.assertGreaterEqual(HTML.count("hasReactionRow(msg)"), 4)

    def test_a_live_frame_goes_through_the_same_rule(self) -> None:
        start = HTML.index("case 'reaction':")
        handler = HTML[start : HTML.index("break", HTML.index("messages.value.find", start))]
        self.assertIn("applyLiveReactions(reactionMsg, data.reactions)", handler)

    def test_the_chips_are_quieter_than_the_live_ones(self) -> None:
        start = HTML.index(".message-bubble .reaction-chip.reaction-removed-toggle,")
        rule = HTML[start : HTML.index("}", start)]
        self.assertIn("background: none;", rule)
        self.assertIn("color: rgb(var(--tg-meta));", rule)
        self.assertIn("dashed", rule)

    def test_hover_never_fills_the_toggle(self) -> None:
        # The chip's text is the time colour, which test_a_deleted_bubble_stays_readable
        # checks on the plain and the washed bubble. A hover fill in that colour
        # would sit under the text and drop it below 4.5:1 on the wash, so no
        # rule for these chips may paint a background other than none.
        start = HTML.index(".message-bubble .reaction-chip.reaction-removed-toggle,")
        css = HTML[start : HTML.index(".reaction-removed .reaction-emoji", start)]
        backgrounds = [line.strip() for line in css.splitlines() if "background" in line]
        self.assertEqual(backgrounds, ["background: none;"])
        hover = css[css.index("reaction-removed-toggle:hover") :]
        self.assertIn("border-color: rgb(var(--tg-meta));", hover[: hover.index("}")])


if __name__ == "__main__":
    unittest.main()
