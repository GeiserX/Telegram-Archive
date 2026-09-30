// C. Peek: hovering the "edited" mark for half a second, focusing it, or a
// long press on a phone shows the text before the last edit in a small
// popover, with what that edit changed marked, and a link to the full history.
// The app would load the versions on the first peek (loadMessageVersions).
(() => {
    const open = async (row, msg, meta, target) => {
        if (row.querySelector('.ev-peek')) return
        const list = await ev.versions(msg)
        if (list.length < 2) return
        const before = list[list.length - 2]
        const now = list[list.length - 1]
        const peek = document.createElement('div')
        peek.className = 'ev-peek'
        peek.setAttribute('role', 'tooltip')
        peek.id = `ev-peek-${msg.id}`
        peek.dataset.mockupFrame = '1'
        // Removed words of the previous text are marked; the added ones are in
        // the bubble below, so the popover shows only the old side.
        const oldSide = ev.diff(before.text, now.text)
            .filter((p) => p.kind !== 'ins')
            .map((p) => (p.kind === 'del' ? `<span class="ev-del">${ev.escape(p.text)}</span>` : ev.escape(p.text)))
            .join('')
        peek.innerHTML = `<div class="ev-peek-head">Before the last edit · ${before.label === 'Original' ? 'sent' : 'edited'} ${ev.hhmm(before.date)}</div>` +
            `<div class="ev-peek-text" dir="auto">${oldSide}</div>` +
            `<button type="button" class="ev-peek-all">See all ${list.length} versions</button>`
        peek.querySelector('.ev-peek-all').addEventListener('click', (e) => { e.stopPropagation(); ev.app().toggleMessageVersions(msg) })
        // A pointer gets it beside the mark. A long press gets it above the
        // bubble, so the finger and the popover leave the current text in view.
        if (matchMedia('(hover: hover)').matches) {
            meta.appendChild(peek)
        } else {
            peek.classList.add('ev-peek-above')
            row.querySelector('.message-bubble').appendChild(peek)
        }
        target.setAttribute('aria-describedby', peek.id)
    }
    ev.eachEdited((row, msg) => {
        const meta = row.querySelector('.message-meta')
        const target = meta?.querySelector('.meta-edited')
        if (!target || target.dataset.evPeek) return
        target.dataset.evPeek = '1'
        let timer = null
        target.addEventListener('pointerenter', () => { timer = setTimeout(() => open(row, msg, meta, target), 500) })
        target.addEventListener('pointerleave', () => clearTimeout(timer))
        target.addEventListener('focus', () => open(row, msg, meta, target))
        if (window.__evPeekOpen) open(row, msg, meta, target)
    })
})()
