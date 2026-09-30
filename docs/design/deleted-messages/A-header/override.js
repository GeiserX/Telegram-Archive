// A. Header: "Deleted · 21:59" at the head of a deleted bubble, where the
// sender's name sits. The mockup reads the deletion time from the tooltip the
// viewer already puts on the meta mark ("Deleted in Telegram on September 30,
// 2026 at 08:38. The archive kept it.") and writes the header from it. The app
// would render it from msg.deleted_at in the template instead.
(() => {
    if (window.__dvHeader) return
    window.__dvHeader = true

    const TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

    // "September 30, 2026 at 08:38" -> { day: 'September 30, 2026', time: '08:38' }
    const parseStamp = (text) => {
        const m = /on ([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{2}:\d{2})/.exec(text || '')
        return m ? { day: m[1], time: m[2] } : null
    }
    // The send day, from the time's own tooltip, when the viewer gives one.
    const sendDay = (bubble) => {
        const time = bubble.querySelector('.message-meta > .order-3')
        const m = /([A-Z][a-z]+ \d{1,2}, \d{4})/.exec(time?.getAttribute('title') || '')
        return m ? m[1] : null
    }
    // "Deleted · 08:38" the same day, "Deleted Oct 2 · 08:38" on a later one:
    // a sync can notice a deletion days after the message was sent.
    const label = (bubble, mark) => {
        const title = mark.getAttribute('title') || mark.getAttribute('aria-label') || ''
        const stamp = parseStamp(title)
        const what = title.startsWith('A picture') ? 'Picture deleted' : 'Deleted'
        if (!stamp) return { text: what, title }
        let day = ''
        if (sendDay(bubble) && sendDay(bubble) !== stamp.day) {
            const [month, rest] = stamp.day.split(' ')
            day = ` ${month.slice(0, 3)} ${rest.replace(',', '')}`
            if (!MONTHS.includes(month)) day = ''
        }
        return { text: `${what}${day} · ${stamp.time}`, title }
    }

    const decorate = () => {
        for (const bubble of document.querySelectorAll('.message-bubble.is-deleted')) {
            if (bubble.querySelector(':scope > .dv-deleted-head, :scope > div > .dv-deleted-head')) continue
            const mark = bubble.querySelector('.message-meta .meta-deleted')
            if (!mark) continue
            const { text, title } = label(bubble, mark)
            const head = document.createElement('span')
            head.className = 'dv-deleted-head'
            head.setAttribute('title', title)
            head.innerHTML = `${TRASH}<span>${text}</span>`
            // The sender's line when the bubble has one: the mark sits at its
            // far end, where Telegram puts "admin". Otherwise a line of its own.
            const nameRow = bubble.querySelector(':scope > div.flex.items-baseline')
            if (nameRow) {
                nameRow.appendChild(head)
            } else {
                const row = document.createElement('div')
                row.className = 'dv-deleted-head-row'
                row.appendChild(head)
                bubble.insertBefore(row, bubble.firstChild)
            }
        }
    }
    decorate()
    new MutationObserver(decorate).observe(document.documentElement, { childList: true, subtree: true })
})()
