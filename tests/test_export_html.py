"""The HTML export's renderer and ZIP writer, without a database (``telegram_archive.web.export_html``).

Formatting is a port of the viewer's ``renderEntityHtml``: the nesting
fixtures below are what the viewer's JavaScript produces for the same input.
Everything a message carries is escaped, links keep to their allowed schemes,
and no stored name can steer a ZIP entry anywhere.
"""

import io
import os
import re
import zipfile
from collections import Counter
from datetime import UTC
from html.parser import HTMLParser
from pathlib import Path

import pytest

from telegram_archive.web import export_html
from telegram_archive.web.export_html import (
    HtmlExportOptions,
    _PageContext,
    iter_html_zip,
    media_entry_name,
    parse_media_kinds,
    render_index,
    render_message,
    render_page,
    render_text,
    safe_href,
)

ALL = frozenset(export_html.MEDIA_KINDS)
OPTIONS = HtmlExportOptions(media=ALL, max_file_bytes=50 * 1024 * 1024, players=True)


def _msg(message_id: int = 1, **extra) -> dict:
    msg = {
        "id": message_id,
        "date": "2026-03-01T09:00:00",
        "sender": {"name": "Fixture Sender", "username": None},
        "text": "fixture",
        "is_outgoing": False,
        "reply_to": None,
        "is_deleted": False,
        "deleted_at": None,
        "edit_date": None,
        "edit_hide": None,
        "media": [],
        "versions": [],
        "reaction_history": [],
        "snapshots": [],
        "raw": {"entities": []},
        "reply_to_text": None,
        "is_pinned": False,
        "reactions": [],
    }
    msg.update(extra)
    return msg


def _media(media_type: str, name: str, *, message_id: int = 1, **extra) -> dict:
    media = {
        "media_id": f"m{message_id}_{media_type}",
        "type": media_type,
        "file_name": name,
        "file_size": 10,
        "mime_type": None,
        "width": None,
        "height": None,
        "duration": 75,
        "file_path": f"chat/{name}",
        "downloaded": True,
    }
    media.update(extra)
    return media


async def _aiter(items):
    for item in items:
        yield item


async def _zip(messages, *, root: Path, options=OPTIONS, poster=None) -> zipfile.ZipFile:
    def resolve(file_path):
        path = root / file_path
        return path if path.is_file() else None

    body = b"".join(
        [
            piece
            async for piece in iter_html_zip(
                _aiter(messages),
                chat={"title": "Fixture Chat"},
                filters=None,
                options=options,
                resolve_media=resolve,
                poster=poster,
                tz=UTC,
                top="ChatExport_Fixture",
            )
        ]
    )
    return zipfile.ZipFile(io.BytesIO(body))


