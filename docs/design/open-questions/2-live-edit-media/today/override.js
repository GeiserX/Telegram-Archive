// 2, today: an open chat got the edit frame of a message whose photo was
// replaced. The frame carries the text, the edit time and the formatting, so
// the pencil moves, but the bubble keeps the photo it had until a reload (or
// the next refresh, for a message among the newest 50). The mockup draws that
// state: the earlier photo, from the versions endpoint, in the bubble.
window.mockupBeforeShot = async () => {
    const row = await oq.anchorOn('Found this view on the way back')
    if (!row) return
    const state = oq.app()
    const msg = (state.messages || []).find((m) => oq.startsWith(m, 'Found this view on the way back'))
    const res = await fetch(`/api/chats/${encodeURIComponent(state.selectedChat.ref)}/messages/${msg.id}/versions`, { credentials: 'include' })
    const versions = res.ok ? await res.json() : []
    const earlier = versions.flatMap((v) => v.media || []).find((m) => m.url)
    const photo = [...row.querySelectorAll('.message-bubble img')].sort((a, b) => b.width * b.height - a.width * a.height)[0]
    if (earlier && photo) {
        photo.removeAttribute('srcset')
        photo.src = earlier.url
        await new Promise((resolve) => { photo.onload = resolve; photo.onerror = resolve; setTimeout(resolve, 3000) })
    }
}
