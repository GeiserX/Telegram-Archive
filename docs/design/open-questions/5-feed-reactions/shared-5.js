// 5: a "Reaction taken back" card in What changed, built from the edit card of
// the same photo: the same pill and bubble, with the reaction that went in
// place of the earlier text.
window.q5 = (() => {
    const findCard = (kindWord, who) => [...document.querySelectorAll('.change-entry')].find((card) =>
        card.querySelector('.change-pill')?.textContent.trim().startsWith(kindWord)
        && card.querySelector('.change-bubble > div')?.textContent.trim() === who)
    const addReactionCard = () => {
        const edit = findCard('Edited', 'Kofi Brightwater')
        if (!edit || document.querySelector('[data-mockup-reaction-card]')) return
        const card = edit.cloneNode(true)
        card.dataset.mockupReactionCard = '1'
        const pill = card.querySelector('.change-pill')
        const spans = pill.querySelectorAll(':scope > span')
        spans[0].textContent = 'Reaction taken back'
        const time = spans[spans.length - 1]
        const [h, m] = time.textContent.split(':').map(Number)
        time.textContent = new Date(Date.UTC(2000, 0, 1, h, m + 2)).toISOString().slice(11, 16)
        card.querySelector('.reply-quote')?.remove()
        const meta = card.querySelector('.message-meta')
        meta.replaceChildren()
        const chip = oq.el('span', 'reaction-chip reaction-removed')
        chip.appendChild(oq.el('span', 'reaction-emoji', '😮'))
        chip.appendChild(oq.el('span', 'reaction-count', '1'))
        meta.appendChild(chip)
        meta.appendChild(oq.el('span', 'mockup-taken-back', 'taken back'))
        card.querySelector('.change-show')?.remove()
        edit.parentNode.insertBefore(card, edit)
    }
    return { addReactionCard }
})()