class TestEntities:
    @pytest.mark.parametrize(
        ("kind", "extra", "expected"),
        [
            ("bold", {}, "<strong>x</strong>"),
            ("italic", {}, "<em>x</em>"),
            ("underline", {}, "<u>x</u>"),
            ("strikethrough", {}, "<s>x</s>"),
            ("code", {}, '<code class="tg-code">x</code>'),
            ("pre", {"language": "python"}, '<pre class="tg-pre"><code>x</code></pre>'),
            ("blockquote", {}, '<blockquote class="tg-blockquote">x</blockquote>'),
            ("spoiler", {}, '<span class="tg-spoiler" tabindex="0">x</span>'),
            (
                "text_url",
                {"url": "https://ok.example/"},
                '<a href="https://ok.example/" target="_blank" rel="noopener noreferrer">x</a>',
            ),
            ("mention", {}, '<span class="tg-mention">x</span>'),
            ("mention_name", {"user_id": 5}, '<span class="tg-mention">x</span>'),
            ("custom_emoji", {"document_id": 5}, "x"),
        ],
    )
    def test_each_entity_type(self, kind, extra, expected):
        assert render_text("a x b", [{"type": kind, "offset": 2, "length": 1, **extra}]) == f"a {expected} b"

    def test_url_and_email(self):
        assert render_text("go https://ok.example", [{"type": "url", "offset": 3, "length": 18}]) == (
            'go <a href="https://ok.example" target="_blank" rel="noopener noreferrer">https://ok.example</a>'
        )
        assert render_text("go me@x.org", [{"type": "email", "offset": 3, "length": 8}]) == (
            'go <a href="mailto:me@x.org">me@x.org</a>'
        )
        # A url entity over a non-http text is no link.
        assert render_text("go ftp://x", [{"type": "url", "offset": 3, "length": 7}]) == "go ftp://x"

    def test_an_offset_inside_a_surrogate_pair_never_splits_the_character(self):
        # Misaligned offsets (Telegram does not send them): the emoji stays whole, as the viewer shows it.
        html = render_text('h"👍', [{"type": "text_url", "offset": 1, "length": 2, "url": "https://ok.example"}])
        assert "\ufffd" not in html and "👍" in html
        html = render_text("a👍b", [{"type": "bold", "offset": 2, "length": 2}])
        assert "\ufffd" not in html and "👍" in html

    def test_offsets_are_utf16_code_units(self):
        assert render_text("👍 **x**", [{"type": "bold", "offset": 5, "length": 1}]) == "👍 **<strong>x</strong>**"
        assert render_text("👍 x", [{"type": "bold", "offset": 3, "length": 1}]) == "👍 <strong>x</strong>"
        # An astral character inside the entity counts two units.
        assert render_text("a 𝔘𝔫 b", [{"type": "italic", "offset": 2, "length": 4}]) == "a <em>𝔘𝔫</em> b"

    # Output of the viewer's renderEntityHtml for the same text and entities.
    VIEWER_FIXTURES = [
        (
            "bold *italic* end",
            [{"type": "bold", "offset": 0, "length": 13}, {"type": "italic", "offset": 5, "length": 8}],
            "<strong>bold <em>*italic*</em></strong> end",
        ),
        (
            "overlap here now",
            [{"type": "bold", "offset": 0, "length": 10}, {"type": "italic", "offset": 5, "length": 11}],
            "<strong>overl<em>ap he</em></strong>re now",
        ),
        ("👍 x y", [{"type": "bold", "offset": 3, "length": 1}], "👍 <strong>x</strong> y"),
        ("a 𝔘𝔫 b", [{"type": "italic", "offset": 2, "length": 4}], "a <em>𝔘𝔫</em> b"),
        (
            "see https://ex.org now",
            [{"type": "url", "offset": 4, "length": 14}, {"type": "bold", "offset": 0, "length": 22}],
            '<strong>see <a href="https://ex.org" target="_blank" rel="noopener noreferrer">https://ex.org</a>'
            " now</strong>",
        ),
        (
            "code <b> & stuff",
            [{"type": "code", "offset": 5, "length": 3}],
            'code <code class="tg-code">&lt;b&gt;</code> &amp; stuff',
        ),
        (
            "link text",
            [{"type": "text_url", "offset": 0, "length": 4, "url": "https://ok.example/?a=1&b=2"}],
            '<a href="https://ok.example/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">link</a> text',
        ),
        ("mail me@x.org", [{"type": "email", "offset": 5, "length": 8}], 'mail <a href="mailto:me@x.org">me@x.org</a>'),
        (
            "same start",
            [{"type": "italic", "offset": 0, "length": 4}, {"type": "bold", "offset": 0, "length": 10}],
            "<strong><em>same</em> start</strong>",
        ),
    ]

    @pytest.mark.parametrize(("text", "entities", "expected"), VIEWER_FIXTURES)
    def test_nesting_matches_the_viewer(self, text, entities, expected):
        assert render_text(text, entities) == expected

    def test_malformed_entities_degrade_to_plain_text(self):
        assert render_text("a <b>", [{"type": "bold", "offset": "0", "length": True}]) == "a &lt;b&gt;"
        assert render_text("a", "not a list") == "a"
        assert render_text(None, None) == ""

    def test_plain_text_links_only_http(self):
        assert render_text("x javascript:alert(1) https://ok.example", None) == (
            'x javascript:alert(1) <a href="https://ok.example" target="_blank" '
            'rel="noopener noreferrer">https://ok.example</a>'
        )


