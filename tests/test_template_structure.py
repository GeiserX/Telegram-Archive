"""The viewer template must be structurally sound HTML (8.3.0 regression).

8.3.0 shipped with the export modal's closing tags amputated (a modal was
spliced in mid-element). Browsers recover from that per spec by force-closing
open elements — which ejected every later modal OUT of the ``#app`` mount
target. Vue compiled only the top of the page; the ejected modals rendered as
raw ``{{ mustaches }}`` stacked over the UI with no working close buttons.
No gate could catch it: ``node --check`` sees only the script, and there is
no browser test in CI.

This test enforces the invariant that makes spec-recovery unreachable: the
template's element tree is PERFECTLY balanced (every non-void element closed,
no stray close tags, no button-in-button), and every piece of the app UI —
the late modals and the toast included — sits inside ``#app``. It fails on
the exact 8.3.0 template.
"""

import os
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"

VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}

# Elements whose nesting the HTML parser rewrites via implied end tags —
# nesting them is exactly the recovery behavior this test exists to keep
# unreachable. (li/td/etc. are legal to repeat as siblings only when closed;
# this template always closes them explicitly, so the strict rule holds.)
NO_SELF_NESTING = {"button", "form", "a", "p", "select", "option", "label"}

# The only elements this template legitimately self-closes: SVG icon children,
# where foreign-content parsing honors the slash. Deliberately NOT "anything
# inside <svg>" — an HTML element like <div/> stays open even there (and
# foreignObject re-enters HTML parsing), so it must keep being flagged.
SVG_SELF_CLOSING_CHILDREN = {
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "use",
    "stop",
}


class StrictBalanceAuditor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self.app_depth: int | None = None
        self.inside_app_text: list[str] = []

    def _line(self) -> int:
        return self.getpos()[0]

    def handle_starttag(self, tag, attrs):
        if tag in VOID_ELEMENTS:
            return
        if tag in NO_SELF_NESTING and any(open_tag == tag for open_tag, _ in self.stack):
            self.errors.append(f"line {self._line()}: <{tag}> opened inside an unclosed <{tag}>")
        if dict(attrs).get("id") == "app":
            self.app_depth = len(self.stack)
        self.stack.append((tag, self._line()))

    def handle_startendtag(self, tag, attrs):
        # WHATWG parsing IGNORES a trailing solidus on non-void HTML elements:
        # `<div/>` opens a div that stays open. Only SVG/MathML foreign
        # content honors self-closing. Treat the HTML-side illusion as an
        # error so the gate can't be green while a browser sees an unclosed
        # element.
        if tag in VOID_ELEMENTS:
            return
        if tag in SVG_SELF_CLOSING_CHILDREN and any(open_tag == "svg" for open_tag, _ in self.stack):
            return  # foreign content: the slash really closes these
        self.errors.append(
            f"line {self._line()}: <{tag}/> self-closing syntax on a non-void HTML element — "
            "browsers ignore the slash and the element stays OPEN; close it explicitly"
        )
        self.stack.append((tag, self._line()))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if not self.stack:
            self.errors.append(f"line {self._line()}: stray </{tag}> with nothing open")
            return
        open_tag, open_line = self.stack[-1]
        if open_tag != tag:
            self.errors.append(
                f"line {self._line()}: </{tag}> closes <{open_tag}> opened at line {open_line} — "
                "an element in between was never closed"
            )
            # Recover the audit (not the document): unwind to the matching tag
            # if one is open, so one amputation doesn't drown the report.
            open_tags = [t for t, _ in self.stack]
            if tag in open_tags:
                while self.stack and self.stack[-1][0] != tag:
                    self.stack.pop()
                self.stack.pop()
            return
        self.stack.pop()
        if self.app_depth is not None and len(self.stack) == self.app_depth:
            self.app_depth = None  # left #app

    def handle_data(self, data):
        if self.app_depth is not None:
            self.inside_app_text.append(data)


def _audit() -> StrictBalanceAuditor:
    auditor = StrictBalanceAuditor()
    auditor.feed(TEMPLATE.read_text(encoding="utf-8"))
    auditor.close()
    return auditor


def test_template_elements_are_perfectly_balanced():
    auditor = _audit()
    assert auditor.errors == [], "\n".join(auditor.errors[:10])
    leftovers = [(t, line) for t, line in auditor.stack if t != "html"]
    assert leftovers == [], f"elements never closed: {leftovers[:10]}"


def test_every_app_surface_lives_inside_the_mount_target():
    """The late modals and the toast must be Vue-compiled — i.e. inside #app.

    If any of these markers falls outside, browsers show it as raw mustaches
    stacked over the page (the 8.3.0 iPhone lock-out).
    """
    auditor = _audit()
    inside = "".join(auditor.inside_app_text)
    for marker in ("Admin Settings", "toastMessage", "adminTokenError", "Create Share Token"):
        assert marker in inside, f"{marker!r} is OUTSIDE #app — Vue will never compile it"
