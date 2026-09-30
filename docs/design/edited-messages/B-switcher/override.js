// B. Switcher: "‹ 3/3 ›" in the meta row of an edited bubble flips its text
// between the versions in place. On an earlier version a line at the top of
// the bubble says which one it is, and the bubble takes a faint tint, so a
// reader never takes an old text for the current one. The app would load the
// versions on the first press (loadMessageVersions) and keep the index per row.
(() => {
    const CHEV_L = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>'
    const CHEV_R = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>'

    const show = async (row, msg, index) => {
        const list = await ev.versions(msg)
        const i = Math.max(0, Math.min(list.length - 1, index))
        const entry = list[i]
        const bubble = row.querySelector('.message-bubble')
        const body = row.querySelector('.message-body')
        row.dataset.evIndex = String(i)
        bubble.classList.toggle('ev-earlier', !entry.current)
        let alt = body.querySelector('.ev-alt-text')
        if (!alt) {
            alt = document.createElement('span')
            alt.className = 'ev-alt-text whitespace-pre-wrap break-words'
            body.insertBefore(alt, body.firstChild)
        }
        alt.textContent = entry.current ? '' : entry.text
        let label = bubble.querySelector('.ev-version-label')
        if (!label) {
            label = document.createElement('div')
            label.className = 'ev-version-label'
            body.parentElement.insertBefore(label, body)
        }
        label.textContent = entry.current ? '' : `${entry.label === 'Original' ? 'Original, sent' : `${entry.label}, edited`} ${ev.hhmm(entry.date)}`
        const sw = row.querySelector('.ev-switch')
        sw.querySelector('.ev-switch-pos').textContent = `${i + 1}/${list.length}`
        sw.querySelector('.ev-prev').disabled = i === 0
        sw.querySelector('.ev-next').disabled = i === list.length - 1
        sw.querySelector('.ev-switch-pos').setAttribute('aria-label', `Version ${i + 1} of ${list.length}${entry.current ? ', current' : `, ${entry.label.toLowerCase()}`}`)
    }

    ev.eachEdited((row, msg) => {
        const n = Number(msg.version_count) || 0
        const meta = row.querySelector('.message-meta')
        if (!n || !meta || meta.querySelector('.ev-switch')) return
        const total = n + 1
        const sw = document.createElement('span')
        sw.className = 'ev-switch order-2'
        sw.setAttribute('role', 'group')
        sw.setAttribute('aria-label', 'Versions of this message')
        sw.innerHTML = `<button type="button" class="ev-prev hit-40" aria-label="Earlier version">${CHEV_L}</button>` +
            `<span class="ev-switch-pos" aria-live="polite">${total}/${total}</span>` +
            `<button type="button" class="ev-next hit-40" aria-label="Later version" disabled>${CHEV_R}</button>`
        sw.querySelector('.ev-prev').addEventListener('click', (e) => { e.stopPropagation(); show(row, msg, Number(row.dataset.evIndex ?? n) - 1) })
        sw.querySelector('.ev-next').addEventListener('click', (e) => { e.stopPropagation(); show(row, msg, Number(row.dataset.evIndex ?? n) + 1) })
        meta.querySelector('.meta-edited')?.classList.add('ev-hidden')
        meta.insertBefore(sw, meta.firstChild)
        if (typeof window.__evSwitchTo === 'number') show(row, msg, window.__evSwitchTo)
    })
})()
