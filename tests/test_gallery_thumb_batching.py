"""The shared-media grid admits thumbnails a few at a time, executed.

A tile does not fetch a picture, it asks the server to make one, and a video
costs an ffmpeg run (two at a time server-side, 15 s each). A grid that showed
fifty tiles at once queued dozens of generations and starved everything behind
them: on a cold cache the message list and the gallery's own item request sat
for 7 to 39 seconds and were reset, so the pane read "Failed to load media"
while the tab badges still counted the chat's files.

These lift the real admission queue out of the template and run it under node,
so they pin the behaviour rather than the source text. They run on the vendored
Vue build's own ref and watch: the queue learns about tiles through a shallow
watch, and a stub that called the watcher by hand hid that pushing a page into
the list never fired it (#537).
"""

import json
import re

from test_frontend_audit_fixes import INDEX_HTML, _run_node

# Load the build the viewer's script tag names, so a Vue bump never edits this file.
_VUE_TAG = re.search(
    r'<script src="/static/(vendor/vue-[^"]+\.global\.prod\.js)"', INDEX_HTML.read_text(encoding="utf-8")
)
assert _VUE_TAG, "index.html no longer loads a vendored Vue build"
VUE_JS = INDEX_HTML.parents[1] / "static" / _VUE_TAG.group(1)

BLOCK_START = "                const THUMB_CONCURRENCY = 4\n"
BLOCK_END = "                const mediaTabs = [\n"
LOADER_START = "                let mediaGalleryRequestSeq = 0\n"
LOADER_END = "                const loadMediaCounts = async"

# The Vue build is too large to pass inline to `node -e`, so the script reads it.
VUE_LOADER = f"""
"use strict";
require('node:vm').runInThisContext(require('node:fs').readFileSync({json.dumps(str(VUE_JS))}, 'utf8'));
"""

PRELUDE = """
const assert = require('node:assert/strict');
const { ref, nextTick } = Vue;
// The real watch, flushed synchronously so a test reads the outcome on the next
// line. Flush timing does not change what a watch tracks: a shallow watch still
// ignores a push into the list.
const watch = (source, fn, options) => Vue.watch(source, fn, { ...options, flush: 'sync' });
const mediaGalleryItems = ref([]);
// Timers are driven by hand so the test never sleeps.
const timers = new Map();
let nextTimer = 1;
const setTimeout = (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id };
const clearTimeout = id => timers.delete(id);
const fire = id => { const t = timers.get(id); assert.ok(t, 'no such timer'); timers.delete(id); t.fn() };
// Insertion-ordered, so the first key is the oldest tile still in flight.
const oldestTimer = () => { assert.ok(timers.size, 'no pending timer'); return [...timers.keys()][0] };
const items = n => Array.from({ length: n }, (_, i) => ({ id: `${i + 1}_photo`, thumb_url: `/media/thumb/200/ref/${i + 1}_photo` }));
const admitted = () => [...admittedThumbs.value];
const failed = () => [...failedThumbs.value];
const setItems = list => { mediaGalleryItems.value = list };
"""


def _queue_block(html: str) -> str:
    start = html.index(BLOCK_START)
    return html[start : html.index(BLOCK_END, start)]


