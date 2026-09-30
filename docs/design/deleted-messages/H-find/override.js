// H. Find: two ways to reach the deleted messages of a chat, on top of C.
// A "2 deleted" chip in the chat header steps through them (up for the one
// before, down for the one after), and a "Deleted only" chip under the chat
// search narrows the chat to them. The mockup counts the deleted bubbles on
// the page; the app would take the count from chatStats.deleted_messages,
// which the info panel already shows, and ask the server for the next one.
(() => {
    if (window.__dvFind) return
    window.__dvFind = true

    const TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    const UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 15l6-6 6 6"/></svg>'
    const DOWN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>'
    const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'

    let current = -1
    // The deleted rows, oldest first (the list is drawn bottom up).
    const deletedRows = () => [...document.querySelectorAll('.message-row:has(> .message-bubble.is-deleted)')]
        .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)

    const step = (by) => {
        const rows = deletedRows()
        if (!rows.length) return
        current = current < 0 ? (by > 0 ? 0 : rows.length - 1) : (current + by + rows.length) % rows.length
        const row = rows[current]
        row.scrollIntoView({ block: 'center', behavior: 'smooth' })
        row.classList.remove('dv-found')
        void row.offsetWidth
        row.classList.add('dv-found')
        render()
    }

    const render = () => {
        const chip = document.querySelector('.dv-find')
        if (!chip) return
        const n = deletedRows().length
        // Write only what changed: every write is a mutation the observer
        // below would answer with another render.
        const set = (el, text) => { if (el.textContent !== text) el.textContent = text }
        if (chip.hidden !== (n === 0)) chip.hidden = n === 0
        set(chip.querySelector('.dv-find-count'), current < 0 ? `${n} deleted` : `${current + 1} of ${n} deleted`)
    }

    const addChip = () => {
        const search = document.querySelector('.chat-header button[aria-label="Search in this chat"]')
        if (!search || document.querySelector('.dv-find')) return
        const chip = document.createElement('div')
        chip.className = 'dv-find'
        chip.setAttribute('role', 'group')
        chip.setAttribute('aria-label', 'Deleted messages in this chat')
        chip.innerHTML =
            `<button type="button" class="dv-find-label" title="Go to the next deleted message">${TRASH}<span class="dv-find-count"></span></button>` +
            `<button type="button" class="dv-find-step" aria-label="Previous deleted message" title="Previous deleted message">${UP}</button>` +
            `<button type="button" class="dv-find-step" aria-label="Next deleted message" title="Next deleted message">${DOWN}</button>`
        const [label, up, down] = chip.querySelectorAll('button')
        label.addEventListener('click', () => step(1))
        up.addEventListener('click', () => step(-1))
        down.addEventListener('click', () => step(1))
        search.parentElement.insertBefore(chip, search)
        render()
    }

    // The search state: the chat search open, and under the header a line of
    // filter chips with "Deleted only" pressed. The mockup narrows the list by
    // hiding the other rows; the app would pass the filter to the search query.
    // Keep the day line of each day that still has a deleted message. The
    // list is drawn bottom up, so in the DOM a day's rows come before its line.
    const markDays = () => {
        let keep = false
        for (const el of document.querySelectorAll('.message-row, .date-separator')) {
            if (el.classList.contains('date-separator')) {
                el.classList.toggle('dv-keep', keep)
                keep = false
            } else if (el.querySelector(':scope > .message-bubble.is-deleted')) {
                keep = true
            }
        }
    }
    const setFilter = (on) => {
        markDays()
        document.documentElement.classList.toggle('dv-deleted-only', on)
        const chip = document.querySelector('.dv-filter-chip')
        if (chip) chip.setAttribute('aria-pressed', String(on))
        const count = document.querySelector('.dv-filter-count')
        if (count) {
            const n = deletedRows().length
            count.textContent = on ? `${n} ${n === 1 ? 'message' : 'messages'}` : ''
        }
    }
    const addFilterBar = () => {
        const header = document.querySelector('.chat-header')
        if (!header || document.querySelector('.dv-filter-bar')) return
        const bar = document.createElement('div')
        bar.className = 'dv-filter-bar'
        bar.innerHTML =
            `<button type="button" class="dv-filter-chip" aria-pressed="false">${CHECK}${TRASH}<span>Deleted only</span></button>` +
            '<span class="dv-filter-count"></span>'
        bar.querySelector('button').addEventListener('click', (e) => setFilter(e.currentTarget.getAttribute('aria-pressed') !== 'true'))
        header.insertAdjacentElement('afterend', bar)
    }

    const tick = () => {
        addChip()
        render()
    }
    tick()
    new MutationObserver(tick).observe(document.documentElement, { childList: true, subtree: true })
    // The search pictures open the search and press the chip once the rig has
    // framed the view (shoot.mjs calls this just before the picture).
    if (window.__dvSearchState) {
        window.mockupBeforeShot = async () => {
            document.querySelector('.chat-header button[aria-label="Search in this chat"]')?.click()
            addFilterBar()
            setFilter(true)
            await new Promise((r) => setTimeout(r, 400))
        }
    }
})()
