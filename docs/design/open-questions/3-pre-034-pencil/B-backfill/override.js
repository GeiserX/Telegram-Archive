// 3, B: after a one-off backfill read both messages from Telegram again. The
// reaction-bumped row gets edit_hide 1 and loses its pencil; the real edit
// gets edit_hide 0 and keeps it.
window.mockupBeforeShot = async () => {
    q3.set(1, 0)
    await oq.anchorOn('Which trailhead did you park at?')
}
