"""Tests for group sender avatars (#229) and the migration banner (#228).

Covers three slices:

* US-210 (slice 1): the pure-frontend initials circle — getSenderInitials /
  getAvatarFill exist in the template, and every palette's seven avatar fills
  and peer name colours clear WCAG AA contrast.
* US-211 (slice 2a): the media-ACL fix that serves member avatars for users
  who spoke in a visible chat, plus per-message sender_avatar_url resolution
  from files already on disk.
* US-203 (#228): the display-only group→supergroup migration banner.

The template assertions follow the string-matching idiom of
test_frontend_bootstrap.py; the backend assertions follow the temp-media-dir
idiom of test_database_viewer.py (TestAvatarPathLookup).
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("BACKUP_PATH", tempfile.mkdtemp(prefix="ta_test_backup_"))

from telegram_archive.db.adapter import DatabaseAdapter  # noqa: E402
from telegram_archive.db.base import DatabaseManager  # noqa: E402
from telegram_archive.db.models import Base, Message  # noqa: E402
from telegram_archive.web import main as web_main  # noqa: E402

try:
    from httpx import ASGITransport, AsyncClient

    _HTTPX_AVAILABLE = True
except Exception:
    _HTTPX_AVAILABLE = False

INDEX_HTML = Path(__file__).resolve().parents[1] / "telegram_archive" / "web" / "templates" / "index.html"


def _luminance(rgb: tuple[int, int, int]) -> float:
    def _channel(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 2 contrast ratio of two opaque sRGB colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _triplet(value: str) -> tuple[int, int, int]:
    r, g, b = (int(part) for part in value.split())
    return (r, g, b)


def _hex(value: str) -> tuple[int, int, int]:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def _palettes(html: str) -> dict[str, dict[str, str]]:
    """Every palette's --tg-* declarations, one data-theme block each."""
    blocks = {}
    for match in re.finditer(r':root\[data-theme="([a-z]+)"\]\s*\{([^}]*)\}', html):
        blocks[match.group(1)] = match.group(2)
    return {name: dict(re.findall(r"(--tg-[a-z0-9-]+):\s*([^;]+);", body)) for name, body in blocks.items()}


class TestSenderInitialsTemplate(unittest.TestCase):
    """US-210: initials helper contract, expressed via the template source."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_initials_and_fill_helpers_exist_and_are_exported(self):
        self.assertIn("const getSenderInitials = (msg) =>", self.html)
        self.assertIn("const getAvatarFill = (msg) =>", self.html)
        # Must be returned from setup() or Vue cannot resolve them in-template.
        self.assertIn("getSenderInitials,", self.html)
        self.assertIn("getAvatarFill,", self.html)

    def test_initials_mirror_sender_name_sources(self):
        """The monogram must derive from getSenderName, not a private name chain,
        so it matches the visible label; only '?' for the Deleted Account terminal."""
        start = self.html.index("const getSenderInitials = (msg) =>")
        body = self.html[start : start + 700]
        # Derives from the SAME source as the visible name label.
        self.assertIn("getSenderName(msg)", body)
        # '?' only when getSenderName itself has nothing real.
        self.assertIn("name === 'Deleted Account'", body)
        self.assertIn("return '?'", body)
        # Must NOT reintroduce the getChatName 'DA' fallback.
        self.assertNotIn("getChatName", body)

    def test_avatar_fill_and_name_colour_are_theme_tokens(self):
        # The index picks one of seven per-theme tokens; no hue is computed in JS.
        start = self.html.index("const getAvatarFill = (msg) =>")
        body = self.html[start : start + 300]
        self.assertIn("`var(--tg-avatar-${getSenderColor(msg)})`", body)
        start = self.html.index("const getSenderNameColor = (msg) =>")
        body = self.html[start : start + 300]
        self.assertIn("`rgb(var(--tg-peer-${getSenderColor(msg)}))`", body)
        self.assertNotIn("hsl(", self.html[self.html.index("const getPeerIndex") : start])

    @unittest.skipUnless(shutil.which("node"), "node is required to execute the helper")
    def test_peer_index_is_the_bare_telegram_id_modulo_seven(self):
        start = self.html.index("const getPeerIndex = (id) => {")
        end = self.html.index("\n                }\n", start) + len("\n                }\n")
        script = (
            self.html[start:end]
            + """