PAYLOADS = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "</textarea><script>x</script>"]
BAD_URLS = ["javascript:alert(1)", "data:text/html,<script>1</script>", "vbscript:msgbox(1)"]


class _Elements(HTMLParser):
    """Every start tag of a page, with its attributes, as a browser would parse them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _assert_inert(html: str) -> None:
    parser = _Elements()
    parser.feed(html)
    for tag, attrs in parser.tags:
        assert tag not in ("script", "iframe", "object", "embed", "textarea"), tag
        assert not [name for name in attrs if name.startswith("on")], (tag, attrs)
        for name in ("href", "src", "poster"):
            value = (attrs.get(name) or "").strip().lower()
            assert not value.startswith(("javascript:", "data:", "vbscript:")), (tag, attrs)
        if tag == "img":
            assert attrs.get("src") != "x", attrs
    assert "<script" not in html.lower()


class TestEscaping:
    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_every_field_is_escaped(self, payload):
        msg = _msg(
            text=payload,
            sender={"name": payload},
            reply_to=1,
            reply_to_text=payload,
            raw={
                "entities": [{"type": "text_url", "offset": 0, "length": 3, "url": BAD_URLS[0]}],
                "forward_from_name": payload,
                "webpage": {"url": BAD_URLS[0], "title": payload, "description": payload, "site_name": payload},
                "post_author": payload,
            },
            media=[_media("voice", payload, file_path=None)],
            media_payload={
                "poll": {"question": payload, "answers": [{"text": payload, "option": "AA=="}]},
                "contact": {"first_name": payload, "last_name": payload, "phone_number": payload},
                "venue": {"title": payload, "address": payload, "lat": 1.0, "long": 2.0},
            },
            transcripts=[{"media_id": "m1_voice", "text": payload}],
            versions=[
                {"text": payload, "date": "2026-03-01T08:00:00", "entities": None, "media": [{"file_name": payload}]}
            ],
            reactions=[{"emoji": payload, "count": 1}],
        )
        ctx = _PageContext(options=OPTIONS, tz=UTC, page_first_ids=[1])
        ctx.add_message(1)
        ctx.plans = {
            "m1_voice": export_html._MediaPlan(
                media=msg["media"][0], kind="voice", display_name=payload, skip="not_downloaded"
            )
        }
        html = render_message(msg, ctx)
        _assert_inert(html)
        assert "&lt;" in html or "&quot;" in html

    @pytest.mark.parametrize("payload", [*PAYLOADS, 'x" onmouseover="alert(1)', "x' onmouseover='alert(1)"])
    def test_attributes_are_escaped(self, payload):
        """An included file's name lands in ``download=``; an allowed link may carry a quote."""
        msg = _msg(
            text="click here",
            raw={"entities": [{"type": "text_url", "offset": 0, "length": 5, "url": f"https://ok.example/{payload}"}]},
            media=[_media("voice", "a.ogg")],
        )
        ctx = _PageContext(options=OPTIONS, tz=UTC)
        ctx.plans = {
            "m1_voice": export_html._MediaPlan(
                media=msg["media"][0],
                kind="voice",
                display_name=payload,
                entry="voice_messages/1_1.ogg",
                player="audio",
            )
        }
        html = render_message(msg, ctx)
        _assert_inert(html)
        assert 'download="' in html and "<audio" in html

    @pytest.mark.parametrize("url", BAD_URLS)
    def test_text_url_with_a_bad_scheme_is_text(self, url):
        html = render_text("click", [{"type": "text_url", "offset": 0, "length": 5, "url": url}])
        assert html == "click"

    def test_page_and_index_escape_the_chat_title(self):
        page = render_page({"title": PAYLOADS[0]}, "", 1, False)
        index = render_index({"title": PAYLOADS[1]}, {"from": PAYLOADS[0]}, OPTIONS, [(1, "a", "b", 1)], Counter())
        _assert_inert(page)
        _assert_inert(index)


