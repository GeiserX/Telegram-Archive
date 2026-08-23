"""Executable regressions for the viewer's entity-formatting renderer (9t6.10.1)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "index.html"


def _extract_between(html: str, start: str, end: str) -> str:
    i = html.index(start)
    j = html.index(end, i)
    return html[i:j]


def _renderer_bundle(html: str) -> str:
    """The renderer plus every template helper it calls, escapeHtml stubbed.

    The shipped escapeHtml is DOM-based (document.createElement), so the node
    harness substitutes a pure equivalent; everything else runs verbatim.
    """
    parts = [
        _extract_between(html, "const isHttpUrl", "const TAG_TOKEN_RE"),
        _extract_between(html, "const TAG_TOKEN_RE", "const safeEntityUrl"),
        _extract_between(html, "const safeEntityUrl", "const renderMessageHtml"),
        _extract_between(html, "const renderMessageHtml", "// Resolve the rendered row"),
    ]
    code = "\n".join(parts)
    names = (
        "isHttpUrl|escapeUrlForAttr|TAG_TOKEN_RE|isTagToken|tagifyText|linkifyText|"
        "safeEntityUrl|wrapEntity|ESCAPE_ONLY_ENTITY_TYPES|renderEntityHtml|renderMessageHtml"
    )
    code = re.sub(rf"\bconst ({names})", r"globalThis.\1", code)
    stub = (
        "globalThis.escapeHtml = (t) => String(t)"
        ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')"
        ".replace(/\"/g, '&quot;').replace(/'/g, '&#39;');\n"
    )
    return stub + code


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable is not installed")

    # SECURITY-REVIEW: The executable path is resolved locally and untrusted input is never passed to a shell.
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"Node behavior test failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _run_renderer_asserts(assert_body: str) -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            _renderer_bundle(html),
            "const R = (text, ents) => renderMessageHtml({ text, raw_data: { entities: ents } });",
            assert_body,
        ]
    )
    _run_node(script)


def test_entity_text_is_escaped_before_markup() -> None:
    """Hostile text inside a formatted span must never become live HTML."""
    _run_renderer_asserts(
        """
const html = R('<img src=x onerror=alert(1)> hi', [{type: 'bold', offset: 0, length: 27}])
assert.ok(!html.includes('<img'), html)
assert.ok(html.includes('&lt;img'), html)
assert.ok(html.startsWith('<strong>'), html)
"""
    )


def test_nested_entities_render_as_nested_tags() -> None:
    _run_renderer_asserts(
        """
assert.equal(
    R('hola mundo', [{type: 'bold', offset: 0, length: 10}, {type: 'italic', offset: 5, length: 5}]),
    '<strong>hola <em>mundo</em></strong>')
"""
    )


def test_spoiler_is_one_span_and_blurred_by_default() -> None:
    """One spoiler entity produces ONE reveal target, not per-segment shards."""
    _run_renderer_asserts(
        """
const html = R('el secreto grande', [{type: 'spoiler', offset: 3, length: 14}])
assert.equal((html.match(/tg-spoiler/g) || []).length, 1, html)
assert.ok(html.includes('role="button"'), html)
assert.ok(!html.includes('tg-spoiler-revealed'), html)
"""
    )


def test_code_keeps_characters_literal_and_untagged() -> None:
    """No linkify/tagify inside code — #test must stay text, < must escape."""
    _run_renderer_asserts(
        """
const html = R('run #test <now>', [{type: 'code', offset: 4, length: 11}])
assert.ok(html.includes('<code class="tg-code">#test &lt;now&gt;</code>'), html)
assert.ok(!html.includes('tag-link'), html)
"""
    )


