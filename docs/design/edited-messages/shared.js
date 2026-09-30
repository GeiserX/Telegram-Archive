// Helpers the edited-message mockups share. Loaded before a variant's
// override.js. They read what the viewer already has: the message rows in the
// app's state and the versions endpoint it already calls for the edit history.
// The app would do the same from msg.version_count and loadMessageVersions.
(() => {
    if (window.ev) return
    const app = () => document.querySelector('#app')?._vnode?.component?.setupState
    const hhmm = (iso) => (iso ? iso.slice(11, 16) : '')
    const cache = new Map()

    // The message a row shows, from the app's own list.
    const messageOf = (row) => {
        const id = Number(row?.dataset?.msgId)
        return (app()?.messages || []).find((m) => m.id === id) || null
    }

    // Every text of a message, oldest first, the current one last:
    // [{ text, date, label, current }].
    const versions = async (msg) => {
        const key = `${msg.chat_id}:${msg.id}`
        if (!cache.has(key)) {
            const ref = app()?.selectedChat?.ref
            cache.set(key, fetch(`/api/chats/${encodeURIComponent(ref)}/messages/${msg.id}/versions?limit=100`, { credentials: 'include' })
                .then((r) => (r.ok ? r.json() : []))
                .then((kept) => {
                    const list = kept.slice().reverse().map((v, i) => ({
                        text: v.text || '', date: v.date, current: false,
                        label: i === 0 ? 'Original' : `Edit ${i}`,
                    }))
                    list.push({ text: msg.text || '', date: msg.edit_date, current: true, label: 'Now' })
                    return list
                }))
        }
        return cache.get(key)
    }

    // The viewer's word diff (diffWords in index.html), trimmed: words and the
    // spaces between them, a longest-common-subsequence table.
    const diff = (before, after) => {
        const a = String(before || '').split(/(\s+)/).filter(Boolean)
        const b = String(after || '').split(/(\s+)/).filter(Boolean)
        const cols = b.length + 1
        const t = new Uint16Array((a.length + 1) * cols)
        for (let i = a.length - 1; i >= 0; i--) {
            for (let j = b.length - 1; j >= 0; j--) {
                t[i * cols + j] = a[i] === b[j] ? t[(i + 1) * cols + j + 1] + 1 : Math.max(t[(i + 1) * cols + j], t[i * cols + j + 1])
            }
        }
        const parts = []
        const push = (kind, text) => {
            const last = parts[parts.length - 1]
            if (last && last.kind === kind) last.text += text
            else parts.push({ kind, text })
        }
        let i = 0
        let j = 0
        while (i < a.length && j < b.length) {
            if (a[i] === b[j]) { push('same', a[i]); i++; j++ } else if (t[(i + 1) * cols + j] >= t[i * cols + j + 1]) { push('del', a[i]); i++ } else { push('ins', b[j]); j++ }
        }
        while (i < a.length) push('del', a[i++])
        while (j < b.length) push('ins', b[j++])
        return parts
    }

    // Every value from the page or the versions API goes in as text, never as
    // markup: el() sets textContent. Only the constant icons below are parsed.
    const el = (tag, className, text) => {
        const node = document.createElement(tag)
        if (className) node.className = className
        if (text != null) node.textContent = String(text)
        return node
    }
    const icon = (constantSvg) => {
        const holder = document.createElement('span')
        holder.innerHTML = constantSvg
        return holder.firstElementChild
    }
    // The diff as nodes: unchanged runs as text, added and removed runs as
    // spans (ev-ins, ev-del). `kinds` picks which runs to keep.
    const diffNodes = (before, after, kinds = ['same', 'ins', 'del']) => {
        const out = document.createDocumentFragment()
        for (const part of diff(before, after)) {
            if (!kinds.includes(part.kind)) continue
            out.appendChild(part.kind === 'same' ? document.createTextNode(part.text) : el('span', part.kind === 'ins' ? 'ev-ins' : 'ev-del', part.text))
        }
        return out
    }

    const PENCIL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'

    // Run fn on every edited row now and after every render of the list.
    const eachEdited = (fn) => {
        const run = () => {
            for (const row of document.querySelectorAll('.message-row')) {
                const msg = messageOf(row)
                if (msg && (msg.edit_date || Number(msg.version_count) > 0)) fn(row, msg)
            }
        }
        run()
        new MutationObserver(run).observe(document.documentElement, { childList: true, subtree: true })
    }

    window.ev = { app, messageOf, versions, diff, diffNodes, el, icon, hhmm, eachEdited, PENCIL }
})()
