// 3, C: the viewer hides the pencil when the flag is empty and no earlier text
// is kept. Both rows lose it, the real edit too. Drawn with the flag set,
// which gives the same bubble.
window.mockupBeforeShot = async () => {
    q3.set(1, 1)
    await oq.anchorOn('Which trailhead did you park at?')
}