def test_text_url_rejects_javascript_scheme() -> None:
    _run_renderer_asserts(
        """
assert.equal(R('click', [{type: 'text_url', offset: 0, length: 5, url: 'javascript:alert(1)'}]), 'click')
const ok = R('click', [{type: 'text_url', offset: 0, length: 5, url: 'https://ok.example'}])
assert.ok(ok.includes('href="https://ok.example"'), ok)
assert.ok(ok.includes('rel="noopener noreferrer"'), ok)
"""
    )


def test_url_entity_anchors_once_without_double_linkify() -> None:
    _run_renderer_asserts(
        """
const html = R('see https://x.example/a now', [{type: 'url', offset: 4, length: 19}])
assert.equal((html.match(/<a /g) || []).length, 1, html)
assert.ok(html.includes('href="https://x.example/a"'), html)
"""
    )


def test_no_entities_falls_back_to_linkify() -> None:
    """Old rows (and malformed payloads) render exactly as before this change."""
    _run_renderer_asserts(
        """
const plain = R('plain https://y.example #tag', null)
assert.ok(plain.includes('href="https://y.example"'), plain)
assert.ok(plain.includes('tag-link'), plain)
assert.equal(R('x', []), 'x')
assert.equal(R('short', [{type: 'bold', offset: 99, length: 4}]), 'short')
"""
    )


def test_utf16_offsets_apply_natively() -> None:
    """Telegram offsets are UTF-16 units; an emoji (surrogate pair) before the
    entity must not shift the formatted range."""
    _run_renderer_asserts(
        """
assert.equal(
    R('X \\u{1F3B2} bold', [{type: 'bold', offset: 5, length: 4}]),
    'X \\u{1F3B2} <strong>bold</strong>')
"""
    )


def test_blockquote_and_pre_wrap_as_blocks() -> None:
    _run_renderer_asserts(
        """
assert.equal(
    R('quoted', [{type: 'blockquote', offset: 0, length: 6}]),
    '<blockquote class="tg-blockquote">quoted</blockquote>')
const pre = R('x = 1', [{type: 'pre', offset: 0, length: 5, language: 'py'}])
assert.equal(pre, '<pre class="tg-pre"><code>x = 1</code></pre>')
"""
    )


def test_spoiler_reveal_does_not_hijack_revealed_links() -> None:
    """Concealed: first activation reveals and suppresses navigation. Revealed:
    the span stops intercepting so url/text_url/tag links inside work."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("const toggleSpoiler")
    end = html.index("}", html.index("spoiler.classList.add", start)) + 1
    script = "\n".join(
        [
            '"use strict";',
            "const assert = require('node:assert/strict');",
            html[start:end].replace("const toggleSpoiler", "globalThis.toggleSpoiler"),
            """
const mkEvent = (revealed) => {
    const cls = new Set(revealed ? ['tg-spoiler', 'tg-spoiler-revealed'] : ['tg-spoiler'])
    const spoiler = { classList: { contains: (c) => cls.has(c), add: (c) => cls.add(c) } }
    let prevented = false
    return {
        ev: { target: { closest: (sel) => sel === '.tg-spoiler' ? spoiler : null }, preventDefault: () => { prevented = true } },
        cls,
        wasPrevented: () => prevented,
    }
}
const concealed = mkEvent(false)
toggleSpoiler(concealed.ev)
assert.ok(concealed.cls.has('tg-spoiler-revealed'))
assert.ok(concealed.wasPrevented())
const revealed = mkEvent(true)
toggleSpoiler(revealed.ev)
assert.ok(!revealed.wasPrevented())
""",
        ]
    )
    _run_node(script)
    # And the tag delegate must leave concealed-spoiler tags to the reveal.
    assert ".tg-spoiler:not(.tg-spoiler-revealed)" in html


def test_template_uses_entity_renderer_for_message_text() -> None:
    """The message bubble must route through renderMessageHtml (album-aware)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'v-html="renderMessageHtml(getAlbumCaptionMessage(msg) || msg)"' in html
    assert 'v-html="linkifyText(getAlbumCaption(msg) || msg.text)"' not in html
