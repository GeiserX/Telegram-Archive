// 6: What changed for the book club, opened after the reader looked at the
// last 24 hours in What changed for every chat. The book club's four kept
// edits span three days.
window.q6 = (() => {
    const openBookClub = async (period) => {
        const state = oq.app()
        const chat = (state.chats || []).find((c) => c.title === 'Book Club')
        if (!chat) return
        state.openChangesFeed(false, { ref: chat.ref, title: chat.title })
        await oq.wait(300)
        state.setChangesPeriod(period)
        await oq.wait(1200)
    }
    return { openBookClub }
})()
