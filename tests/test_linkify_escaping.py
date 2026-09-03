"""Executable regressions for the viewer's message linkifier.

``linkifyText`` turns fully attacker-controlled text — anything anyone can send
to the archived account — into markup that the SPA renders through ``v-html``.
It used to HTML-escape the whole message and then decode ``&amp;`` back to ``&``
while building each href. That decode was a hole: the HTML parser decodes the
attribute a *second* time, so a message could smuggle an entity through it and
send the link somewhere its own visible text never showed. A message reading
``https://good.example&#x40;evil.example/`` navigated to ``evil.example``,
because ``&#x40;`` came back as ``@`` and turned the visible host into userinfo.

The tests below execute the real helpers lifted verbatim out of the shipped
template and parse what they emit with an HTML parser, so they pin behaviour
rather than source text.
"""

import json
import re
import shutil
import subprocess
import tempfile
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"

# ``escapeHtml`` round-trips through the DOM (``div.textContent`` in,
# ``div.innerHTML`` out). This stub reproduces the HTML fragment serializer for
# a text node: ``&``, ``<``, ``>`` and U+00A0 are escaped and nothing else is —
# quotes in particular stay raw, which is precisely why an href needs its own
# encoding rather than the text escaper. Checked against jsdom's serializer.
_DOCUMENT_STUB = r"""
const document = {
    createElement: () => ({
        set textContent(value) { this.value = String(value) },
        get innerHTML() {
            return this.value
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/ /g, '&nbsp;')
        },
    }),
}
"""

# Every payload is one message body containing exactly one link. Hosts are
# reserved ``.example`` names, so nothing here resolves anywhere real.
PAYLOADS = {
    "plain_url_with_query": "see https://good.example/a?x=1&y=2 ok",
    "http_scheme": "http://good.example/x",
    "raw_double_quote": 'https://good.example/"onmouseover="alert(1)',
    "raw_single_quote": "https://good.example/'onmouseover='alert(1)",
    "raw_angle_brackets": "https://good.example/<img/src=x/onerror=alert(1)>",
    "entity_quote_breakout": "https://good.example/&quot;onmouseover=&quot;alert(1)",
    "entity_tag_injection": "https://good.example/&quot;&gt;&lt;img/src=x/onerror=alert(1)&gt;",
    "double_entity_tag_injection": (
        "https://good.example/&amp;quot;&amp;gt;&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;"
    ),
    "hex_entity_userinfo": "https://good.example&#x40;evil.example/",
    "decimal_entity_userinfo": "https://good.example&#64;evil.example/",
    "named_entity_userinfo": "https://good.example&commat;evil.example/",
    "entity_userinfo_in_a_sentence": "Sign in at https://good.example&#x40;evil.example/ now",
    "entity_userinfo_with_colon": "https://good.example&#x3A;x&#x40;evil.example/",
    "entity_newline_in_host": "https://good.example&#10;evil.example/",
    "entity_tab_in_path": "https://good.example/&#9;x",
    "entity_slashes": "https://good.example&#47;&#47;evil.example/",
    "legacy_entity_without_semicolon": "https://good.example/&quot",
    "already_escaped_ampersand": "https://good.example/?a=1&amp;b=2",
    "trailing_punctuation": "visit https://good.example/page. now",
    "unicode_idn": "https://münchen.example/straße?q=ü",
    "non_breaking_space": "https://good.example/ next",
    "very_long_url": "https://good.example/" + "a" * 5000,
}

# The four characters that cannot sit raw inside href="..." and are therefore
# percent-encoded on the way in. Everything else must reach the href untouched.
HREF_PERCENT_ENCODED = {"<": "%3C", ">": "%3E", '"': "%22", "'": "%27"}


# The host a reader takes from a link's visible text: the hostname run right
# after the scheme. Parsed and compared whole, never prefix-matched — a
# ``startswith("https://good.example")`` premise would also accept
# ``https://good.example.evil.example/``.
_VISIBLE_HOST = re.compile(r"^https?://([A-Za-z0-9.-]+)")


