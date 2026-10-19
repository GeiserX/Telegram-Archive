"""A video in the chat is a still, never a <video> that fetches its file (#578).

A ``<video preload="metadata">`` per clip opened a request for every video the
moment a chat rendered. Over HTTP/1.1 a browser allows about six connections to
one host, the video responses held them, and the chat's photos and its older
messages queued behind them for good. The chat, its albums and the info panel
now draw the clip's thumbnail instead, and the lightbox plays the file.

The template checks pin where each branch draws; the node checks run the real
helper block lifted out of the template, so they pin behaviour: the still's
URL, the fallback after a failed still, and the Missing placeholder when the
file itself is gone.
"""

import re

from test_frontend_audit_fixes import INDEX_HTML, _run_node

HTML = INDEX_HTML.read_text(encoding="utf-8")

BLOCK_START = "                const failedVideoStills = ref(new Set())\n"
BLOCK_END = "                const noteNaturalSize = (msg, width, height) => {\n"


def _block() -> str:
    start = HTML.index(BLOCK_START)
    return HTML[start : HTML.index(BLOCK_END, start)]


def _branch(marker: str, length: int = 2500) -> str:
    """The template text that starts at ``marker``."""
    at = HTML.index(marker)
    return HTML[at : at + length]


def test_no_video_element_fetches_its_file_when_drawn():
    """Every <video> in the page either waits for an observer (data-src) or is
    the lightbox's player. One bound to the file's URL anywhere else fetches
    it as soon as it renders."""
    # Comments name the element in prose ("a <video v-else-if>"); only markup counts.
    code = re.sub(r"//[^\n]*", "", re.sub(r"<!--.*?-->", "", HTML, flags=re.S))
    eager = []
    for match in re.finditer(r"<video\b[^>]*>", code):
        tag = match.group(0)
        if "data-src" in tag or "lightboxMedia" in tag:
            continue
        eager.append(tag)
    assert eager == []
    assert '<source :src="getMediaUrl' not in HTML


def test_chat_video_draws_a_lazy_still():
    branch = _branch("<div v-else-if=\"msg.media?.type === 'video'\">")
    assert '<img v-if="!videoStillFailed(msg)"' in branch
    assert ':src="videoStillUrl(msg)"' in branch
    assert 'loading="lazy"' in branch
    assert '@error="failVideoStill(msg)"' in branch
    # The play mark stays whether or not the still loads.
    assert 'd="M8 5v14l11-7z"' in branch


def test_album_video_draws_a_lazy_still_and_a_tile_without_one():
    branch = _branch("<!-- A video is its still (videoStillUrl), never a <video>. -->", 800)
    assert ':src="videoStillUrl(albumMsg)"' in branch
    assert 'loading="lazy"' in branch
    assert '@error="failVideoStill(albumMsg)"' in branch
    assert '<div v-else-if="albumMsg.media?.type === \'video\'" class="w-full h-full bg-tg-n700"></div>' in branch


def test_info_panel_video_draws_a_small_still():
    branch = _branch('<img v-else-if="isVideoMedia(mediaMsg)', 400)
    assert ':src="videoStillUrl(mediaMsg, 200)"' in branch
    assert '@error="failVideoStill(mediaMsg)"' in branch


PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const getMediaUrl = msg => msg.media?.url || '';
const requests = [];
let status = 206;
let fail = false;
let cancelled = 0;
const fetch = async (url, init) => {
    requests.push([url, init?.headers?.Range]);
    if (fail) throw new TypeError('NetworkError');
    return { status, body: { cancel: async () => { cancelled++ } } };
};
"""


def _run(program: str) -> None:
    _run_node(
        PRELUDE
        + _block()
        + "\n(async () => {\n"
        + program
        + "\n})().catch(e => { console.error(e); process.exit(1) });"
    )


def test_still_url_takes_the_thumbnail_lane():
    _run(
        """
        const msg = { media: { url: '/media/r1/5_video?v=2' } };
        assert.equal(videoStillUrl(msg), '/media/thumb/400/r1/5_video?v=2');
        assert.equal(videoStillUrl(msg, 200), '/media/thumb/200/r1/5_video?v=2');
        """
    )


def test_failed_still_keeps_the_play_mark_when_the_file_is_there():
    _run(
        """
        const msg = { media: { url: '/media/r1/5_video' } };
        assert.equal(videoStillFailed(msg), false);
        await failVideoStill(msg);
        assert.equal(videoStillFailed(msg), true);
        // One byte of the file, and the response is let go at once.
        assert.deepEqual(requests, [['/media/r1/5_video', 'bytes=0-0']]);
        assert.equal(cancelled, 1);
        assert.equal(msg.mediaLoadFailed, undefined);
        // A second error (the info panel's still) asks nothing more.
        await failVideoStill(msg);
        assert.equal(requests.length, 1);
        """
    )


def test_failed_still_of_a_gone_file_takes_the_missing_placeholder():
    _run(
        """
        status = 404;
        const msg = { media: { url: '/media/r1/6_video' } };
        await failVideoStill(msg);
        assert.equal(msg.mediaLoadFailed, true);
        """
    )


def test_failed_still_offline_stays_a_play_mark():
    _run(
        """
        fail = true;
        const msg = { media: { url: '/media/r1/7_video' } };
        await failVideoStill(msg);
        assert.equal(videoStillFailed(msg), true);
        assert.equal(msg.mediaLoadFailed, undefined);
        """
    )


def test_still_failures_are_kept_per_file():
    _run(
        """
        const a = { media: { url: '/media/r1/8_video' } };
        const b = { media: { url: '/media/r1/9_video' } };
        await failVideoStill(a);
        assert.equal(videoStillFailed(a), true);
        assert.equal(videoStillFailed(b), false);
        """
    )
