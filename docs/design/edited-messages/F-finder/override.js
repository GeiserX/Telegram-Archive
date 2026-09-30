// F. Finder: "Edited messages" in the chat's "More actions" menu, beside the
// "Deleted messages" row of the deletions design, opens the chat search in an
// "Edited only" mode: the list narrows to the edited messages and the days
// they belong to. On a phone, where that menu lives in the info panel, the
// "Edited messages" figure of "In the archive" becomes the row that does it.
// The app would pass the mode to the chat's message query (edited_only=1) and
// take the counts from chatStats; the mockup hides the other rows.
(() => {
    const HISTORY = '<svg class="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>'
    const TRASH = '<svg class="w-5 h-5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
    const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>'
    const CHEVRON = '<svg class="tg-row-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>'
    const stats = () => ev.app()?.chatStats || {}
    const isEdited = (row) => { const m = ev.messageOf(row); return !!m && (!!m.edit_date || Number(m.version_count) > 0) }

    // The two finder rows at the end of the menu, under a divider.
    const addMenuRows = () => {
        const menu = document.querySelector('.popover-sheet[aria-label="More actions"]')
        if (!menu || menu.querySelector('.ev-find')) return
        menu.dataset.mockupFrame = '1'
        const s = stats()
        const divider = document.createElement('div')
        divider.className = 'ev-divider'
        menu.appendChild(divider)
        for (const [kind, icon, label, n] of [['deleted', TRASH, 'Deleted messages', s.deleted_messages], ['edited', HISTORY, 'Edited messages', s.edited_messages]]) {
            if (!n) continue
            const b = document.createElement('button')
            b.type = 'button'
            b.className = `info-action ev-find ev-find-${kind}`
            b.setAttribute('aria-label', `${label}: ${n}. Show only these in the chat`)
            b.innerHTML = `${icon}<span class="flex-1 text-left">${label}</span><span class="ev-find-count">${n}</span>`
            b.addEventListener('click', () => { ev.app().chatMenuOpen = false; openSearch(kind) })
            menu.appendChild(b)
        }
    }
    // The phone's entry: the figure in "In the archive" becomes a row button.
    const upgradeInfoRow = () => {
        for (const row of document.querySelectorAll('#info-panel .tg-row')) {
            if (row.dataset.ev || !/(Edited|Deleted) messages/.test(row.textContent)) continue
            row.dataset.ev = '1'
            row.classList.add('ev-info-find')
            row.setAttribute('role', 'button')
            row.setAttribute('tabindex', '0')
            row.insertAdjacentHTML('beforeend', CHEVRON)
        }
    }

    // The search mode: a bar under the header with the two modes as chips.
    const markDays = () => {
        let keep = false
        for (const el of document.querySelectorAll('.message-row, .date-separator')) {
            if (el.classList.contains('date-separator')) {
                el.classList.toggle('ev-keep', keep)
                keep = false
            } else if (isEdited(el)) {
                el.classList.add('ev-edited-row')
                keep = true
            }
        }
    }
    const openSearch = (kind) => {
        document.querySelector('.chat-header button[aria-label="Search in this chat"]')?.click()
        const header = document.querySelector('.chat-header')
        if (header && !document.querySelector('.ev-filter-bar')) {
            const bar = document.createElement('div')
            bar.className = 'ev-filter-bar'
            bar.dataset.mockupFrame = '1'
            bar.setAttribute('role', 'group')
            bar.setAttribute('aria-label', 'Show only')
            bar.innerHTML =
                `<button type="button" class="ev-chip" data-kind="edited" aria-pressed="false">${CHECK}<span>Edited</span></button>` +
                `<button type="button" class="ev-chip" data-kind="deleted" aria-pressed="false">${CHECK}<span>Deleted</span></button>` +
                '<span class="ev-filter-count" aria-live="polite"></span>'
            header.insertAdjacentElement('afterend', bar)
            header.dataset.mockupFrame = '1'
        }
        markDays()
        document.documentElement.classList.toggle('ev-edited-only', kind === 'edited')
        for (const chip of document.querySelectorAll('.ev-chip')) chip.setAttribute('aria-pressed', String(chip.dataset.kind === kind))
        const n = stats().edited_messages || document.querySelectorAll('.ev-edited-row').length
        document.querySelector('.ev-filter-count').textContent = kind === 'edited' ? `${n} edited ${n === 1 ? 'message' : 'messages'}` : ''
    }

    new MutationObserver(() => { addMenuRows(); upgradeInfoRow() }).observe(document.documentElement, { childList: true, subtree: true })

    window.mockupBeforeShot = async () => {
        const wide = matchMedia('(min-width: 768px)').matches
        if (window.__evFinder === 'menu') {
            if (wide) {
                const header = document.querySelector('.chat-header')
                if (header) header.dataset.mockupFrame = '1'
                document.querySelector('.chat-header button[aria-label="More actions"]')?.click()
                await new Promise((r) => setTimeout(r, 200))
                addMenuRows()
            } else {
                document.querySelector('.chat-header button[aria-label="Chat information"]')?.click()
                await new Promise((r) => setTimeout(r, 600))
                upgradeInfoRow()
                document.querySelector('.ev-info-find')?.scrollIntoView({ block: 'center' })
            }
        } else if (window.__evFinder === 'search') {
            openSearch('edited')
            document.activeElement?.blur()
            // The narrowed list starts at its newest end.
            const list = document.querySelector('.messages-scroll')
            if (list) list.scrollTop = 0
        }
        await new Promise((r) => setTimeout(r, 900))
    }
})()
