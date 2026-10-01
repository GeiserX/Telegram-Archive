// 4: the demo photo's reactions after two changes the archive cannot keep
// today. Seven hearts dropped to five (two taken back, never zero), and the
// surprised face was taken back and later given again.
window.q4 = (() => {
    const later = (iso, minutes) => {
        const d = new Date(`${String(iso).slice(0, 19)}Z`)
        d.setUTCMinutes(d.getUTCMinutes() + minutes)
        return d.toISOString().slice(0, 19)
    }
    const live = [
        { emoji: '❤️', count: 5, user_ids: [] },
        { emoji: '🔥', count: 1, user_ids: [] },
        { emoji: '😮', count: 1, user_ids: [] },
    ]
    // What today's table holds: the live counts, no tombstone left.
    const today = () => oq.patchMessages((row) => {
        if (!oq.startsWith(row, 'Found this view on the way back')) return
        row.reactions = live
        row.removed_reactions = []
    })
    // What a history table could show: each drop with the count before it.
    const history = () => oq.patchMessages((row) => {
        if (!oq.startsWith(row, 'Found this view on the way back')) return
        row.reactions = live
        row.removed_reactions = [
            { emoji: '❤️', count: 2, removed_at: later(row.date, 11) },
            { emoji: '😮', count: 1, removed_at: later(row.date, 5) },
        ]
    })
    return { today, history, later }
})()