const assert = require('node:assert/strict');
assert.equal(getPeerIndex(7), 0);
assert.equal(getPeerIndex(15), 1);
// A basic group -<id> and a channel -100<id> take the colour of the bare id.
assert.equal(getPeerIndex(-15), 1);
assert.equal(getPeerIndex(-1001234567890), 1234567890 % 7);
assert.equal(getPeerIndex('4000000008'), 4000000008 % 7);
for (const bad of [null, undefined, 'x', NaN]) assert.equal(getPeerIndex(bad), 0);
"""
        )
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestSenderInitialsLogic(unittest.TestCase):
    """US-210: replicate getSenderInitials semantics to lock the contract.

    getSenderInitials mirrors getSenderName's source chain (post_author →
    first/last → username → 'User <id>') and only yields '?' for the terminal
    'Deleted Account' — so the monogram always matches the visible name label.
    """

    @staticmethod
    def _sender_name(msg):
        """Mirror of the JS getSenderName resolution chain."""
        raw = msg.get("raw_data") or {}
        if raw.get("post_author"):
            return raw["post_author"]
        first, last = msg.get("first_name"), msg.get("last_name")
        if first or last:
            return f"{first or ''} {last or ''}".strip()
        if msg.get("username"):
            return msg["username"]
        if msg.get("sender_id"):
            return f"User {msg['sender_id']}"
        return "Deleted Account"

    def _initials(self, msg):
        """Mirror of the JS getSenderInitials."""
        name = self._sender_name(msg)
        if not name or name == "Deleted Account":
            return "?"
        return "".join(w[0] for w in name.split() if w)[:2].upper() or "?"

    def test_two_names_two_letters(self):
        self.assertEqual(self._initials({"first_name": "Ada", "last_name": "Lovelace"}), "AL")

    def test_one_name_one_letter(self):
        self.assertEqual(self._initials({"first_name": "Grace"}), "G")
        self.assertEqual(self._initials({"first_name": "grace", "last_name": ""}), "G")

    def test_username_fallback_matches_label(self):
        # No first/last but a username → monogram from the username (visible label).
        self.assertEqual(self._initials({"username": "grace"}), "G")

    def test_post_author_signature(self):
        # Channel post signature is the visible label → monogram from it.
        self.assertEqual(self._initials({"raw_data": {"post_author": "John Doe"}}), "JD")

    def test_sender_id_fallback_is_not_question_mark(self):
        # getSenderName renders 'User <id>' → a real label, so NOT '?'.
        self.assertEqual(self._initials({"sender_id": 12345}), "U1")

    def test_empty_returns_question_mark(self):
        # Only when getSenderName would have nothing real.
        self.assertEqual(self._initials({}), "?")
        self.assertEqual(self._initials({"first_name": "", "last_name": ""}), "?")


def _tint(value: str) -> tuple[tuple[int, int, int], float]:
    """An `rgb(r g b / a)` or `rgba(r, g, b, a)` token as a colour and its alpha."""
    match = re.fullmatch(r"rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)\s*[/,]\s*([\d.]+)\s*\)", value.strip())
    assert match is not None, value
    r, g, b, a = match.groups()
    return (int(r), int(g), int(b)), float(a)


def _over(colour: tuple[int, int, int], alpha: float, under: tuple[int, int, int]) -> tuple[int, int, int]:
    """colour at alpha composited over an opaque colour."""
    return tuple(round(c * alpha + u * (1 - alpha)) for c, u in zip(colour, under, strict=True))


class TestPeerColourContrast(unittest.TestCase):
    """Every palette's text pairs stay readable (WCAG AA, 4.5:1) where they are drawn.

    Bubbles are opaque in every palette, so they are measured on their fill, with
    the tints drawn on them composited on top. Text drawn over the pane sits on
    the service pill, which is measured over the pane colour and over every
    stop of the palette's wallpaper gradient.
    """

    @classmethod
    def setUpClass(cls):
        html = INDEX_HTML.read_text(encoding="utf-8")
        cls.palettes = _palettes(html)

    def _bubble(self, tokens: dict[str, str], side: str) -> tuple[int, int, int]:
        return _triplet(tokens["--tg-other" if side == "in" else "--tg-own"])

    def _fills(self, tokens: dict[str, str], side: str) -> list[tuple[int, int, int]]:
        """Every colour the side's bubble paints: its flat fill and, for a gradient
        outgoing bubble (iOS Night), each stop of the gradient laid over it."""
        fills = [self._bubble(tokens, side)]
        if side == "out":
            fills += [_hex(stop) for stop in re.findall(r"#[0-9A-Fa-f]{6}", tokens["--tg-own-image"])]
        return fills

    def _tinted(self, tokens: dict[str, str], token: str, under: tuple[int, int, int]) -> tuple[int, int, int]:
        colour, alpha = _tint(tokens[token])
        return _over(colour, alpha, under)

    def _check(self, theme: str, label: str, fg: tuple[int, int, int], bg: tuple[int, int, int], minimum: float = 4.5):
        with self.subTest(theme=theme, pair=label):
            self.assertGreaterEqual(round(_contrast(fg, bg), 2), minimum, f"{theme}: {label}")

    def test_every_palette_is_checked(self):
        self.assertIn("telegram", self.palettes)
        self.assertIn("slate", self.palettes)
        self.assertGreaterEqual(len(self.palettes), 11)

    def test_initials_clear_both_gradient_stops(self):
        # The initials sit in the centre of the circle, and a top-to-bottom gradient
        # spans the whole disc, so both stops and the midpoint carry the letters.
        for name, tokens in self.palettes.items():
            fg = _triplet(tokens["--tg-avatar-fg"])
            for index in range(7):
                stops = [_hex(stop) for stop in re.findall(r"#[0-9A-Fa-f]{6}", tokens[f"--tg-avatar-{index}"])]
                self.assertEqual(len(stops), 2, f"{name} avatar-{index}")
                middle = tuple(round((a + b) / 2) for a, b in zip(*stops, strict=True))
                for label, stop in (("first stop", stops[0]), ("second stop", stops[1]), ("midpoint", middle)):
                    self._check(name, f"avatar-{index} {label}", fg, stop)

    def test_the_avatar_check_can_fail(self):
        # Positive control: a bright Telegram stop under white initials fails.
        self.assertLess(_contrast((255, 255, 255), _hex("#6DBE4E")), 4.5)

    def test_peer_colours_are_told_apart(self):
        # Red (0) and pink (6) are the closest pair on the wheel; at the same
        # lightness they must still sit 30 degrees or more apart.
        import colorsys

        for name, tokens in self.palettes.items():
            if len({tokens[f"--tg-peer-{index}"] for index in range(7)}) == 1:
                continue  # one ink for every name, by design (Minimal, Graphite)
            hues = []
            for index in (0, 6):
                r, g, b = _triplet(tokens[f"--tg-peer-{index}"])
                hues.append(colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0] * 360)
            gap = abs(hues[0] - hues[1])
            with self.subTest(theme=name):
                self.assertGreaterEqual(min(gap, 360 - gap), 30, name)

    def test_names_read_on_the_incoming_bubble(self):
        for name, tokens in self.palettes.items():
            bubble = self._bubble(tokens, "in")
            for index in range(7):
                self._check(name, f"peer-{index}", _triplet(tokens[f"--tg-peer-{index}"]), bubble)

    def test_bubble_text_pairs_read_on_both_sides(self):
        for name, tokens in self.palettes.items():
            pairs = [(each, fill) for each in ("in", "out") for fill in self._fills(tokens, each)]
            for side, bubble in pairs:
                quote_bg = self._tinted(tokens, f"--tg-quote-bg-{side}", bubble)
                quote = _triplet(tokens[f"--tg-quote-{side}"])
                meta = _triplet(tokens[f"--tg-meta-{side}"])
                self._check(name, f"text-{side} on bubble", _triplet(tokens[f"--tg-text-{side}"]), bubble)
                # Links, mentions and the reply label.
                self._check(name, f"quote-{side} on bubble", quote, bubble)
                self._check(name, f"quote-{side} on quote-bg", quote, quote_bg)
                self._check(name, f"forward-{side} on quote-bg", _triplet(tokens[f"--tg-forward-{side}"]), quote_bg)
                # The forward header is two plain lines on the bubble itself.
                self._check(name, f"forward-{side} on bubble", _triplet(tokens[f"--tg-forward-{side}"]), bubble)
                # The time, the deleted text and the archive status lines
                # (not downloaded, no speech detected), on the bubble and in a quote box.
                self._check(name, f"meta-{side} on bubble", meta, bubble)
                self._check(name, f"meta-{side} on quote-bg", meta, quote_bg)
                preview_bg = self._tinted(tokens, f"--tg-preview-bg-{side}", bubble)
                self._check(name, f"quote-{side} on preview-bg", quote, preview_bg)
                # The reply snippet and a transcript are the side's text on its quote box.
                self._check(name, f"text-{side} on quote-bg", _triplet(tokens[f"--tg-text-{side}"]), quote_bg)
                # The play disc is the quote colour with the bubble's fill as its glyph,
                # and the focus ring inside a bubble is the quote colour: both are the
                # quote-on-bubble pair above, so the disc and the ring clear 4.5:1 too.
                # The account chip inside a bubble is an outline in the quote colour.
                self._check(name, f"chip-{side}", quote, bubble)
                # The "deleted" marker in the meta row.
                self._check(name, f"danger-fg-{side} on bubble", _triplet(tokens["--tg-danger-fg"]), bubble)

    def test_a_deleted_bubble_stays_readable(self):
        """The deleted wash keeps every text pair drawn straight on the bubble at
        4.5:1: the text, the time, links and the poll's figures, and the
        "deleted" mark in the side's calm deleted colour. The tinted boxes inside a deleted bubble are drawn over
        the plain fill (see test_deleted_boxes_skip_the_wash), so their pairs are
        the ones test_bubble_text_pairs_read_on_both_sides measures."""
        for name, tokens in self.palettes.items():
            wash, wash_alpha = _tint(tokens["--tg-deleted-wash"])
            for side in ("in", "out"):
                for fill in self._fills(tokens, side):
                    washed = _over(wash, wash_alpha, fill)
                    for token in (
                        f"--tg-text-{side}",
                        f"--tg-meta-{side}",
                        f"--tg-quote-{side}",
                        f"--tg-deleted-fg-{side}",
                    ):
                        self._check(name, f"{token} on the deleted {side} bubble", _triplet(tokens[token]), washed)
                    # The reaction count is the side's text colour on the reaction tint.
                    reaction = self._tinted(tokens, f"--tg-reaction-bg-{side}", fill)
                    self._check(
                        name, f"text-{side} on the reaction tint", _triplet(tokens[f"--tg-text-{side}"]), reaction
                    )

    def test_the_folded_line_and_the_header_read_on_the_wash(self):
        """A folded deleted message is a line in the bubble's fill under the same
        wash: its sender name in the peer colour, the trash and "Deleted photo" in
        the side's deleted colour, the time and the dots in the time colour, and
        "Show" in the link colour. Opened, the header ("Deleted · 21:59", Hide,
        Show in chat) uses the same pairs on the washed bubble."""
        for name, tokens in self.palettes.items():
            wash, wash_alpha = _tint(tokens["--tg-deleted-wash"])
            for side in ("in", "out"):
                for fill in self._fills(tokens, side):
                    washed = _over(wash, wash_alpha, fill)
                    pairs = [
                        ("deleted label", f"--tg-deleted-fg-{side}"),
                        ("time and dots", f"--tg-meta-{side}"),
                        ("Show, Hide and Show in chat", f"--tg-quote-{side}"),
                    ]
                    if side == "in":
                        pairs += [(f"name peer-{index}", f"--tg-peer-{index}") for index in range(7)]
                    for label, token in pairs:
                        self._check(name, f"{label} on the washed {side} line", _triplet(tokens[token]), washed)

    def test_the_deleted_only_controls_read(self):
        """The chat menu's count, and the "Deleted only" line under the header: the
        pressed chip in on-accent on the accent (and its hover), the count muted."""
        for name, tokens in self.palettes.items():
            muted = _triplet(tokens["--tg-muted"])
            sidebar = _triplet(tokens["--tg-sidebar"])
            hovered = _over(_triplet(tokens["--tg-accent"]), 0.08, sidebar)
            self._check(name, "menu count on the menu", muted, sidebar)
            self._check(name, "menu count on a hovered item", muted, hovered)
            on_accent = _triplet(tokens["--tg-on-accent"])
            self._check(name, "chip on the accent", on_accent, _triplet(tokens["--tg-accent-strong"]))
            self._check(name, "chip on the hovered accent", on_accent, _triplet(tokens["--tg-accent-hover"]))
            self._check(name, "count on the header", muted, _triplet(tokens["--tg-header"]))
            # The chip's focus ring sits outside it, on the header.
            self._check(
                name, "chip focus on the header", _triplet(tokens["--tg-focus"]), _triplet(tokens["--tg-header"]), 3.0
            )

    def test_the_brick_wash_at_the_new_strength_would_break_the_time(self):
        # Positive control: the first wash colour, brick red, raised to 11% puts
        # Telegram Day's time under 4.5:1; the brighter red at 11% keeps it.
        tokens = self.palettes["telegram"]
        fill = _triplet(tokens["--tg-other"])
        meta = _triplet(tokens["--tg-meta-in"])
        self.assertLess(_contrast(meta, _over((163, 58, 47), 0.11, fill)), 4.5)
        wash, wash_alpha = _tint(tokens["--tg-deleted-wash"])
        self.assertEqual((wash, wash_alpha), ((255, 80, 72), 0.11))
        self.assertGreaterEqual(_contrast(meta, _over(wash, wash_alpha, fill)), 4.5)

    def test_the_wash_would_break_a_boxed_pair(self):
        # Positive control: Telegram Day's incoming quote colour on its quote tint
        # passes on the plain fill and fails once the deleted wash sits under it,
        # which is why the boxes are drawn over the plain fill.
        tokens = self.palettes["telegram"]
        fill = _triplet(tokens["--tg-other"])
        quote = _triplet(tokens["--tg-quote-in"])
        self.assertGreaterEqual(_contrast(quote, self._tinted(tokens, "--tg-quote-bg-in", fill)), 4.5)
        wash, wash_alpha = _tint(tokens["--tg-deleted-wash"])
        washed = _over(wash, wash_alpha, fill)
        self.assertLess(_contrast(quote, self._tinted(tokens, "--tg-quote-bg-in", washed)), 4.5)

    def test_the_switch_reads_on_the_panel(self):
        """A switch is a non-text control: 3:1 against the panel it sits on. Off
        is a ring and a thumb in n400, on is the track in --tg-switch-on."""
        for name, tokens in self.palettes.items():
            panel = _triplet(tokens["--tg-sidebar"])
            self._check(name, "switch off ring on the panel", _triplet(tokens["--tg-n400"]), panel, 3.0)
            self._check(name, "switch on track on the panel", _triplet(tokens["--tg-switch-on"]), panel, 3.0)
            # A ticked box or radio (the What changed filter, the admin
            # checklists) uses the same colour, and its white check sits on it.
            self._check(name, "check mark on a ticked box", panel, _triplet(tokens["--tg-switch-on"]), 3.0)

    def test_ticked_boxes_and_radios_use_the_switch_colour(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        for selector in (
            '[aria-checked="true"] > .tg-check {',
            '[aria-checked="true"] > .tg-radio {',
            ".admin-checkbox {",
        ):
            rule = html[html.index(selector) :]
            rule = rule[: rule.index("}")]
            assert "--tg-switch-on" in rule, selector
            assert "--tg-accent-strong" not in rule, selector

    def test_the_edit_history_marks_stay_readable(self):
        """Added words keep the side's text colour on the green tint; removed
        words take the time colour on the faint red tint."""
        for name, tokens in self.palettes.items():
            for side in ("in", "out"):
                for fill in self._fills(tokens, side):
                    added = self._tinted(tokens, "--tg-diff-ins-bg", fill)
                    removed = self._tinted(tokens, "--tg-diff-del-bg", fill)
                    self._check(name, f"text-{side} on the added tint", _triplet(tokens[f"--tg-text-{side}"]), added)
                    self._check(
                        name, f"meta-{side} on the removed tint", _triplet(tokens[f"--tg-meta-{side}"]), removed
                    )
                    # The underline under added words is a non-text mark: 3:1 on the fill.
                    line = _triplet(tokens["--tg-diff-ins-line"])
                    self._check(name, f"added underline on the {side} bubble", line, fill, 3.0)

    def test_added_words_carry_a_mark_beside_the_colour(self):
        """Added words are underlined and removed ones struck through, so the
        edit history reads without telling the tints apart (WCAG 1.4.1). The key
        under the header names both marks."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        for selector in (".version-bubble .diff-ins {", ".version-key .diff-ins {"):
            rule = html[html.index(selector) :]
            rule = rule[: rule.index("}")]
            assert "text-decoration: underline;" in rule, selector
            assert "rgb(var(--tg-diff-ins-line))" in rule, selector
        for selector in ("}\n        .version-bubble .diff-del {", "}\n        .version-key .diff-del {"):
            rule = html[html.index(selector) + 1 :]
            rule = rule[: rule.index("}")]
            assert "text-decoration: line-through;" in rule, selector
        assert '<span class="diff-ins text-tg-ink">Underlined</span> was added' in html
        assert ">Green</span> was added" not in html

    def test_a_missing_file_reason_reads_in_a_deleted_bubble(self):
        """A placeholder in a deleted bubble is drawn over the plain fill, like the
        other boxes, so its reason line (the time colour on the quote tint) is
        the pair measured on a live bubble. The amber reason of a file missing
        from the disk reads on the same tint."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        rule = html[html.index(".message-bubble.is-deleted .media-placeholder,") :]
        rule = rule[: rule.index("}")]
        assert "linear-gradient(var(--tg-quote-bg), var(--tg-quote-bg)), rgb(var(--tg-bubble-fill))" in rule
        for name, tokens in self.palettes.items():
            for side in ("in", "out"):
                fill = self._bubble(tokens, side)
                tint = self._tinted(tokens, f"--tg-quote-bg-{side}", fill)
                self._check(name, f"meta-{side} on the placeholder", _triplet(tokens[f"--tg-meta-{side}"]), tint)
                if side == "in":
                    self._check(name, "warning on the in placeholder", _triplet(tokens["--tg-warning"]), tint)
                # The amber disc against the tint, and its glyph on it.
                disc = _triplet(tokens["--tg-warning"])
                self._check(name, f"amber disc on the {side} placeholder", disc, tint, 3.0)
                self._check(name, "glyph on the amber disc", _triplet(tokens["--tg-warning-bg"]), disc, 3.0)

    def test_the_floating_pill_reads_over_any_content(self):
        """The floating date passes over photos and bubbles: its text is measured
        over black and over white, not only over the wallpaper."""
        for name, tokens in self.palettes.items():
            fg = _triplet(re.fullmatch(r"rgb\(([\d ]+)\)", tokens["--tg-service-fg"]).group(1))
            for under in ((0, 0, 0), (255, 255, 255)):
                pill = self._tinted(tokens, "--tg-float-pill-bg", under)
                self._check(name, f"floating pill text over {under}", fg, pill)

    def test_a_focus_ring_on_the_pane_is_seen(self):
        """Focus rings drawn on the pane (Retry, the sender avatars) take --tg-focus-pane,
        a non-text indicator at 3:1 against the pane and every wallpaper stop."""
        for name, tokens in self.palettes.items():
            focus = _triplet(tokens["--tg-focus-pane"])
            unders = [_triplet(tokens["--tg-bg"])]
            unders += [_hex(stop) for stop in re.findall(r"#[0-9A-Fa-f]{6}", tokens["--tg-wall-gradient"])]
            for under in unders:
                self._check(name, f"pane focus over {under}", focus, under, 3.0)

    def test_the_panel_controls_and_banners(self):
        for name, tokens in self.palettes.items():
            # The migration banner and the transcription nudge: text, icon and
            # Dismiss in accent-faint on accent-dim, a tint of the panel.
            dim = _triplet(tokens["--tg-accent-dim"])
            self._check(name, "accent-faint on the banner", _triplet(tokens["--tg-accent-faint"]), dim)
            self._check(name, "focus on the banner", _triplet(tokens["--tg-focus"]), dim, 3.0)
            # The What changed cards sit on n950; their kind pills.
            card = _triplet(tokens["--tg-n950"])
            self._check(name, "card text n200 on n950", _triplet(tokens["--tg-n200"]), card)
            danger_pill = _over(_triplet(tokens["--tg-danger"]), 0.15, card)
            self._check(name, "deleted pill on the card", _triplet(tokens["--tg-danger-fg"]), danger_pill)
            # The theme picker: the rows' dim text on the popover and on a hovered
            # row, and the "Match system" hint on the selected row (n700 at 70%).
            popover = _triplet(tokens["--tg-n800"])
            n300 = _triplet(tokens["--tg-n300"])
            selected = _over(_triplet(tokens["--tg-n700"]), 0.7, popover)
            self._check(name, "n300 on the popover", n300, popover)
            self._check(name, "n300 on a hovered row", n300, _triplet(tokens["--tg-n700"]))
            self._check(name, "n300 on the selected row", n300, selected)
            # The "Match system" hint is n400, a step quieter than the label.
            n400 = _triplet(tokens["--tg-n400"])
            self._check(name, "hint n400 on the selected row", n400, selected)
            self._check(name, "hint n400 on a hovered row", n400, _triplet(tokens["--tg-n700"]))
            # Chat header controls, the search field's icon and the archived-chats
            # folder avatar: non-text marks at 3:1.
            self._check(
                name, "bar icon on the header", _triplet(tokens["--tg-bar-icon"]), _triplet(tokens["--tg-header"]), 3.0
            )
            self._check(
                name, "search icon on the field", _triplet(tokens["--tg-n400"]), _triplet(tokens["--tg-field"]), 3.0
            )
            self._check(
                name,
                "folder icon on the archive avatar",
                _triplet(tokens["--tg-sidebar"]),
                _triplet(tokens["--tg-archive-avatar"]),
                3.0,
            )
            # The sign-in method switch: the chosen label on its raised thumb.
            self._check(
                name, "ink on the segment thumb", _triplet(tokens["--tg-ink"]), _triplet(tokens["--tg-seg-thumb"])
            )

    def test_sidebar_and_selection_pairs(self):
        for name, tokens in self.palettes.items():
            muted = _triplet(tokens["--tg-muted"])
            # n950 is the well that boxes on a panel sit in (versions, file cards).
            for surface in ("--tg-sidebar", "--tg-header", "--tg-field", "--tg-n950"):
                self._check(name, f"muted on {surface}", muted, _triplet(tokens[surface]))
            active = _triplet(tokens["--tg-active"])
            self._check(name, "active-text on active", _triplet(tokens["--tg-active-text"]), active)
            self._check(name, "active-muted on active", _triplet(tokens["--tg-active-muted"]), active)
            on_accent = _triplet(tokens["--tg-on-accent"])
            self._check(name, "on-accent on accent-strong", on_accent, _triplet(tokens["--tg-accent-strong"]))
            self._check(name, "on-accent on accent-hover", on_accent, _triplet(tokens["--tg-accent-hover"]))
            # Account tags: the colour on its own 10% fill, over the panel and
            # over a hovered row.
            for index in range(7):
                tag = _triplet(tokens[f"--tg-tag-{index}"])
                for surface in ("--tg-sidebar", "--tg-hover"):
                    self._check(name, f"tag {index} on {surface}", tag, _over(tag, 0.10, _triplet(tokens[surface])))
            # The deleted mark outside a bubble: search hits, the info panel.
            for surface in ("--tg-sidebar", "--tg-hover"):
                self._check(
                    name, f"deleted-fg on {surface}", _triplet(tokens["--tg-deleted-fg"]), _triplet(tokens[surface])
                )
            chip_active_bg = self._tinted(tokens, "--tg-account-chip-active-bg", active)
            self._check(name, "account chip on the selected row", _triplet(tokens["--tg-active-text"]), chip_active_bg)
            # A hovered chat row lifts its second line to text-dim and its accent to accent-bright.
            hover = _triplet(tokens["--tg-hover"])
            self._check(name, "text-dim on hover", _triplet(tokens["--tg-text-dim"]), hover)
            self._check(name, "accent-bright on hover", _triplet(tokens["--tg-accent-bright"]), hover)
            # An input's outline is its only edge: 3:1 against the field and what surrounds it.
            field_border = _triplet(tokens["--tg-field-border"])
            for surface in ("--tg-field", "--tg-sidebar", "--tg-header"):
                self._check(name, f"field-border on {surface}", field_border, _triplet(tokens[surface]), 3.0)
            # A focus ring is a non-text indicator: 3:1 on the panels. On the
            # selected row it takes active-text (see the rule checked below),
            # measured above at 4.5:1.
            focus = _triplet(tokens["--tg-focus"])
            self._check(name, "focus on sidebar", focus, _triplet(tokens["--tg-sidebar"]), 3.0)
            self._check(name, "focus on header", focus, _triplet(tokens["--tg-header"]), 3.0)
            # Link-coloured text on the panels (the info panel's links).
            for surface in ("--tg-sidebar", "--tg-header"):
                self._check(
                    name, f"accent-soft on {surface}", _triplet(tokens["--tg-accent-soft"]), _triplet(tokens[surface])
                )
            # The admin chips: outlined on the n700 well, name in n300, account in n400.
            well = _triplet(tokens["--tg-n700"])
            self._check(name, "n300 chip text on n700", _triplet(tokens["--tg-n300"]), well)
            self._check(name, "n400 chip text on n700", _triplet(tokens["--tg-n400"]), well)
            # The login error's box and the pane's error pill.
            danger_chip = _over(_triplet(tokens["--tg-danger"]), 0.15, _triplet(tokens["--tg-sidebar"]))
            self._check(name, "danger-fg on its tinted box", _triplet(tokens["--tg-danger-fg"]), danger_chip)
            self._check(
                name,
                "warning-fg on warning-bg",
                _triplet(tokens["--tg-warning-fg"]),
                _triplet(tokens["--tg-warning-bg"]),
            )

    def test_the_archive_surfaces_read(self):
        """The surfaces only an archive has: a media placeholder's two lines on
        the side's quote tint, a file's extension on its disc, the shown-once
        token panel, and the tags and deleted mark on the selected row."""
        for name, tokens in self.palettes.items():
            for side in ("in", "out"):
                for fill in self._fills(tokens, side):
                    tint = self._tinted(tokens, f"--tg-quote-bg-{side}", fill)
                    self._check(
                        name, f"placeholder type on the {side} tint", _triplet(tokens[f"--tg-text-{side}"]), tint
                    )
                    self._check(
                        name, f"placeholder reason on the {side} tint", _triplet(tokens[f"--tg-meta-{side}"]), tint
                    )
            self._check(
                name,
                "file extension on its disc",
                _triplet(tokens["--tg-accent-soft"]),
                _triplet(tokens["--tg-accent-dim"]),
            )
            self._check(
                name,
                "success-fg on success-bg",
                _triplet(tokens["--tg-success-fg"]),
                _triplet(tokens["--tg-success-bg"]),
            )

    def test_the_selected_row_ring_is_its_text_colour(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(".chat-row.is-active {\n            --tg-focus: var(--tg-active-text);", html)

    def test_pills_read_over_the_wallpaper(self):
        # Dates, service messages and the pane notes. The pane is --tg-bg under
        # the palette's gradient, so the pill is measured over both.
        for name, tokens in self.palettes.items():
            fg = _triplet(re.fullmatch(r"rgb\(([\d ]+)\)", tokens["--tg-service-fg"]).group(1))
            unders = [_triplet(tokens["--tg-bg"])]
            unders += [_hex(stop) for stop in re.findall(r"#[0-9A-Fa-f]{6}", tokens["--tg-wall-gradient"])]
            for under in unders:
                pill = self._tinted(tokens, "--tg-service-bg", under)
                self._check(name, f"pill text over {under}", fg, pill)

    def test_the_check_can_fail(self):
        # Positive control: the Night palette's old secondary text fails on its sidebar.
        self.assertLess(_contrast((108, 120, 131), (23, 33, 43)), 4.5)


class TestSenderAvatarUrl(unittest.TestCase):
    """US-211 as reshaped by v8.0: per-message sender_avatar_url points at
    /media/avatar/{chat_ref}/{message_id} — present only when the sender's
    avatar file is already on disk, and carrying no user id in the URL."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_media_path = web_main.config.media_path
        web_main.config.media_path = self.temp_dir.name
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.chat = web_main.ChatContext(account_id=1, chat_id=-1001, ref="senderAvatarRef001AB", type="group")

    def tearDown(self):
        web_main.config.media_path = self.original_media_path
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _touch_avatar(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as avatar_file:
            avatar_file.write("x")

    def _attached(self, message: dict) -> dict:
        web_main._attach_message_payload_urls([message], self.chat)
        return message

    def test_present_when_file_globs(self):
        user_id = 555000111
        avatars_dir = os.path.join(self.temp_dir.name, "avatars", "users")
        self._touch_avatar(os.path.join(avatars_dir, f"{user_id}_42.jpg"))

        message = self._attached({"id": 7, "sender_id": user_id})
        self.assertEqual(message["sender_avatar_url"], "/media/avatar/senderAvatarRef001AB/7")
        # The proof of the phase: neither the user id nor a filename in the URL.
        self.assertNotIn(str(user_id), message["sender_avatar_url"])

    def test_null_when_absent(self):
        message = self._attached({"id": 7, "sender_id": 999888777})
        self.assertIsNone(message["sender_avatar_url"])

    def test_null_for_missing_or_non_user_sender(self):
        self.assertIsNone(self._attached({"id": 7, "sender_id": None})["sender_avatar_url"])
        # Negative ids are channels/groups, never a users/ avatar.
        self.assertIsNone(self._attached({"id": 7, "sender_id": -1001234})["sender_avatar_url"])


class TestSenderResolution(unittest.TestCase):
    """US-211's successor: the membership probe is gone. Entitlement to the chat
    is the membership proof, and the sender is resolved from the MESSAGE row
    (_message_sender_id), so no arbitrary user id can be probed at all."""

    def setUp(self):
        web_main._sender_lookup_cache.clear()
        self.chat = web_main.ChatContext(account_id=1, chat_id=-1001, ref="senderLookupRef001AB", type="group")

    def tearDown(self):
        web_main._sender_lookup_cache.clear()

    def _with_db(self, lookup):
        original_db = web_main.db
        web_main.db = type("D", (), {"get_message_sender_id": staticmethod(lookup)})()
        return original_db

    def test_sender_resolved_from_the_message_row(self):
        """The db lookup receives the resolved chat's identity, never URL input."""
        lookup = AsyncMock(return_value=555)
        original_db = self._with_db(lookup)
        try:
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 9)), 555)
            lookup.assert_awaited_once_with(-1001, 9, account_id=1)
        finally:
            web_main.db = original_db

    def test_sender_lookup_is_cached_across_requests(self):
        """The row lookup runs once per (account, chat, message); repeats hit the cache."""
        lookup = AsyncMock(return_value=555)
        original_db = self._with_db(lookup)
        try:
            for _ in range(3):
                self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 9)), 555)
            lookup.assert_awaited_once()
        finally:
            web_main.db = original_db

    def test_missing_sender_is_cached_as_none(self):
        """A message without a sender resolves (and caches) None — the route 404s."""
        lookup = AsyncMock(return_value=None)
        original_db = self._with_db(lookup)
        try:
            self.assertIsNone(asyncio.run(web_main._message_sender_id(self.chat, 9)))
        finally:
            web_main.db = original_db

    def test_cache_is_scoped_by_message(self):
        """Different messages never share a cached sender."""
        lookup = AsyncMock(side_effect=[555, 777])
        original_db = self._with_db(lookup)
        try:
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 1)), 555)
            self.assertEqual(asyncio.run(web_main._message_sender_id(self.chat, 2)), 777)
        finally:
            web_main.db = original_db


