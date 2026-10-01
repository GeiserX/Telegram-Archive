// 10, B: each chat row's second line shows its last message, the sender
// first in a group, "You" for the account's own, a word for media with no
// text, as Telegram's chat list does. The mockup reads the newest message of
// each chat from the messages endpoint the chat itself uses.
window.mockupBeforeShot = async () => {
    const state = oq.app()
    const chats = state.filteredChats || []
    const rows = [...document.querySelectorAll('.chat-row')].filter((row) => row.querySelector('.chat-row-line'))
    const MEDIA_WORDS = { photo: 'Photo', video: 'Video', voice: 'Voice message', audio: 'Audio', document: 'File', sticker: 'Sticker', video_note: 'Video message', animation: 'GIF' }
    await Promise.all(rows.map(async (row, i) => {
        const chat = chats[i]
        if (!chat) return
        const res = await fetch(`/api/chats/${encodeURIComponent(chat.ref)}/messages?limit=1`, { credentials: 'include' })
        const last = res.ok ? (await res.json())[0] : null
        if (!last) return
        const line = row.querySelector('p.chat-row-sub')
        const isGroup = ['group', 'supergroup'].includes(chat.type)
        let who = null
        if (last.is_outgoing) who = 'You'
        else if (isGroup) who = (last.first_name || last.sender_name || '').split(' ')[0] || null
        let what = (last.text || '').split('\n')[0]
        if (!what && last.raw_data?.poll) what = `Poll: ${last.raw_data.poll.question}`
        if (!what && last.media) what = MEDIA_WORDS[last.media.type] || 'Media'
        if (!what && last.raw_data?.service_type) what = 'Joined the group'
        line.replaceChildren()
        if (who) line.appendChild(oq.el('span', 'mockup-preview-who', `${who}: `))
        line.appendChild(oq.el('span', null, what))
    }))
    await oq.frame()
}
