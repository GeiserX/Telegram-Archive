// 4, today: five hearts, one fire, one surprised face. Nothing says there
// were seven hearts, or that the face was taken back once.
window.mockupBeforeShot = async () => {
    q4.today()
    await oq.frame()
    await oq.anchorOn('Found this view on the way back')
}