class TestMigrationBannerTemplate(unittest.TestCase):
    """US-203 (#228): the display-only migration banner."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_banner_computed_reads_migrate_pointers(self):
        self.assertIn("const migrationBanner = computed(() =>", self.html)
        start = self.html.index("const migrationBanner = computed(() =>")
        body = self.html[start : start + 900]
        self.assertIn("raw.migrate_to_id != null", body)
        self.assertIn("raw.migrate_from_id != null", body)
        self.assertIn("This group continues as a supergroup →", body)
        self.assertIn("← Migrated from ", body)

    def test_banner_is_rendered_and_exported(self):
        # Rendered only when the pointer data exists (degrades to no banner).
        self.assertIn('v-if="migrationBanner && !showPinnedOnly"', self.html)
        self.assertIn("{{ migrationBanner.text }}", self.html)
        self.assertIn("migrationBanner,", self.html)  # exported from setup()


class TestGroupAvatarRenderTemplate(unittest.TestCase):
    """US-210/US-211: the avatar slot renders photo-or-initials in group chats."""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_avatar_gutter_is_group_and_non_own_only(self):
        self.assertIn('v-if="isGroup && !isOwnMessage(msg)"', self.html)

    def test_imported_supergroups_are_treated_as_groups(self):
        start = self.html.index("const isGroup = computed(() =>")
        body = self.html[start : start + 400]
        self.assertIn("'supergroup'", body)

    def test_photo_falls_back_to_initials_on_error(self):
        # Mirror the chat.avatar_url @error pattern: null the url so the
        # initials template renders instead.
        self.assertIn('v-if="msg.sender_avatar_url"', self.html)
        self.assertIn('@error="msg.sender_avatar_url = null"', self.html)
        self.assertIn("{{ getSenderInitials(msg) }}", self.html)

    def test_sender_avatar_img_is_lazy(self):
        # The sender-avatar <img> must be lazy like every other <img> so a group
        # page doesn't eagerly fetch every member avatar.
        start = self.html.index('v-if="msg.sender_avatar_url"')
        img = self.html[start : start + 300]
        self.assertIn('loading="lazy"', img)

    def test_deferred_download_is_documented(self):
        # A visible note that proactive member-avatar download is deferred.
        self.assertIn("slice 2b", self.html)


# ---------------------------------------------------------------------------
# US-211 backend coverage (FIX 4): real adapter query + real-endpoint ACL /
# sender_avatar_url wiring.
# ---------------------------------------------------------------------------


@pytest.fixture
async def adapter():
    """Real in-memory SQLite adapter (mirrors test_messages_page_batching)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db_manager = DatabaseManager.__new__(DatabaseManager)
    db_manager.engine = engine
    db_manager.database_url = "sqlite+aiosqlite://"
    db_manager._is_sqlite = True
    db_manager.async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    yield DatabaseAdapter(db_manager)
    await engine.dispose()


