// 6, C: the shared period stays, and a line under the chip says where it came
// from, with a button to see all time.
window.mockupBeforeShot = async () => {
    await q6.openBookClub('1')
    const bar = document.querySelector('.deleted-filter-bar')
    if (!bar || document.querySelector('[data-mockup-hint]')) return
    const hint = oq.el('div', 'mockup-period-hint')
    hint.dataset.mockupHint = '1'
    hint.appendChild(oq.el('span', null, 'Last 24 hours, as in What changed for every chat.'))
    hint.appendChild(oq.el('button', 'mockup-period-all', 'Show all time'))
    bar.appendChild(hint)
    await oq.frame()
}
