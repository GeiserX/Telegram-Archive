// G. Edit time: the meta row says when the message was edited, not only that
// it was. The send time comes first, as in every bubble, and "edited 08:57"
// follows it; an edit on a later day shows the day instead ("edited Oct 1").
// The app would render this from msg.edit_date in the template.
(() => {
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    const when = (msg) => {
        if (!msg.edit_date) return ''
        if (msg.edit_date.slice(0, 10) === String(msg.date).slice(0, 10)) return ev.hhmm(msg.edit_date)
        const [, m, d] = msg.edit_date.slice(0, 10).split('-').map(Number)
        return `${MONTHS[m - 1]} ${d}`
    }
    ev.eachEdited((row, msg) => {
        const meta = row.querySelector('.message-meta')
        const mark = meta?.querySelector('.meta-edited') || meta?.querySelector('span.order-2')
        if (!mark || mark.querySelector('.ev-edit-time')) return
        const t = when(msg)
        if (!t) return
        const span = document.createElement('span')
        span.className = 'ev-edit-time'
        span.textContent = t
        mark.appendChild(span)
        mark.classList.add('ev-g')
        const n = Number(msg.version_count) || 0
        if (mark.tagName === 'BUTTON') mark.setAttribute('aria-label', `Edited ${t}${n ? `, ${n} earlier ${n === 1 ? 'version' : 'versions'} kept. Open edit history` : ''}`)
    })
})()