class TestSafeHref:
    @pytest.mark.parametrize("url", ["https://ok.example", "http://ok.example", "tg://resolve?domain=x", "HTTPS://ok"])
    def test_allowed(self, url):
        assert safe_href(url, schemes=frozenset({"http", "https", "tg"})) == url

    @pytest.mark.parametrize(
        "url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "\x01javascript:alert(1)", "//evil.example", "", None, 5]
    )
    def test_refused(self, url):
        assert safe_href(url, schemes=frozenset({"http", "https", "tg"})) is None

    def test_tg_only_where_allowed(self):
        assert safe_href("tg://x", schemes=frozenset({"http", "https"})) is None


ENTRY_RE = re.compile(r"^[a-z_]+/\d+_\d+(\.[A-Za-z0-9]{1,8})?$")


class TestEntryNames:
    @pytest.mark.parametrize(
        "stored",
        [
            "../../etc/passwd",
            "a/b\\c",
            "x\x00y.jpg",
            "Ф" * 150 + ".jpg",
            "x.#?",
            "",
            None,
            "photo.jpeg",
            "a.toolongext1",
        ],
    )
    def test_names_stay_in_their_folder(self, stored):
        name = media_entry_name("photos", 12, 1, stored)
        assert ENTRY_RE.match(name), name
        assert len(name.encode("utf-8")) <= 143
        assert name.startswith("photos/12_1")

    def test_a_safe_extension_is_kept(self):
        assert media_entry_name("files", 3, 2, "900_report.PDF") == "files/3_2.PDF"

    @pytest.mark.parametrize("stored", ["invite.html", "a.HTM", "pic.svg", "x.xhtml", "y.SVGZ", "z.mhtml", "s.js"])
    def test_an_extension_a_browser_runs_is_dropped(self, stored):
        assert media_entry_name("files", 7, 1, stored) == "files/7_1"

    @pytest.mark.parametrize(
        ("title", "expected"),
        [("Ф" * 200, "Ф" * 50), ("../../x", "x"), ("🎉", "chat"), ("", "chat"), ("a/b\\c", "abc")],
    )
    def test_the_top_folder_name(self, title, expected):
        name = export_html.safe_top_name(title)
        assert name == expected
        assert len(name.encode("utf-8")) <= 100 and "/" not in name and "." not in name


