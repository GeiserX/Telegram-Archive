// A. Count: the meta row shows a pencil and the number of edits the archive
// saw, where it shows "edited" today. The tooltip says when the last edit was
// and how many earlier texts are kept; a click still opens the edit history.
// The app would render this from msg.version_count and msg.edit_date.
(() => {
    ev.eachEdited((row, msg) => {
        const meta = row.querySelector('.message-meta')
        if (!meta || meta.querySelector('.ev-count')) return
        const n = Number(msg.version_count) || 0
        const target = meta.querySelector('.meta-edited') || meta.querySelector('.order-2')
        if (!target) return
        const mark = document.createElement('span')
        mark.className = 'ev-count'
        mark.appendChild(ev.icon(ev.PENCIL))
        if (n > 0) mark.appendChild(ev.el('span', 'ev-count-n', n))
        target.classList.add('ev-count-host')
        target.appendChild(mark)
        const last = ev.hhmm(msg.edit_date)
        const kept = n > 0 ? ` ${n} earlier ${n === 1 ? 'version' : 'versions'} kept.` : ' The archive did not see the earlier text.'
        target.setAttribute('aria-label', `Edited${n > 1 ? ` ${n} times` : ''}, last at ${last}.${kept}${n > 0 ? ' Open edit history' : ''}`)
        // The tooltip, drawn for the picture where a pointer can hover.
        if (window.__evHover && n > 0 && matchMedia('(hover: hover)').matches) {
            const tip = document.createElement('span')
            tip.className = 'ev-tip'
            tip.setAttribute('role', 'tooltip')
            tip.dataset.mockupFrame = '1'
            tip.append('Last edited at ', ev.el('b', null, last), ev.el('br'),
                `${n} earlier ${n === 1 ? 'version' : 'versions'} kept`, ev.el('br'),
                ev.el('span', 'ev-tip-hint', 'Click to compare them'))
            target.appendChild(tip)
            target.classList.add('ev-hovered')
        }
    })
})()
