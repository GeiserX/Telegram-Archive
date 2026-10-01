// 5, B: the filter gains a "Reactions taken back" kind beside the others.
window.mockupBeforeShot = async () => {
    q5.addReactionCard()
    document.querySelector('button[aria-label="Filter What changed"]')?.click()
    await oq.frame()
    const rows = [...document.querySelectorAll('.changes-filter button[role="checkbox"]')]
    const last = rows[rows.length - 1]
    if (last && !document.querySelector('[data-mockup-kind]')) {
        const row = last.cloneNode(true)
        row.dataset.mockupKind = '1'
        row.querySelector('.tg-row-label').textContent = 'Reactions taken back'
        last.after(row)
    }
    document.activeElement?.blur?.()
    await oq.frame()
}