class TestMediaCards:
    async def test_each_skip_reason_draws_a_card_without_a_link(self, tmp_path):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "big.mp4").write_bytes(b"x" * 2048)
        messages = [
            _msg(1, media=[_media("photo", "a.jpg", message_id=1)]),
            _msg(2, media=[_media("video", "b.mp4", message_id=2, downloaded=False)]),
            _msg(3, media=[_media("video", "gone.mp4", message_id=3)]),
            _msg(4, media=[_media("video", "big.mp4", message_id=4)]),
        ]
        options = HtmlExportOptions(media=frozenset({"video"}), max_file_bytes=1024, players=True)
        archive = await _zip(messages, root=tmp_path, options=options)
        page = archive.read("ChatExport_Fixture/messages.html").decode()
        lines = {n: next(line for line in page.splitlines() if f'id="message{n}"' in line) for n in (1, 2, 3, 4)}
        for number, text in (
            (1, "Not included in this export"),
            (2, "Not downloaded to the archive"),
            (3, "Missing from the archive"),
            (4, "Larger than the 0 MB limit"),
        ):
            assert text in lines[number], number
            assert "href=" not in lines[number], number
        index = archive.read("ChatExport_Fixture/index.html").decode()
        for text in ("Not included in this export: 1", "Not downloaded to the archive: 1", "storage: 1"):
            assert text in index

    async def test_players_on_and_off(self, tmp_path):
        (tmp_path / "chat").mkdir()
        for name in ("v.mp4", "a.ogg", "g.mp4", "r.mp4", "s.mp3"):
            (tmp_path / "chat" / name).write_bytes(b"bytes")
        messages = [
            _msg(1, media=[_media("video", "v.mp4", message_id=1)]),
            _msg(2, media=[_media("voice", "a.ogg", message_id=2)]),
            _msg(3, media=[_media("animation", "g.mp4", message_id=3)]),
            _msg(4, media=[_media("video_note", "r.mp4", message_id=4)]),
            _msg(5, media=[_media("audio", "s.mp3", message_id=5)]),
        ]
        on = (await _zip(messages, root=tmp_path)).read("ChatExport_Fixture/messages.html").decode()
        assert on.count("<video") == 3 and on.count("<audio") == 2
        assert 'href="video_files/1_1.mp4"' in on and 'class="round"' in on
        assert '<div class="description">1:15</div>' in on
        off_options = HtmlExportOptions(media=ALL, max_file_bytes=1 << 20, players=False)
        off = (
            (await _zip(messages, root=tmp_path, options=off_options)).read("ChatExport_Fixture/messages.html").decode()
        )
        assert "<video" not in off and "<audio" not in off
        for path in ("video_files/1_1.mp4", "voice_messages/2_1.ogg", "animations/3_1.mp4", "files/5_1.mp3"):
            assert f'href="{path}"' in off

    @pytest.mark.parametrize(
        ("media_type", "name", "mime", "expected", "absent"),
        [
            ("photo", "p.jpg", "image/jpeg", '<a class="photo_wrap" href="photos/1_1.jpg"><img class="photo"', None),
            ("photo", "p.svg", "image/svg+xml", 'href="photos/1_1"', "<img"),
            ("sticker", "s.webp", "image/webp", '<img class="sticker" loading="lazy" src="stickers/1_1.webp"', None),
            (
                "sticker",
                "s.webm",
                "video/webm",
                '<video loop muted playsinline controls preload="metadata" src="stickers/1_1.webm">',
                None,
            ),
            ("sticker", "s.tgs", "application/x-tgsticker", "Animated sticker", "<video"),
            (
                "animation",
                "g.mp4",
                "video/mp4",
                '<video loop muted playsinline controls preload="metadata" src="animations/1_1.mp4">',
                "autoplay",
            ),
            ("animation", "g.gif", "image/gif", '<img class="sticker" loading="lazy" src="animations/1_1.gif"', None),
        ],
    )
    async def test_the_player_each_file_gets(self, tmp_path, media_type, name, mime, expected, absent):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / name).write_bytes(b"bytes")
        messages = [_msg(1, media=[_media(media_type, name, mime_type=mime)])]
        page = (await _zip(messages, root=tmp_path)).read("ChatExport_Fixture/messages.html").decode()
        assert expected in page
        if absent:
            assert absent not in page

    async def test_a_skipped_static_sticker_is_not_called_animated(self, tmp_path):
        messages = [
            _msg(1, media=[_media("sticker", "s.webp", mime_type="image/webp", downloaded=False)]),
            _msg(2, media=[_media("sticker", "t.tgs", message_id=2, mime_type="application/x-tgsticker")]),
        ]
        options = HtmlExportOptions(media=frozenset({"photo"}), max_file_bytes=1 << 20, players=True)
        page = (await _zip(messages, root=tmp_path, options=options)).read("ChatExport_Fixture/messages.html").decode()
        first = next(line for line in page.splitlines() if 'id="message1"' in line)
        second = next(line for line in page.splitlines() if 'id="message2"' in line)
        assert "Animated sticker" not in first and "Sticker" in first
        assert "Animated sticker" in second

    async def test_a_shared_file_that_fails_to_open_is_missing_for_every_media_sharing_it(self, tmp_path, monkeypatch):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "a.jpg").write_bytes(b"\xff\xd8 one blob")
        os.link(tmp_path / "chat" / "a.jpg", tmp_path / "chat" / "b.jpg")
        real_open = open

        def failing_open(path, *args, **kwargs):
            if str(path).endswith("a.jpg"):
                raise PermissionError("fixture")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(export_html, "open", failing_open, raising=False)
        media = [
            _media("photo", "a.jpg", media_id="first"),
            _media("photo", "b.jpg", media_id="second"),
        ]
        archive = await _zip([_msg(1, media=media)], root=tmp_path)
        assert not [name for name in archive.namelist() if "/photos/" in name]
        page = archive.read("ChatExport_Fixture/messages.html").decode()
        assert 'href="photos/' not in page
        assert page.count("Missing from the archive") == 2
        assert "Missing from the archive&#x27;s storage: 2" in archive.read("ChatExport_Fixture/index.html").decode()

    async def test_a_read_error_inside_an_entry_breaks_the_zip(self, tmp_path, monkeypatch):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "v.mp4").write_bytes(b"x" * 4096)
        real_open = open

        class Failing:
            def __init__(self, handle):
                self._handle = handle
                self.calls = 0

            def fileno(self):
                return self._handle.fileno()

            def read(self, size):
                self.calls += 1
                if self.calls > 1:
                    raise OSError("fixture read error")
                return self._handle.read(size)

            def close(self):
                self._handle.close()

        monkeypatch.setattr(export_html, "open", lambda path, mode: Failing(real_open(path, mode)), raising=False)
        received = []

        def resolve(file_path):
            return tmp_path / file_path

        with pytest.raises(OSError, match="fixture read error"):
            async for piece in iter_html_zip(
                _aiter([_msg(1, media=[_media("video", "v.mp4")])]),
                chat={"title": "Fixture Chat"},
                filters=None,
                options=OPTIONS,
                resolve_media=resolve,
                poster=None,
                tz=UTC,
                top="ChatExport_Fixture",
            ):
                received.append(piece)
        with pytest.raises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(b"".join(received)))

    async def test_a_poster_is_written_beside_its_video(self, tmp_path):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "v.mp4").write_bytes(b"bytes")
        (tmp_path / "still.webp").write_bytes(b"RIFF still")

        async def poster(media, source, may_generate):
            return tmp_path / "still.webp"

        archive = await _zip([_msg(1, media=[_media("video", "v.mp4")])], root=tmp_path, poster=poster)
        assert archive.read("ChatExport_Fixture/video_files/1_1_thumb.webp") == b"RIFF still"
        assert 'poster="video_files/1_1_thumb.webp"' in archive.read("ChatExport_Fixture/messages.html").decode()

    async def test_a_shared_video_keeps_its_poster_where_it_appears_again(self, tmp_path):
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "v.mp4").write_bytes(b"bytes")
        os.link(tmp_path / "chat" / "v.mp4", tmp_path / "chat" / "w.mp4")
        (tmp_path / "still.webp").write_bytes(b"RIFF still")

        async def poster(media, source, may_generate):
            return tmp_path / "still.webp"

        messages = [_msg(1, media=[_media("video", "v.mp4")]), _msg(2, media=[_media("video", "w.mp4", message_id=2)])]
        page = (await _zip(messages, root=tmp_path, poster=poster)).read("ChatExport_Fixture/messages.html").decode()
        assert page.count('poster="video_files/1_1_thumb.webp"') == 2

    async def test_posters_stop_being_made_once_the_budget_is_spent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_html, "POSTER_BUDGET_SECONDS", 0.0)
        (tmp_path / "chat").mkdir()
        (tmp_path / "chat" / "v.mp4").write_bytes(b"bytes")
        asked = []

        async def poster(media, source, may_generate):
            asked.append(may_generate)
            return None

        await _zip([_msg(1, media=[_media("video", "v.mp4")])], root=tmp_path, poster=poster)
        assert asked == [False]