async def _seed_message(adapter, *, msg_id, chat_id, sender_id):
    async with adapter.db_manager.async_session_factory() as session:
        session.add(
            Message(
                id=msg_id,
                chat_id=chat_id,
                sender_id=sender_id,
                date=datetime(2026, 1, 1, 12, 0, 0),
                text="hi",
            )
        )
        await session.commit()


class TestSenderHasMessageInChats:
    """FIX 4a: direct adapter test of the membership probe (real SQLite)."""

    async def test_true_when_user_spoke_in_visible_chat(self, adapter):
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, [-500]) is True

    async def test_false_when_user_not_in_visible_chats(self, adapter):
        # User 42 spoke only in -500; probing a different chat set → False.
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, [-999]) is False
        # A different user who never spoke → False.
        assert await adapter.sender_has_message_in_chats(77, [-500]) is False

    async def test_false_for_empty_chat_ids_must_not_match_all(self, adapter):
        # Empty scope must NEVER match-all (would leak avatars to unauthorized
        # viewers). Even with a matching message present, empty → False.
        await _seed_message(adapter, msg_id=1, chat_id=-500, sender_id=42)
        assert await adapter.sender_has_message_in_chats(42, []) is False


@unittest.skipUnless(_HTTPX_AVAILABLE, "httpx not available")
class TestMemberAvatarAclEndpoint(unittest.IsolatedAsyncioTestCase):
    """FIX 4b reshaped: avatar allow/deny through the REAL v8.0 avatar routes.

    /media/avatar/{chat_ref}[/{message_id}] — entitlement to the chat is the
    membership proof; the sender comes from the message row; user-id-addressed
    avatar URLs no longer exist, so no arbitrary user can even be probed.
    """

    VISIBLE_REF = "visibleChatRef0500AB"
    OTHER_REF = "otherChatRef00999ABC"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "avatars" / "users").mkdir(parents=True)
        (root / "avatars" / "chats").mkdir(parents=True)
        (root / "avatars" / "users" / "42_1.jpg").write_bytes(b"x")  # sender of msg 1
        (root / "avatars" / "chats" / "-500_1.jpg").write_bytes(b"x")  # visible chat
        (root / "avatars" / "chats" / "-999_1.jpg").write_bytes(b"x")  # other chat

        self._saved_root = web_main._media_root
        self._saved_media_path = web_main.config.media_path
        self._saved_db = web_main.db
        self._saved_display = web_main.config.display_chat_ids
        web_main._media_root = root.resolve()
        web_main.config.media_path = self.temp_dir.name  # avatar lookup root
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._sender_lookup_cache.clear()

        # Restricted viewer authorized only for the visible chat's ref.
        viewer = web_main.UserContext(username="v", role="viewer", allowed_chat_refs={self.VISIBLE_REF})
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: viewer

        chats = {
            self.VISIBLE_REF: {"id": -500, "account_id": 1, "ref": self.VISIBLE_REF, "type": "group"},
            self.OTHER_REF: {"id": -999, "account_id": 1, "ref": self.OTHER_REF, "type": "group"},
        }

        async def _by_ref(ref, **kwargs):
            return chats.get(ref)

        # Message 1 in the visible chat was sent by user 42; message 2 has no sender.
        async def _sender(chat_id, message_id, account_id=None):
            if chat_id == -500 and message_id == 1:
                return 42
            return None

        self.mock_db = AsyncMock()
        self.mock_db.get_chat_by_ref = AsyncMock(side_effect=_by_ref)
        self.mock_db.get_message_sender_id = AsyncMock(side_effect=_sender)
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        web_main._media_root = self._saved_root
        web_main.config.media_path = self._saved_media_path
        web_main.db = self._saved_db
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        web_main._sender_lookup_cache.clear()
        self.temp_dir.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_member_avatar_allowed(self):
        """The avatar of a message's sender in an entitled chat is served."""
        async with self._client() as client:
            resp = await client.get(f"/media/avatar/{self.VISIBLE_REF}/1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("private", resp.headers.get("cache-control", ""))

    async def test_unknown_message_or_forbidden_chat_blocked(self):
        """No sender resolvable → 404; a chat outside the grant → the SAME 404."""
        async with self._client() as client:
            no_sender = await client.get(f"/media/avatar/{self.VISIBLE_REF}/2")
            forbidden = await client.get(f"/media/avatar/{self.OTHER_REF}/1")
        self.assertEqual(no_sender.status_code, 404)
        self.assertEqual(forbidden.status_code, 404)

    async def test_chat_avatar_visible_allowed_and_other_blocked(self):
        async with self._client() as client:
            ok = await client.get(f"/media/avatar/{self.VISIBLE_REF}")
            blocked = await client.get(f"/media/avatar/{self.OTHER_REF}")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(blocked.status_code, 404)

    async def test_legacy_user_addressed_avatar_url_is_gone(self):
        """The old /media/avatars/users/{id}_... shape 404s at routing — user ids
        are simply unaddressable now."""
        async with self._client() as client:
            resp = await client.get("/media/avatars/users/42_1.jpg")
        self.assertEqual(resp.status_code, 404)

    async def test_db_error_fails_closed_no_bytes(self):
        """A DB error in the sender lookup serves no bytes: a connection-shaped
        error answers 503 (the redacting handler's split), never the avatar."""
        self.mock_db.get_message_sender_id = AsyncMock(side_effect=ConnectionRefusedError("db down"))
        async with self._client() as client:
            resp = await client.get(f"/media/avatar/{self.VISIBLE_REF}/1")
        self.assertEqual(resp.status_code, 503)
        self.assertNotEqual(resp.content, b"x")


@unittest.skipUnless(_HTTPX_AVAILABLE, "httpx not available")
class TestMessagesEndpointAvatarWiring(unittest.IsolatedAsyncioTestCase):
    """FIX 4c: GET /messages attaches sender_avatar_url via the endpoint."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        avatars = Path(self.temp_dir.name) / "avatars" / "users"
        avatars.mkdir(parents=True)
        (avatars / "42_1.jpg").write_bytes(b"x")  # user 42 has a file on disk

        # _sender_avatar_url resolves via config.media_path + _avatar_cache.
        self._saved_media_path = web_main.config.media_path
        self._saved_db = web_main.db
        self._saved_display = web_main.config.display_chat_ids
        web_main.config.media_path = self.temp_dir.name
        web_main.config.display_chat_ids = set()
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None

        viewer = web_main.UserContext(username="v", role="master")
        web_main.app.dependency_overrides[web_main.require_auth] = lambda: viewer

        self.mock_db = AsyncMock()
        self.mock_db.get_chat_by_ref = AsyncMock(
            return_value={"id": -500, "account_id": 1, "ref": "wiringChatRef0500ABC", "type": "group"}
        )
        self.mock_db.get_messages_paginated = AsyncMock(
            return_value=[
                {"id": 1, "sender_id": 42, "chat_id": -500},
                {"id": 2, "sender_id": 77, "chat_id": -500},
            ]
        )
        web_main.db = self.mock_db

    def tearDown(self):
        web_main.app.dependency_overrides.pop(web_main.require_auth, None)
        web_main.config.media_path = self._saved_media_path
        web_main.db = self._saved_db
        web_main.config.display_chat_ids = self._saved_display
        web_main._avatar_cache.clear()
        web_main._avatar_cache_time = None
        self.temp_dir.cleanup()

    def _client(self):
        return AsyncClient(transport=ASGITransport(app=web_main.app), base_url="http://test")

    async def test_sender_avatar_url_present_when_file_globs_and_null_when_absent(self):
        async with self._client() as client:
            resp = await client.get("/api/chats/wiringChatRef0500ABC/messages")
        self.assertEqual(resp.status_code, 200)
        by_id = {m["id"]: m for m in resp.json()}
        # Ref+message addressed: no user id, no filename in the URL.
        self.assertEqual(by_id[1]["sender_avatar_url"], "/media/avatar/wiringChatRef0500ABC/1")
        self.assertIsNone(by_id[2]["sender_avatar_url"])


if __name__ == "__main__":
    unittest.main()
