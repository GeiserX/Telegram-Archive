// 3: two rows archived before migration 034, so neither has an edit_hide flag.
// "That first one looks like a postcard" had its edit time moved by a reaction
// (Telegram hid that edit). "Which trailhead did you park at?" was really
// edited before the archive first read it, so no earlier text is kept.
window.q3 = (() => {
    const later = (iso, minutes) => {
        const d = new Date(`${String(iso).slice(0, 19)}Z`)
        d.setUTCMinutes(d.getUTCMinutes() + minutes)
        return d.toISOString().slice(0, 19)
    }
    const set = (postcardHide, trailheadHide) => oq.patchMessages((row) => {
        if (oq.startsWith(row, 'That first one looks like a postcard')) row.edit_hide = postcardHide
        if (oq.startsWith(row, 'Which trailhead did you park at?')) {
            row.edit_date = later(row.date, 3)
            row.edit_hide = trailheadHide
            row.version_count = 0
        }
    })
    return { set }
})()