class TestPages:
    async def test_a_page_ends_at_the_message_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_html, "MAX_MESSAGES_PER_PAGE", 3)
        archive = await _zip([_msg(n) for n in range(1, 8)], root=tmp_path)
        names = archive.namelist()
        assert [n for n in names if "/messages" in n] == [
            "ChatExport_Fixture/messages.html",
            "ChatExport_Fixture/messages2.html",
            "ChatExport_Fixture/messages3.html",
        ]
        assert archive.read("ChatExport_Fixture/messages3.html").decode().count('class="message default') == 1

    def test_the_default_limits(self):
        assert export_html.MAX_MESSAGES_PER_PAGE == 1000
        assert export_html.MAX_PLAYERS_PER_PAGE == 40

    async def test_a_page_ends_before_its_players_pass_the_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_html, "MAX_PLAYERS_PER_PAGE", 2)
        (tmp_path / "chat").mkdir()
        messages = []
        for n in range(1, 6):
            (tmp_path / "chat" / f"{n}.mp4").write_bytes(b"x")
            messages.append(_msg(n, media=[_media("video", f"{n}.mp4", message_id=n)]))
        archive = await _zip(messages, root=tmp_path)
        pages = [n for n in archive.namelist() if "/messages" in n]
        assert len(pages) == 3
        for name in pages:
            assert archive.read(name).decode().count("<video") <= 2

    async def test_a_reply_links_to_the_page_of_its_target(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_html, "MAX_MESSAGES_PER_PAGE", 2)
        messages = [_msg(n) for n in (10, 11, 12, 13)]
        messages.append(_msg(14, reply_to=12, reply_to_text="quoted"))
        messages.append(_msg(15, reply_to=3))
        archive = await _zip(messages, root=tmp_path)
        last = archive.read("ChatExport_Fixture/messages3.html").decode()
        assert 'href="messages2.html#go_to_message12"' in last
        assert '<span class="reply_quote">quoted</span>' in last
        # Outside the export: said so, with an id-only link the importer still reads.
        assert 'In reply to <a href="#go_to_message3">a message outside this export</a>' in last
        assert 'id="go_to_message12"' in archive.read("ChatExport_Fixture/messages2.html").decode()

    async def test_a_reply_to_a_gap_in_the_export_says_it_is_outside(self, tmp_path):
        # Ids 1 and 10 exported, 10 answers 5: inside the id range, not in the export.
        messages = [_msg(1), _msg(10, reply_to=5), _msg(11, reply_to=1)]
        page = (await _zip(messages, root=tmp_path)).read("ChatExport_Fixture/messages.html").decode()
        assert 'In reply to <a href="#go_to_message5">a message outside this export</a>' in page
        assert "messages.html#go_to_message5" not in page
        assert 'href="messages.html#go_to_message1"' in page

    async def test_an_empty_export_says_so(self, tmp_path):
        archive = await _zip([], root=tmp_path)
        index = archive.read("ChatExport_Fixture/index.html").decode()
        assert "no messages in this export" in index and "Page 1" not in index
        assert "ChatExport_Fixture/messages.html" in archive.namelist()

    async def test_the_last_pieces_stay_near_the_chunk_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_html, "CHUNK", 4096)
        monkeypatch.setattr(export_html, "_PAGE_WRITE_SLICE", 1024)
        (tmp_path / "chat").mkdir()
        messages = []
        for n in range(1, 301):
            (tmp_path / "chat" / f"{n}.bin").write_bytes(b"x")
            messages.append(_msg(n, media=[_media("document", f"{n}.bin", message_id=n)]))
        pieces = [
            piece
            async for piece in iter_html_zip(
                _aiter(messages),
                chat={"title": "Fixture Chat"},
                filters=None,
                options=OPTIONS,
                resolve_media=lambda fp: tmp_path / fp,
                poster=None,
                tz=UTC,
                top="ChatExport_Fixture",
            )
        ]
        assert len(zipfile.ZipFile(io.BytesIO(b"".join(pieces))).namelist()) == 303
        # 303 central-directory records are far more than one CHUNK; they come out in CHUNK pieces.
        tail = b"".join(pieces[-6:])
        assert b"PK\x01\x02" in tail and b"PK\x05\x06" in pieces[-1]
        assert all(len(piece) <= 4096 for piece in pieces[-6:])

    async def test_every_page_is_offline_and_script_free(self, tmp_path):
        archive = await _zip([_msg(1, text="https://ok.example"), _msg(2, text="<script>x</script>")], root=tmp_path)
        for name in archive.namelist():
            if not name.endswith(".html"):
                continue
            page = archive.read(name).decode()
            assert "Content-Security-Policy" in page and "script-src 'none'" in page
            assert '<meta name="referrer" content="no-referrer">' in page
            assert "<script" not in page.lower()
            assert not re.search(r'(src|<link[^>]*href)="https?://', page)


