// Helpers the open-question mockups share. Loaded before an option's
// override.js. A mockup puts a demo message in the state a question is about
// (a flag the demo does not have, a reaction count it never had) and lets the
// viewer draw it: patchMessages changes the rows in the app's own list and
// every later answer of the messages endpoint, so the 3 second refresh keeps
// the change instead of putting the demo's values back.
(() => {
    if (window.oq) return
    const app = () => document.querySelector('#app')?._vnode?.component?.setupState
    const patches = []
    const MESSAGES = /\/api\/chats\/[^/]+\/messages(\?|$)/
    const realFetch = window.fetch.bind(window)
    window.fetch = async (...args) => {
        const res = await realFetch(...args)
        const url = String(args[0]?.url || args[0] || '')
        if (!patches.length || !res.ok || !MESSAGES.test(url)) return res
        const rows = await res.clone().json().catch(() => null)
        if (!Array.isArray(rows)) return res
        for (const row of rows) for (const patch of patches) patch(row)
        const headers = new Headers(res.headers)
        headers.delete('content-length')
        headers.delete('content-encoding')
        return new Response(JSON.stringify(rows), { status: res.status, statusText: res.statusText, headers })
    }

    // fn(row) changes a message row in place. It runs on every row, so it
    // picks its own by text.
    const patchMessages = (fn) => {
        patches.push(fn)
        for (const row of app()?.messages || []) fn(row)
    }
    const startsWith = (row, text) => typeof row?.text === 'string' && row.text.startsWith(text)

    // The message row on screen whose text starts with `text`, the last match
    // (a reply quotes its parent's text above its own).
    const rowOf = (text) => {
        const rows = [...document.querySelectorAll('.message-row')]
            .filter((row) => [...row.querySelectorAll('.message-text')].some((t) => t.textContent.trim().startsWith(text)))
        return rows[rows.length - 1] || null
    }
    // Make `text`'s row the one the desktop frame crops around (editedFrame in
    // the rig), in the middle of the list.
    const anchorOn = async (text) => {
        for (const old of document.querySelectorAll('[data-mockup-anchor]')) delete old.dataset.mockupAnchor
        const row = rowOf(text)
        if (!row) return null
        row.dataset.mockupAnchor = '1'
        row.scrollIntoView({ block: 'center' })
        await wait(1400)
        return row
    }
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
    const frame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))

    // Every value goes in as text, never as markup: el() sets textContent.
    // Only the constant icons are parsed.
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
    const UNDO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>'

    window.oq = { app, patchMessages, startsWith, rowOf, anchorOn, wait, frame, el, icon, UNDO }
})()