def _visible_host(link_text: str) -> str | None:
    match = _VISIBLE_HOST.match(link_text)
    return match.group(1) if match else None


def _expected_href(link_text: str) -> str:
    """The only href the viewer may produce for a link showing ``link_text``.

    The destination is the text the reader can see, with just the four
    unrepresentable characters percent-encoded — nothing decoded, nothing added.
    """
    return "".join(HREF_PERCENT_ENCODED.get(char, char) for char in link_text)


def _setup_slice(html: str, declaration: str) -> str:
    """Return one setup-scope ``const`` body, lifted verbatim from the template.

    Setup-scope declarations sit at 16 spaces of indentation, so the next such
    line ends the current one; anything nested is indented deeper.
    """
    start = html.index(declaration)
    return html[start : html.index("\n                const ", start + len(declaration))]


@cache
def _rendered() -> dict[str, str]:
    """Run the template's real linkifier over every payload and return its markup."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")

    html = INDEX_HTML.read_text(encoding="utf-8")
    program = "\n".join(
        [
            '"use strict";',
            _DOCUMENT_STUB,
            _setup_slice(html, "const escapeHtml = (text) =>"),
            _setup_slice(html, "const escapeUrlForAttr = (url) =>"),
            _setup_slice(html, "const TAG_TOKEN_RE = "),
            _setup_slice(html, "const isTagToken = (sigil, body) =>"),
            _setup_slice(html, "const tagifyText = (raw) =>"),
            _setup_slice(html, "const linkifyText = (text) =>"),
            f"const payloads = {json.dumps(PAYLOADS)};",
            "const rendered = {};",
            "for (const name of Object.keys(payloads)) rendered[name] = linkifyText(payloads[name]);",
            "console.log(JSON.stringify(rendered));",
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "linkify.js"
        script.write_text(program, encoding="utf-8")
        # SECURITY-REVIEW: the executable path is resolved locally and no
        # untrusted input is ever passed to a shell.
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr + "\n----\n" + program
    return json.loads(result.stdout)


class _Markup(HTMLParser):
    """Read linkified markup the way a browser would, using only the stdlib."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []
        self.anchors: list[tuple[dict[str, str | None], str]] = []
        self._text: list[str] = []
        self._open_anchor: tuple[dict[str, str | None], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(attrs)
        if tag == "a":
            self._open_anchor = (dict(attrs), [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._open_anchor is not None:
            attributes, chunks = self._open_anchor
            self.anchors.append((attributes, "".join(chunks)))
            self._open_anchor = None

    def handle_data(self, data: str) -> None:
        self._text.append(data)
        if self._open_anchor is not None:
            self._open_anchor[1].append(data)

    @property
    def text(self) -> str:
        return "".join(self._text)


def _parse(markup: str) -> _Markup:
    parser = _Markup()
    parser.feed(markup)
    parser.close()
    return parser


def _only_anchor(name: str) -> tuple[dict[str, str | None], str]:
    """Return the single anchor a payload produces, as (attributes, link text)."""
    parsed = _parse(_rendered()[name])
    assert len(parsed.anchors) == 1, f"{name} produced {len(parsed.anchors)} anchors"
    return parsed.anchors[0]


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_href_is_the_visible_link_text_and_nothing_else(name: str) -> None:
    """No entity may be decoded on the way into the href.

    This is the double-escaping regression itself: escape-then-decode let the
    parser resolve a smuggled entity inside the attribute, so the href stopped
    matching the text the reader was shown.
    """
    attributes, link_text = _only_anchor(name)

    assert attributes["href"] == _expected_href(link_text)


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_link_navigates_to_the_host_it_displays(name: str) -> None:
    """The host the browser resolves must be the host printed on the page."""
    attributes, link_text = _only_anchor(name)

    assert urlsplit(attributes["href"] or "").netloc == urlsplit(link_text).netloc


@pytest.mark.parametrize(
    "name",
    [
        "hex_entity_userinfo",
        "decimal_entity_userinfo",
        "named_entity_userinfo",
        "entity_userinfo_in_a_sentence",
        "entity_userinfo_with_colon",
        "entity_newline_in_host",
        "entity_slashes",
    ],
    ids=lambda name: name,
)
def test_entity_smuggling_cannot_retarget_a_link(name: str) -> None:
    """An ``@``/newline entity must not turn the displayed host into userinfo.

    Each payload reads as a link to ``good.example``; the second decode used to
    resolve its entity and hand the whole visible host to ``evil.example`` as
    userinfo, so the click landed on the attacker's site.
    """
    attributes, link_text = _only_anchor(name)
    href = attributes["href"] or ""

    assert _visible_host(link_text) == "good.example"
    assert urlsplit(href).hostname != "evil.example", href


def test_visible_host_reads_the_whole_hostname() -> None:
    """The premise helper must reject a lookalike that merely starts with the host."""
    assert _visible_host("https://good.example/x") == "good.example"
    assert _visible_host("https://good.example.evil.example/") == "good.example.evil.example"
    assert _visible_host("https://good.example&#x40;evil.example/") == "good.example"
    assert _visible_host("https://good.example@evil.example/") == "good.example"
    assert _visible_host("not a link") is None


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_payloads_render_as_an_inert_anchor(name: str) -> None:
    """Nothing but the anchor itself may survive into the DOM."""
    markup = _rendered()[name]
    parsed = _parse(markup)

    assert parsed.tags == ["a"]
    assert {attribute for attribute, _ in parsed.attributes} == {"href", "target", "rel"}
    assert not any(attribute.startswith("on") for attribute, _ in parsed.attributes)
    assert "<script" not in markup.lower()
    assert "javascript:" not in markup.lower()


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_href_keeps_an_http_scheme(name: str) -> None:
    """A link's scheme comes from the matched url and can never be swapped."""
    attributes, _ = _only_anchor(name)

    assert urlsplit(attributes["href"] or "").scheme in {"http", "https"}


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_rendered_text_is_the_message_verbatim(name: str) -> None:
    """Escaping happens exactly once, so the reader sees the message unchanged."""
    parsed = _parse(_rendered()[name])

    assert parsed.text == PAYLOADS[name]


def test_ordinary_link_resolves_exactly_as_before() -> None:
    """The everyday case — one url with an ``&`` query — still resolves the same.

    The emitted markup does change here: a query ``&`` is now written ``&amp;``
    instead of leaning on the parser's recovery for a bare ``&``. What the
    browser ends up with — href, link text, target, rel — is identical.
    """
    assert _rendered()["plain_url_with_query"] == (
        "see "
        '<a href="https://good.example/a?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">'
        "https://good.example/a?x=1&amp;y=2</a>"
        " ok"
    )
    attributes, _ = _only_anchor("plain_url_with_query")
    assert attributes["href"] == "https://good.example/a?x=1&y=2"
    assert attributes["target"] == "_blank"
    assert attributes["rel"] == "noopener noreferrer"


def test_message_without_a_url_is_plain_escaped_text() -> None:
    """Text outside a url is escaped once and linked to nothing."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")

    html = INDEX_HTML.read_text(encoding="utf-8")
    program = "\n".join(
        [
            '"use strict";',
            _DOCUMENT_STUB,
            _setup_slice(html, "const escapeHtml = (text) =>"),
            _setup_slice(html, "const escapeUrlForAttr = (url) =>"),
            _setup_slice(html, "const TAG_TOKEN_RE = "),
            _setup_slice(html, "const isTagToken = (sigil, body) =>"),
            _setup_slice(html, "const tagifyText = (raw) =>"),
            _setup_slice(html, "const linkifyText = (text) =>"),
            "console.log(JSON.stringify(["
            "linkifyText('a <b>bold</b> & \"quoted\" claim'),"
            "linkifyText(''),"
            "linkifyText(null),"
            "]));",
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "linkify.js"
        script.write_text(program, encoding="utf-8")
        # SECURITY-REVIEW: the executable path is resolved locally and no
        # untrusted input is ever passed to a shell.
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    plain, empty, missing = json.loads(result.stdout)

    assert plain == 'a &lt;b&gt;bold&lt;/b&gt; &amp; "quoted" claim'
    assert _parse(plain).tags == []
    assert _parse(plain).text == 'a <b>bold</b> & "quoted" claim'
    assert empty == ""
    assert missing == ""


def test_linkifier_never_decodes_what_it_escaped() -> None:
    """Guard the shape CodeQL flags: escaping must not be partially undone."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    linkify = _setup_slice(html, "const linkifyText = (text) =>")
    encode = _setup_slice(html, "const escapeUrlForAttr = (url) =>")

    for source in (linkify, encode):
        assert "&amp;/g, '&'" not in source
        assert "&lt;/g" not in source
        assert "&gt;/g" not in source
    assert "escapeUrlForAttr(part)" in linkify
    assert "escapeHtml(part)" in linkify
    # Plain-text segments route through the tag tokenizer, which escapes
    # per sub-segment with the same escaper.
    assert "tagifyText(part)" in linkify


def test_tags_become_anchors_without_breaking_escaping() -> None:
    """#hashtags and $cashtags render as tag anchors; escaping stays intact.

    Executes the shipped tokenizer: official shapes only (a hashtag needs a
    non-digit, a cashtag is 1-8 uppercase letters), never inside a URL (the
    linkifier's URL branch wins), and markup around a tag is still escaped.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")

    html = INDEX_HTML.read_text(encoding="utf-8")
    cases = {
        "plain_hashtag": "launch day #news arrived",
        "cashtag": "buy $TSLA now",
        "digit_hashtag": "issue #123 stays plain",
        "lowercase_cashtag": "$usd stays plain",
        "mid_word": "word#glued stays plain",
        "url_fragment": "see https://good.example/page#frag ok",
        "markup_around_tag": "<b>#x</b>",
        "unicode_digit_hashtag": "arabic digits #\u0661\u0662\u0663 stay plain",
        "overlong_hashtag": "#" + "a" * 70 + " no partial anchor",
    }
    program = "\n".join(
        [
            '"use strict";',
            _DOCUMENT_STUB,
            _setup_slice(html, "const escapeHtml = (text) =>"),
            _setup_slice(html, "const escapeUrlForAttr = (url) =>"),
            _setup_slice(html, "const TAG_TOKEN_RE = "),
            _setup_slice(html, "const isTagToken = (sigil, body) =>"),
            _setup_slice(html, "const tagifyText = (raw) =>"),
            _setup_slice(html, "const linkifyText = (text) =>"),
            f"const cases = {json.dumps(cases)};",
            "const rendered = {};",
            "for (const name of Object.keys(cases)) rendered[name] = linkifyText(cases[name]);",
            "console.log(JSON.stringify(rendered));",
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "tagify.js"
        script.write_text(program, encoding="utf-8")
        # SECURITY-REVIEW: the executable path is resolved locally and no
        # untrusted input is ever passed to a shell.
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)

    assert '<a href="#" class="tag-link" data-tag="#news">#news</a>' in rendered["plain_hashtag"]
    assert 'data-tag="$TSLA"' in rendered["cashtag"]
    for plain in (
        "digit_hashtag",
        "lowercase_cashtag",
        "mid_word",
        "url_fragment",
        "unicode_digit_hashtag",
        "overlong_hashtag",
    ):
        assert "tag-link" not in rendered[plain], plain
    assert rendered["markup_around_tag"] == ('&lt;b&gt;<a href="#" class="tag-link" data-tag="#x">#x</a>&lt;/b&gt;')
