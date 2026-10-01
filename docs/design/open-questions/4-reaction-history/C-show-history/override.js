// 4, C: the list of reactions taken back also shows partial drops ("2 of 7")
// and a reaction that came back ("back 11:12"), from a history table.
window.mockupBeforeShot = async () => {
    q4.history()
    await oq.frame()
    const row = await oq.anchorOn('Found this view on the way back')
    if (!row) return
    const toggle = row.querySelector('.reaction-removed-toggle')
    if (toggle && toggle.getAttribute('aria-expanded') !== 'true') toggle.click()
    await oq.frame()
    for (const chip of row.querySelectorAll('.reaction-removed')) {
        const emoji = chip.querySelector('.reaction-emoji')?.textContent || ''
        const count = chip.querySelector('.reaction-count')
        if (!count) continue
        const [n, stamp] = count.textContent.split(' · ')
        if (emoji.includes('❤')) count.textContent = `${n} of 7 · ${stamp}`
        if (emoji.includes('😮')) {
            const [h, m] = stamp.split(':').map(Number)
            const back = new Date(Date.UTC(2000, 0, 1, h, m + 4))
            count.textContent = `${n} · ${stamp}, back ${back.toISOString().slice(11, 16)}`
        }
    }
    document.activeElement?.blur?.()
}