def _script(body: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    return "\n".join([VUE_LOADER, PRELUDE, _queue_block(html), body])


def test_only_a_few_tiles_are_admitted_and_each_one_that_settles_admits_the_next() -> None:
    _run_node(
        _script("""
setItems(items(10));
assert.deepEqual(admitted(), ['1_photo', '2_photo', '3_photo', '4_photo'], 'four at a time, in grid order');
assert.equal(timers.size, 4, 'each admitted tile carries a stall timer');

// A tile that loads frees its slot for exactly one more.
settleThumb('1_photo');
assert.deepEqual(admitted().slice(-1), ['5_photo']);
assert.equal(admitted().length, 5);
assert.equal(timers.size, 4);

// A 404 settles the same way a load does.
settleThumb('2_photo');
assert.deepEqual(admitted().slice(-1), ['6_photo']);

// Settling the same tile twice must not open a second slot.
settleThumb('2_photo');
assert.equal(admitted().length, 6, 'a duplicate settle admits nothing');
// ...and neither does settling a tile that was never admitted.
settleThumb('9_photo');
assert.equal(admitted().length, 6);

for (const id of ['3_photo', '4_photo', '5_photo', '6_photo']) settleThumb(id);
assert.equal(admitted().length, 10, 'the queue drains to the end');
assert.equal(timers.size, 4, 'the last four are still in flight, each with its own stall timer');
for (const id of ['7_photo', '8_photo', '9_photo', '10_photo']) settleThumb(id);
assert.equal(timers.size, 0, 'and nothing is left pending once they finish');
""")
    )


def test_a_tile_that_never_loads_releases_its_slot_on_the_timeout() -> None:
    _run_node(
        _script("""
setItems(items(5));
assert.equal(admitted().length, 4);
settleThumb('1_photo');
assert.equal(admitted().length, 5, 'the fifth is admitted, so four tiles are in flight');
assert.equal(timers.size, 4);

// Tile 2 never fires load or error. Only its timer can free that slot.
const stalled = oldestTimer();
setItems(items(5).concat([{ id: '6_photo', thumb_url: '/media/thumb/200/ref/6_photo' }]));
assert.equal(admitted().length, 5, 'a full queue admits nothing new');
fire(stalled);
assert.deepEqual(admitted().slice(-1), ['6_photo'], 'the stalled tile released its slot');
assert.equal(timers.size, 4, 'the released slot is immediately reused, not leaked');
""")
    )


def test_appending_a_page_queues_only_the_new_tiles() -> None:
    _run_node(
        _script("""
const first = items(4);
setItems(first);
assert.equal(admitted().length, 4);
// "Load more" appends; the already-admitted ids must not be queued a second time.
setItems(first.concat([{ id: '5_photo', thumb_url: '/x/5' }, { id: '6_photo', thumb_url: '/x/6' }]));
assert.equal(admitted().length, 4, 'the new page waits its turn');
settleThumb('1_photo');
assert.deepEqual(admitted().slice(-1), ['5_photo']);
settleThumb('1_photo');
assert.equal(admitted().length, 5, 'a settled tile cannot be settled again by a re-render');
""")
    )


def test_a_page_loaded_by_scrolling_gets_its_previews() -> None:
    # The real loader, not a stand-in for it: infinite scroll and "Load more" both
    # call loadMediaGallery(true), and the tiles it adds must join the queue.
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index(LOADER_START)
    loader = html[start : html.index(LOADER_END, start)]
    _run_node(
        "\n".join(
            [
                VUE_LOADER,
                PRELUDE,
                """
const page = top => Array.from({ length: 50 }, (_, i) => ({ id: `${top - i}_photo`, thumb_url: `/media/thumb/200/ref/${top - i}_photo` }));
const requests = [];
const fetch = async url => {
  requests.push(url);
  const older = url.includes('before_id=51_photo');
  return { ok: true, json: async () => older ? { items: page(50), has_more: false } : { items: page(100), has_more: true } };
};
const selectedChat = ref({ ref: 'ref' });
const mediaGalleryTab = ref('photos');
const mediaGalleryLoading = ref(false);
const mediaGalleryHasMore = ref(true);
const mediaGalleryError = ref(false);
let mediaGalleryEmptyPages = 0;
const galleryShownCount = () => mediaGalleryItems.value.length;
const loadMoreMediaIfInView = () => {};
const showToast = message => assert.fail(`unexpected toast: ${message}`);
""",
                _queue_block(html),
                loader,
                """
(async () => {
  await loadMediaGallery();
  assert.deepEqual(admitted(), ['100_photo', '99_photo', '98_photo', '97_photo'], 'the first page starts four at a time');
  for (let n = 100; n > 50; n--) settleThumb(`${n}_photo`);
  assert.equal(admitted().length, 50, 'the whole first page drew');

  await loadMediaGallery(true);
  assert.equal(requests.length, 2);
  assert.ok(requests[1].includes('before_id=51_photo'), 'the second page continues after the last tile');
  assert.equal(mediaGalleryItems.value.length, 100, 'the second page is in the grid');
  assert.deepEqual(admitted().slice(50), ['50_photo', '49_photo', '48_photo', '47_photo'], 'the second page is queued for previews');
  for (let n = 50; n > 0; n--) settleThumb(`${n}_photo`);
  assert.equal(admitted().length, 100, 'every tile loaded by scrolling gets its preview');
})().catch(error => { console.error(error); process.exit(1) });
""",
            ]
        )
    )


def test_a_tab_switch_forgets_the_previous_view_entirely() -> None:
    _run_node(
        _script("""
setItems(items(10));
assert.equal(admitted().length, 4);
// Every tab switch and every gallery open clears the list first.
setItems([]);
assert.deepEqual(admitted(), [], 'nothing stays admitted');
assert.equal(timers.size, 0, 'and no stall timer survives to fire into the new view');
setItems([{ id: '1_photo', thumb_url: '/x/1' }]);
assert.deepEqual(admitted(), ['1_photo'], 'the same id is admitted again in the new view');
""")
    )


def test_items_without_a_thumbnail_never_take_a_slot() -> None:
    _run_node(
        _script("""
setItems([
  { id: 'a_document' },                                   // files tab: no thumbnail at all
  { id: 'b_photo', thumb_url: null },                     // no-download session
  { id: 'c_photo', thumb_url: '/x/c' },
  { id: 'd_photo', thumb_url: '/x/d' },
]);
assert.deepEqual(admitted(), ['c_photo', 'd_photo'], 'only tiles that would fetch something');
""")
    )


def test_a_thumbnail_the_server_cannot_make_gives_way_to_the_tile_icon() -> None:
    _run_node(
        _script("""
setItems(items(6));
assert.deepEqual(admitted(), ['1_photo', '2_photo', '3_photo', '4_photo']);

// An undecodable video answers 404, which fires error, not load.
failThumb('2_photo');
assert.deepEqual(failed(), ['2_photo'], 'the tile is remembered as unrenderable');
assert.equal(isThumbAdmitted('2_photo'), true, 'it stays admitted, so the queue does not re-issue it');
assert.deepEqual(admitted().slice(-1), ['5_photo'], 'and it frees its slot like any other outcome');

// A load after the failure must not resurrect the broken <img>.
settleThumb('2_photo');
assert.deepEqual(failed(), ['2_photo']);
assert.equal(admitted().length, 5, 'and must not open a second slot');

// The next view starts clean: the same id can render again elsewhere.
setItems([]);
assert.deepEqual(failed(), [], 'failures do not outlive the view that produced them');
setItems([{ id: '2_photo', thumb_url: '/x/2' }]);
assert.equal(isThumbAdmitted('2_photo'), true);
assert.equal(failed().length, 0);
""")
    )


def test_the_grid_is_wired_to_the_queue() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("<template v-if=\"mediaGalleryTab === 'photos'\">")
    grid = html[start : html.index("</template>", start)]
    assert 'v-if="item.thumb_url && isThumbAdmitted(item.id) && !thumbFailed(item.id)"' in grid, (
        "the tile waits to be admitted, and steps aside once it is known to be unrenderable"
    )
    assert '@load="settleThumb(item.id)"' in grid and '@error="failThumb(item.id)"' in grid
    # The v-else branch is the fallback: a play icon for video, a picture icon otherwise.
    assert 'data-glyph="play"' in grid and 'data-glyph="image"' in grid
    # A deferred image never fires load, so it would hold its slot until the timeout.
    img = grid[grid.index("<img") : grid.index(">", grid.index("<img"))]
    assert 'loading="lazy"' not in img, "the queue is the throttle; native lazy loading would stall it"
    for name in ("isThumbAdmitted,", "thumbFailed,", "settleThumb,", "failThumb,"):
        assert re.search(rf"^\s+{re.escape(name)}$", html, re.M), f"{name} is returned from setup"
