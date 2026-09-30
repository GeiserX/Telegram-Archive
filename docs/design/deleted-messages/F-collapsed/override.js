// F. Collapsed: a deleted message folds into a one-line pill in its place,
// "Deleted message · 08:38 · Show". Show opens the bubble as it is today, and
// Hide on the opened bubble folds it again. The mockup builds the pill from the
// tooltip the viewer already puts on the meta mark; the app would render it
// from the row (msg.deleted_at, the media type) and keep the open ones in a Set.
(() => {
    if (window.__dvCollapsed) return
    window.__dvCollapsed = true

    const TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    const deletedAt = (title) => (/ at (\d{2}:\d{2})\./.exec(title || '') || [])[1] || ''
    // What was deleted, in the pill's words: a photo, a video, or a message.
    const kind = (bubble) => {
        if (bubble.querySelector('.media-block video')) return 'video'
        if (bubble.querySelector('.media-block img, .album-grid img')) return 'photo'
        return 'message'
    }
    const setOpen = (row, open) => {
        row.classList.toggle('dv-open', open)
        const pill = row.querySelector(':scope > .dv-pill')
        if (pill) pill.setAttribute('aria-expanded', String(open))
    }

    const decorate = () => {
        for (const bubble of document.querySelectorAll('.message-row > .message-bubble.is-deleted')) {
            const row = bubble.parentElement
            if (row.querySelector(':scope > .dv-pill')) continue
            const mark = bubble.querySelector('.message-meta .meta-deleted')
            const title = mark?.getAttribute('title') || mark?.getAttribute('aria-label') || ''
            const sender = bubble.querySelector('.sender-name')?.textContent.trim()
            const sent = bubble.querySelector('.message-meta > .order-3')?.textContent.trim()
            const time = deletedAt(title)
            const what = kind(bubble)

            const pill = document.createElement('button')
            pill.type = 'button'
            pill.className = 'dv-pill ' + (bubble.classList.contains('bubble-out') ? 'bubble-out' : 'bubble-in')
            pill.setAttribute('aria-expanded', 'false')
            pill.setAttribute('aria-label', `${sender ? sender + ', ' : ''}deleted ${what}${sent ? ', sent ' + sent : ''}${time ? ', deleted ' + time : ''}. Show it`)
            pill.title = title
            pill.innerHTML = `${TRASH}<span class="dv-pill-what">Deleted ${what}</span>` +
                (time ? `<span class="dv-pill-dot" aria-hidden="true">·</span><span class="dv-pill-time">${time}</span>` : '') +
                `<span class="dv-pill-dot" aria-hidden="true">·</span><span class="dv-pill-show">Show</span>`
            pill.addEventListener('click', (e) => { e.stopPropagation(); setOpen(row, true) })
            row.insertBefore(pill, bubble)

            // "Hide" on the open bubble, at the far end of its first line.
            const hide = document.createElement('button')
            hide.type = 'button'
            hide.className = 'dv-hide'
            hide.textContent = 'Hide'
            hide.addEventListener('click', (e) => { e.stopPropagation(); setOpen(row, false) })
            const nameRow = bubble.querySelector(':scope > div.flex.items-baseline')
            if (nameRow) {
                nameRow.appendChild(hide)
            } else {
                const line = document.createElement('div')
                line.className = 'dv-hide-row'
                line.appendChild(hide)
                bubble.insertBefore(line, bubble.firstChild)
            }
            if (window.__dvOpenAll) setOpen(row, true)
        }
    }
    decorate()
    new MutationObserver(decorate).observe(document.documentElement, { childList: true, subtree: true })
    // The rig frames the view on the app's own layout, then folds: the fold
    // is html.dv-fold, set just before the picture (shoot.mjs).
    // Folding shortens the list under the view, so the view then centres on
    // the oldest deleted message again, as far as the list lets it.
    window.mockupBeforeShot = async () => {
        document.documentElement.classList.add('dv-fold')
        const rows = document.querySelectorAll('.message-row:has(> .dv-pill)')
        rows[rows.length - 1]?.scrollIntoView({ block: 'center' })
        await new Promise((r) => setTimeout(r, 1400))
    }
})()
