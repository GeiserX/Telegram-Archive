"""The shared-media grid admits thumbnails a few at a time, executed.

A tile does not fetch a picture, it asks the server to make one, and a video
costs an ffmpeg run (two at a time server-side, 15 s each). A grid that showed
fifty tiles at once queued dozens of generations and starved everything behind
them: on a cold cache the message list and the gallery's own item request sat
for 7 to 39 seconds and were reset, so the pane read "Failed to load media"
while the tab badges said there were 1,701 files.

These lift the real admission queue out of the template and run it under node,
so they pin the behaviour rather than the source text.
"""

import re

from test_frontend_audit_fixes import INDEX_HTML, _run_node

BLOCK_START = "                const THUMB_CONCURRENCY = 4\n"
BLOCK_END = "                const mediaTabs = [\n"

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const ref = value => ({ value });
const watchers = [];
const watch = (source, fn) => watchers.push({ source, fn });
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
const setItems = list => { mediaGalleryItems.value = list; watchers[0].fn(list) };
"""


def _queue_block(html: str) -> str:
    start = html.index(BLOCK_START)
    return html[start : html.index(BLOCK_END, start)]


def _script(body: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    return "\n".join([PRELUDE, _queue_block(html), body])


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


def test_the_grid_is_wired_to_the_queue() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index("<template v-if=\"mediaGalleryTab === 'photos'\">")
    grid = html[start : html.index("</template>", start)]
    assert 'v-if="item.thumb_url && isThumbAdmitted(item.id)"' in grid, "the tile waits to be admitted"
    assert '@load="settleThumb(item.id)"' in grid and '@error="settleThumb(item.id)"' in grid
    # A deferred image never fires load, so it would hold its slot until the timeout.
    img = grid[grid.index("<img") : grid.index(">", grid.index("<img"))]
    assert 'loading="lazy"' not in img, "the queue is the throttle; native lazy loading would stall it"
    assert re.search(r"isThumbAdmitted,\s*\n\s*settleThumb,", html), "both are returned from setup"
