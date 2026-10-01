// 3, today: both rows have no flag, so both show a pencil with no count.
window.mockupBeforeShot = async () => {
    q3.set(null, null)
    await oq.anchorOn('Which trailhead did you park at?')
}