class TestSpool:
    async def test_the_source_is_read_to_the_end_before_the_first_message(self):
        from datetime import datetime

        finished = []

        async def source():
            try:
                for n in range(1, 4):
                    yield {"id": n, "date": datetime(2026, 3, 1, 9, n), "text": "é"}
            finally:
                finished.append(True)

        spooled = export_html.spool_messages(source())
        first = await anext(spooled)
        assert finished == [True]
        assert first == {"id": 1, "date": "2026-03-01T09:01:00", "text": "é"}
        rest = [msg async for msg in spooled]
        assert [msg["id"] for msg in rest] == [2, 3]

    async def test_an_error_in_the_source_reaches_the_caller(self):
        async def source():
            yield {"id": 1}
            raise RuntimeError("fixture")

        with pytest.raises(RuntimeError, match="fixture"):
            [msg async for msg in export_html.spool_messages(source())]


class TestMarkers:
    def test_a_service_message_with_no_stored_wording_names_its_action(self):
        ctx = _PageContext(options=OPTIONS, tz=UTC)
        html = render_message(_msg(1, text="", raw={"service_type": "service", "action_type": "chat_edit_title"}), ctx)
        assert "Chat edit title</div>" in html
        html = render_message(_msg(2, text=None, raw={"service_type": "service"}), ctx)
        assert "Service message</div>" in html
        html = render_message(_msg(3, text="Stored wording", raw={"service_type": "service"}), ctx)
        assert "Stored wording</div>" in html

    def test_a_text_only_index_names_no_size_limit(self):
        index = render_index(
            {"title": "x"}, None, HtmlExportOptions(frozenset(), 50 << 20, True), [(1, "a", "b", 1)], Counter()
        )
        assert "none (text only)" in index and "MB" not in index and "1 message<" in index

    def test_deleted_and_edited_messages(self):
        ctx = _PageContext(options=OPTIONS, tz=UTC)
        deleted = render_message(_msg(1, is_deleted=True, deleted_at="2026-03-02T10:00:00"), ctx)
        assert "message default clearfix deleted" in deleted and "Deleted from Telegram on 02.03.2026 10:00" in deleted
        edited = render_message(
            _msg(
                2,
                edit_date="2026-03-01T10:00:00",
                edit_hide=0,
                versions=[
                    {
                        "text": "first draft",
                        "date": "2026-03-01T09:00:00",
                        "entities": [{"type": "bold", "offset": 0, "length": 5}],
                        "media": [{"file_name": "900_old.jpg"}],
                    }
                ],
            ),
            ctx,
        )
        assert 'title="01.03.2026 10:00:00 UTC+00:00">edited</span>' in edited
        assert "<summary>Earlier versions (1)</summary>" in edited
        assert "<strong>first</strong> draft" in edited and "Media: old.jpg" in edited
        hidden = render_message(_msg(3, edit_date="2026-03-01T10:00:00", edit_hide=1), ctx)
        assert ">edited<" not in hidden

    def test_date_title_is_what_the_importer_parses(self):
        from zoneinfo import ZoneInfo

        from telegram_archive.telegram_import import parse_html_date

        ctx = _PageContext(options=OPTIONS, tz=ZoneInfo("Europe/Madrid"))
        html = render_message(_msg(1, date="2026-07-01T09:00:00"), ctx)
        title = re.search(r'class="pull_right date details" title="([^"]+)"', html).group(1)
        assert title == "01.07.2026 11:00:00 UTC+02:00"
        assert parse_html_date(title) == "2026-07-01T11:00:00+02:00"

    def test_parse_media_kinds(self):
        assert parse_media_kinds(None) == frozenset()
        assert parse_media_kinds("photo, gif,") == frozenset({"photo", "gif"})
        with pytest.raises(ValueError, match="'foo'"):
            parse_media_kinds("photo,foo")
