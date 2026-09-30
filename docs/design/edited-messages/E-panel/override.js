// E. Panel: the edit history opens beside the chat on a wide screen, with the
// chat still readable and the message it belongs to highlighted, and as a
// bottom sheet over the chat on a phone. The versions run down a timeline:
// when each text was sent or edited, and how long after the one before.
// The app would render the labels from the version dates (versionEntries).
(() => {
    const minutes = (hhmm) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m }
    const after = (a, b) => {
        const d = minutes(b) - minutes(a)
        if (d <= 0) return ''
        return d < 60 ? `${d} min later` : `${Math.round(d / 60)} h later`
    }
    const relabel = () => {
        const labels = [...document.querySelectorAll('.versions-drawer .version-list li > .version-label')]
        if (!labels.length || labels[0].dataset.ev) return
        let prev = null
        labels.forEach((label, i) => {
            const text = label.textContent.trim()
            const time = (/(\d{2}:\d{2})\s*$/.exec(text) || [])[1]
            if (!time) return
            label.dataset.ev = '1'
            const li = label.parentElement
            const current = i === labels.length - 1
            li.classList.add(current ? 'ev-now' : i === 0 ? 'ev-first' : 'ev-mid')
            const what = i === 0 ? 'Sent' : current ? `Edit ${i}, current` : `Edit ${i}`
            const gap = prev ? after(prev, time) : ''
            label.innerHTML = `<span class="ev-when">${time}</span><span class="ev-what">${what}${gap ? ` · ${gap}` : ''}</span>`
            prev = time
        })
    }
    const markSource = () => {
        const msg = ev.app()?.versionsMessage
        document.querySelectorAll('.message-row.ev-source').forEach((r) => { if (!msg || Number(r.dataset.msgId) !== msg.id) r.classList.remove('ev-source') })
        if (msg) document.querySelector(`.message-row[data-msg-id="${msg.id}"]`)?.classList.add('ev-source')
    }
    new MutationObserver(() => { relabel(); markSource() }).observe(document.documentElement, { childList: true, subtree: true })
    // On a phone, the chat above the sheet shows the message the sheet is about.
    window.mockupBeforeShot = async () => {
        relabel()
        markSource()
        const row = document.querySelector('.message-row.ev-source')
        const list = document.querySelector('.messages-scroll')
        if (row && list && !matchMedia('(min-width: 768px)').matches) {
            const top = row.getBoundingClientRect().top - list.getBoundingClientRect().top
            list.scrollTop += top - 12
        }
        await new Promise((r) => setTimeout(r, 1400))
    }
})()
