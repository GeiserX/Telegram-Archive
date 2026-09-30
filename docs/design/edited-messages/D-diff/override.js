// D. Inline diff: a click on "edited" turns the bubble's own text into the
// diff of its last edit, underlined where words were added and struck where
// they were removed, under a line that says which edit it is. A second click
// turns it back. "All versions" on that line opens the full history. The app
// would load the versions on the first press and keep the open rows in a Set.
(() => {
    const setOn = async (row, msg, on) => {
        const bubble = row.querySelector('.message-bubble')
        const body = row.querySelector('.message-body')
        const toggle = row.querySelector('.meta-edited')
        bubble.classList.toggle('ev-diff-on', on)
        toggle.setAttribute('aria-pressed', String(on))
        if (!on) return
        const list = await ev.versions(msg)
        if (list.length < 2) return
        const before = list[list.length - 2]
        const now = list[list.length - 1]
        if (!body.querySelector('.ev-diff-text')) {
            const text = document.createElement('span')
            text.className = 'ev-diff-text whitespace-pre-wrap break-words'
            text.appendChild(ev.diffNodes(before.text, now.text))
            body.insertBefore(text, body.firstChild)
            const head = document.createElement('div')
            head.className = 'ev-diff-head'
            const all = ev.el('button', 'ev-diff-all', `All ${list.length} versions`)
            all.type = 'button'
            all.addEventListener('click', (e) => { e.stopPropagation(); ev.app().toggleMessageVersions(msg) })
            head.append(ev.el('span', null, `What the edit at ${ev.hhmm(now.date)} changed`), all)
            body.parentElement.insertBefore(head, body)
        }
    }
    ev.eachEdited((row, msg) => {
        const toggle = row.querySelector('.message-meta .meta-edited')
        if (!toggle || toggle.dataset.evDiff) return
        toggle.dataset.evDiff = '1'
        toggle.removeAttribute('aria-expanded')
        toggle.setAttribute('aria-pressed', 'false')
        toggle.setAttribute('aria-label', 'Edited. Show what the last edit changed')
        // Capture, so the viewer's own click (open the history) never runs.
        toggle.addEventListener('click', (e) => {
            e.stopImmediatePropagation()
            e.preventDefault()
            setOn(row, msg, toggle.getAttribute('aria-pressed') !== 'true')
        }, true)
        if (window.__evDiffOn) setOn(row, msg, true)
    })
})()
