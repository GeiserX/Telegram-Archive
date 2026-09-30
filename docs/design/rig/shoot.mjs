// Screenshot rig for the viewer mockups.
//
// Logs in to a running viewer seeded with scripts/generate_dummy_db.py, opens
// the same views every time and saves one PNG per view. A CSS file, when given,
// is injected on every page so a mockup can restyle the app without touching it.
// A script file, when given, runs on every page after it loads, for a mockup
// that needs an element the app does not draw. Both take a comma-separated list,
// joined in order, so one mockup can build on another.
//
// Usage:
//   node docs/design/rig/shoot.mjs --css <file[,file]|none> --out <dir> (--port <n> | --base <url>)
//        [--js <file[,file]>] [--theme <id>] [--scheme light|dark] [--only 02,08]
//
// --port reaches a viewer on 127.0.0.1; --base takes a whole address instead
// (http://host:port), and VIEWER_BASE_URL or VIEWER_PORT stand in for both, so
// the rig does not care which port a viewer runs on.
//
// --theme passes any id through ?theme= unchanged, so a theme the app does not
// know yet still reaches it. --scheme sets the colour scheme the page sees
// (prefers-color-scheme) before every navigation. --js runs its file once per
// page, after the CSS, when the page has loaded; the file sees the finished app
// and may watch it for changes (a MutationObserver) to follow later renders. A
// script that changes the layout (folds rows, filters the list) can do it in a
// window.mockupBeforeShot function instead: the rig calls it, and waits for the
// promise it returns, after the view has scrolled and just before the picture,
// so the view is framed on the app's own layout.
//
// Credentials come from VIEWER_USERNAME and VIEWER_PASSWORD, with the demo
// defaults admin and demo-not-a-secret. The share-link view opens the demo's
// share link (DEMO_SHARE_TOKEN in scripts/generate_dummy_db.py), or
// SHARE_TOKEN when set.

import { createRequire } from 'node:module'
import { mkdirSync, readFileSync } from 'node:fs'
import { resolve, join } from 'node:path'

const require = createRequire(import.meta.url)
const PLAYWRIGHT = process.env.PLAYWRIGHT_PATH || 'playwright'
const { chromium } = require(PLAYWRIGHT)

function parseArgs(argv) {
    const args = { css: 'none', js: null, out: null, port: null, base: null, theme: null, scheme: null, only: null }
    for (let i = 0; i < argv.length; i++) {
        const key = argv[i].replace(/^--/, '')
        if (!(key in args)) throw new Error(`unknown option ${argv[i]}`)
        args[key] = argv[++i]
    }
    args.base = args.base || process.env.VIEWER_BASE_URL || null
    args.port = args.port || process.env.VIEWER_PORT || null
    if (!args.out || (!args.port && !args.base)) {
        throw new Error('usage: shoot.mjs --css <file[,file]|none> --out <dir> (--port <n> | --base <url>) [--js <file[,file]>] [--theme <id>] [--scheme light|dark] [--only 02,08]')
    }
    if (args.scheme && !['light', 'dark'].includes(args.scheme)) {
        throw new Error(`--scheme must be light or dark, got ${args.scheme}`)
    }
    return args
}

const args = parseArgs(process.argv.slice(2))
const BASE = (args.base || `http://127.0.0.1:${args.port}`).replace(/\/+$/, '')
const OUT = resolve(args.out)
const readList = (list, glue) => list.split(',').map((f) => readFileSync(resolve(f.trim()), 'utf8')).join(glue)
const CSS = args.css && args.css !== 'none' ? readList(args.css, '\n') : null
const JS = args.js ? readList(args.js, ';\n') : null
const ONLY = args.only ? new Set(args.only.split(',').map((s) => s.trim().padStart(2, '0'))) : null
const USER = process.env.VIEWER_USERNAME || 'admin'
const PASS = process.env.VIEWER_PASSWORD || 'demo-not-a-secret'
const SHARE_TOKEN = process.env.SHARE_TOKEN || 'demo-share-link-not-a-secret'
const GROUP = 'Weekend Hikers'
mkdirSync(OUT, { recursive: true })

const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 }
const MOBILE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }

// Six site files are not whole frames: the main menu and the pages it opens
// in place, the top of the sidebar, and the album row across the chat pane.
// A view that returns one of these boxes is clipped to it. The boxes are in
// CSS pixels, so at deviceScaleFactor 2 the file has twice the pixels of the
// hand-cut 1x crops it replaces.
const LEFT_PANEL = { x: 0, y: 0, width: 720, height: 900 }   // main menu, Theme, Archive status
const STATS_CARD = { x: 0, y: 0, width: 720, height: 420 }   // Statistics
const SIDEBAR_TOP = { x: 0, y: 0, width: 560, height: 260 }  // Archived Chats

// The row holding `text`, across the whole chat pane. The same pick as
// centerOn: a reply quotes its parent, so the row with the most pictures wins.
async function rowAcrossPane(page, text) {
    const rows = page.locator('.message-row').filter({ hasText: text })
    const counts = await rows.evaluateAll((els) => els.map((el) => el.querySelectorAll('img').length))
    const row = await rows.nth(counts.indexOf(Math.max(...counts))).boundingBox()
    const pane = await page.locator('.messages-scroll').first().boundingBox()
    return { x: pane.x, y: row.y, width: pane.width, height: row.height }
}

// --- page helpers ----------------------------------------------------------

async function injectCss(page) {
    if (CSS) {
        if (await claim(page, 'mockupCss')) {
            await page.addStyleTag({ content: CSS }).then((h) => h.evaluate((el) => { el.id = 'mockup-override' }))
        }
    }
    await injectJs(page)
}

// The script runs once per document: a marker on the page stops a second run.
async function injectJs(page) {
    if (!JS) return
    if (await claim(page, 'mockupScript')) {
        await page.addScriptTag({ content: JS }).then((h) => h.evaluate((el) => { el.id = 'mockup-script' }))
    }
}

// Two injections can overlap (one after domcontentloaded, one after goto). The
// page's own JavaScript is single-threaded, so checking and setting the marker
// in one evaluate lets exactly one of them win for each document.
async function claim(page, key) {
    return page.evaluate((k) => {
        const root = document.documentElement
        if (root.dataset[k]) return false
        root.dataset[k] = '1'
        return true
    }, key).catch(() => false)
}

async function settle(page) {
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.evaluate(async () => {
        const imgs = Array.from(document.images).filter((img) => img.offsetParent !== null)
        await Promise.all(imgs.map((img) => img.complete ? null : new Promise((r) => {
            img.addEventListener('load', r, { once: true })
            img.addEventListener('error', r, { once: true })
            setTimeout(r, 5000)
        })))
        await document.fonts.ready
    })
    await injectCss(page)
    await page.waitForTimeout(300)
}

// A pointer left where the last click was draws that control's hover state,
// so a row looks selected. Before each picture it moves to a spot with
// nothing clickable under it: the right edge of the chat pane first.
async function parkPointer(page) {
    const { width, height } = page.viewportSize()
    const spots = [[width - 4, Math.round(height / 2)], [width - 4, height - 4], [Math.round(width / 2), height - 4], [0, 0]]
    for (const [x, y] of spots) {
        const free = await page.evaluate(([px, py]) => {
            const el = document.elementFromPoint(px, py)
            return !!el && !el.closest('button, a, input, label, [role="button"], [role="option"], [tabindex], .cursor-pointer, .chat-row, .message-row')
        }, [x, y])
        if (free) {
            await page.mouse.move(x, y)
            return
        }
    }
    await page.mouse.move(0, 0)
}

// A view may return what to keep of the page: a rectangle (a clip box such as
// LEFT_PANEL), or a function run in the page just before the picture,
// after any mockupBeforeShot, that returns the rectangle (a crop that follows
// the layout, as editedFrame).
async function shot(page, name, clip = null) {
    if (JS) await page.evaluate(() => window.mockupBeforeShot?.())
    // A view that pictures a hover (the edit peek) keeps the pointer where it is.
    if (!page.keepPointer) await parkPointer(page)
    await settle(page)
    const file = join(OUT, `${name}.png`)
    const box = typeof clip === 'function' ? await page.evaluate(clip) : clip
    await page.screenshot({ path: file, animations: 'disabled', caret: 'hide', ...(box ? { clip: box } : {}) })
    console.log(`wrote ${file}`)
}

async function newPage(context) {
    const page = await context.newPage()
    if (args.scheme) await page.emulateMedia({ colorScheme: args.scheme })
    page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) page.waitForLoadState('domcontentloaded').then(() => injectCss(page)).catch(() => {})
    })
    return page
}

async function open(page) {
    const query = args.theme ? `/?theme=${encodeURIComponent(args.theme)}` : '/'
    await page.goto(BASE + query, { waitUntil: 'load' })
    await injectCss(page)
    await page.getByText(GROUP, { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
    await settle(page)
}

async function openGroup(page) {
    await page.locator('[data-chat-list] h3, .cursor-pointer h3').filter({ hasText: GROUP }).first().click()
    await page.locator('.message-row').first().waitFor({ state: 'visible', timeout: 20000 })
    await settle(page)
}

// Scroll the message list so the row holding `text` sits in the middle, or at
// the top of the list with `block: 'start'` (for rows taller than half a screen).
async function centerOn(page, text, block = 'center') {
    const rows = page.locator('.message-row').filter({ hasText: text })
    await rows.first().waitFor({ state: 'attached', timeout: 15000 })
    // A reply quotes its parent's text, so several rows can match. The message
    // itself is the match holding the most pictures (an album beats its reply).
    const counts = await rows.evaluateAll((els) => els.map((el) => el.querySelectorAll('img').length))
    const row = rows.nth(counts.indexOf(Math.max(...counts)))
    await row.evaluate((el, b) => {
        el.scrollIntoView({ block: b })
        if (b === 'start') el.closest('.messages-scroll')?.scrollBy(0, -24)
    }, block)
    // The floating day fades 1.2 s after the last scroll; a picture of a chat
    // at rest shows it gone, as a reader sees it.
    await page.waitForTimeout(1400)
}

// Whether a bubble's time sits under the jump-to-latest button.
async function timeUnderScrollButton(page) {
    return page.evaluate(() => {
        const button = document.querySelector('.scroll-to-bottom-btn')
        if (!button) return false
        const b = button.getBoundingClientRect()
        return [...document.querySelectorAll('.message-meta')].some((meta) => {
            const m = meta.getBoundingClientRect()
            return m.right > b.left - 4 && m.left < b.right + 4 && m.bottom > b.top - 4 && m.top < b.bottom + 4
        })
    })
}

// Scrolled up, the list shows the jump-to-latest button over its bottom right
// corner, as Telegram does. For a picture, move the list a little (up to 160px
// back towards the album) until no bubble's time sits under the button.
async function clearOfScrollButton(page) {
    for (let step = 0; step <= 20; step++) {
        if (!(await timeUnderScrollButton(page))) break
        await page.locator('.messages-scroll').first().evaluate((el) => el.scrollBy(0, -8))
        await page.waitForTimeout(50)
    }
    await page.waitForTimeout(1400)
}

// Where the rows of the message list stand against its top edge, in pixels
// from that edge: the row or day separator crossing it (`cut`), the one after
// it, and the last one wholly above it.
function readTopEdge(list) {
    const edge = list.getBoundingClientRect().top
    const items = [...list.querySelectorAll(':scope > .message-row, :scope > .date-separator, :scope > [data-msg-id]')]
        .map((el) => {
            const r = el.getBoundingClientRect()
            return { top: r.top - edge, bottom: r.bottom - edge, separator: el.classList.contains('date-separator') }
        })
        .filter((r) => r.bottom > r.top)
        .sort((a, b) => a.top - b.top)
    const i = items.findIndex((r) => r.top < -0.5 && r.bottom > 0.5)
    return {
        scrollTop: list.scrollTop,
        cut: i >= 0 ? items[i] : null,
        next: i >= 0 ? items[i + 1] || null : null,
        above: items.filter((r) => r.bottom <= 0.5).pop() || null,
    }
}

// A picture starts on a whole element, never on half a bubble. Run after
// every other scroll of a view: the row or day separator crossing the top of
// the list is shown whole with 8px of wallpaper above it when it is a day
// separator or short, and otherwise scrolled out, leaving 8px above the next
// one. Where hiding it would put a time under the jump-to-latest button, or
// the list cannot scroll that far, the element is revealed instead, and while
// a time still sits under the button the element above comes into view whole.
async function frameTopEdge(page) {
    const list = page.locator('.messages-scroll').first()
    if (!(await list.isVisible().catch(() => false))) return
    const scrollTo = async (top) => {
        await list.evaluate((el, t) => { el.scrollTop = t }, top)
        await page.waitForTimeout(150)
    }
    let revealOnly = false
    for (let pass = 0; pass < 8; pass++) {
        const edge = await list.evaluate(readTopEdge)
        if (edge.cut) {
            const reveal = edge.scrollTop + edge.cut.top - 8
            const short = edge.cut.separator || edge.cut.bottom - edge.cut.top < 80
            if (short || revealOnly) {
                await scrollTo(reveal)
                continue
            }
            const hide = edge.scrollTop + (edge.next ? Math.max(edge.cut.bottom, edge.next.top - 8) : edge.cut.bottom)
            await scrollTo(hide)
            const after = await list.evaluate(readTopEdge)
            if (after.cut || await timeUnderScrollButton(page)) {
                revealOnly = true
                await scrollTo(reveal)
            }
            continue
        }
        if (!(await timeUnderScrollButton(page)) || !edge.above) break
        revealOnly = true
        await scrollTo(edge.scrollTop + edge.above.top - 8)
    }
    const final = await list.evaluate(readTopEdge)
    if (final.cut) console.error(`warning: an element still crosses the top of the list (${Math.round(final.cut.top)}px)`)
    if (await timeUnderScrollButton(page)) console.error('warning: a bubble time sits under the jump-to-latest button')
    // The floating day fades 1.2 s after the last scroll.
    await page.waitForTimeout(1400)
}

// A headless browser answers "denied" for notifications whatever it is
// granted, so the main menu would read "Blocked in the browser settings" in
// every picture. The pages see the answer of a browser that said yes.
async function allowNotifications(context) {
    await context.addInitScript(() => {
        try { Object.defineProperty(Notification, 'permission', { get: () => 'granted' }) } catch (e) { /* ignore */ }
    })
}

async function newContext(browser, profile) {
    const context = await browser.newContext({ ...profile, reducedMotion: 'reduce', timezoneId: 'UTC', locale: 'en-US' })
    await allowNotifications(context)
    // The transcription nudge only shows on a fresh browser with no server set;
    // dismissing it keeps every view on the chat itself.
    await context.addInitScript(() => {
        try { localStorage.setItem('transcriptNudgeDismissed', '1') } catch (e) { /* ignore */ }
    })
    const res = await context.request.post(`${BASE}/api/login`, { data: { username: USER, password: PASS } })
    if (!res.ok()) throw new Error(`login failed: HTTP ${res.status()}`)
    return context
}

// --- the archive's own views ----------------------------------------------
//
// What makes this an archive and not a Telegram client: the changes feed, the
// statistics, an edit history, earlier profile photos, the export, a kept
// deletion, the archived chats and the date picker. Each runs on both widths.

async function openChangesFeed(page) {
    await open(page)
    await page.getByRole('button', { name: 'What changed', exact: true }).click()
    await page.locator('#changes-feed-title').waitFor({ state: 'visible', timeout: 10000 })
    // Every period, so the whole demo feed shows.
    await page.getByRole('button', { name: 'Filter What changed' }).click()
    await page.getByRole('radio', { name: 'All time' }).click()
    await page.keyboard.press('Escape')
    // Escape hands focus back to the filter button; its ring is not part of the view.
    await page.evaluate(() => document.activeElement?.blur())
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(400)
}

// What changed narrowed to the group: from the chat's More actions menu on a
// wide screen, from its info panel on a phone, where that menu is hidden.
async function openChangesForChat(page, fromInfo = false) {
    await open(page)
    await openGroup(page)
    if (fromInfo) {
        await page.getByRole('button', { name: 'Chat information' }).click()
        await page.locator('.tg-row').filter({ hasText: 'What changed in this chat' }).first().click()
    } else {
        await page.getByRole('button', { name: 'More actions' }).click()
        await page.locator('.popover-sheet .info-action').filter({ hasText: 'What changed in this chat' }).click()
    }
    await page.locator('#changes-feed-title').waitFor({ state: 'visible', timeout: 10000 })
    await page.getByRole('button', { name: 'Filter What changed' }).click()
    await page.getByRole('radio', { name: 'All time' }).click()
    await page.keyboard.press('Escape')
    await page.evaluate(() => document.activeElement?.blur())
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(400)
}

// The main menu, and one of the pages it opens in place (Statistics, Theme,
// Archive status).
async function openMainMenu(page, row = null) {
    await open(page)
    await page.getByRole('button', { name: 'Main menu' }).click()
    await page.locator('.main-menu').waitFor({ state: 'visible', timeout: 10000 })
    if (row) {
        await page.locator('.main-menu .tg-row').filter({ hasText: row }).first().click()
        await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    }
    await page.mouse.move(0, 0)
    await page.waitForTimeout(400)
}

async function openStatusPanel(page) {
    await openMainMenu(page, 'Statistics')
    await page.getByRole('group', { name: 'Statistics' }).last().waitFor({ state: 'visible', timeout: 10000 })
}

// Media the archive does not show: one of each reason, in one private chat.
async function openMediaMissing(page) {
    await open(page)
    await page.locator('.cursor-pointer h3').filter({ hasText: 'Orson Quill' }).first().click()
    await page.locator('.media-placeholder').first().waitFor({ state: 'visible', timeout: 20000 })
    await centerOn(page, 'The panorama from the top', 'start')
    await frameTopEdge(page)
}

// A forum's topics in the sidebar, with one topic open.
async function openTopics(page) {
    await open(page)
    await page.locator('.cursor-pointer h3').filter({ hasText: 'Maker Space' }).first().click()
    await page.locator('.chat-row').filter({ hasText: 'Woodworking' }).first().waitFor({ state: 'visible', timeout: 15000 })
    await page.locator('.chat-row').filter({ hasText: 'Events' }).first().click()
    await page.locator('.message-row').first().waitFor({ state: 'visible', timeout: 20000 })
    await settle(page)
    await frameTopEdge(page)
}

async function openAdmin(page) {
    await openMainMenu(page, 'Admin settings')
    await page.locator('#admin-title').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)
}

// The transcript in each state: two versions, no speech, and a failure.
async function openTranscripts(page) {
    await open(page)
    await page.locator('.cursor-pointer h3').filter({ hasText: 'Juniper Vale' }).first().click()
    await page.locator('.message-row').first().waitFor({ state: 'visible', timeout: 20000 })
    const buttons = page.locator('.message-row .transcript-btn:not(.transcript-btn--overlay)')
    for (let i = 0; i < await buttons.count(); i++) {
        const button = buttons.nth(i)
        if ((await button.getAttribute('aria-expanded')) !== 'true') await button.click()
        await page.waitForTimeout(200)
    }
    const dismiss = page.getByRole('button', { name: 'Dismiss' })
    if (await dismiss.count()) await dismiss.first().click()
    await page.mouse.move(0, 0)
    await centerOn(page, 'The trail map is on the fridge', 'center')
    await frameTopEdge(page)
}

// The edit history of the demo's edited message: a panel beside the chat on a
// wide screen, a bottom sheet on a phone. The chat narrows when the panel opens,
// so its top edge is framed again after.
async function openEditHistory(page) {
    await open(page)
    await openGroup(page)
    const edited = page.locator('.message-row').filter({ hasText: EDITED_TEXT }).last().locator('.meta-edited')
    await edited.waitFor({ state: 'attached', timeout: 15000 })
    await edited.evaluate((el) => el.closest('.message-row').scrollIntoView({ block: 'center' }))
    await frameTopEdge(page)
    await edited.click()
    await page.locator('#versions-title').waitFor({ state: 'visible', timeout: 10000 })
    await page.locator('#versions-panel .version-list').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)
    await frameTopEdge(page)
}

// The peek: the pointer resting on the pencil of the demo's edited message.
// The picture is cropped to the message column around it (peekFrame).
async function openEditPeek(page) {
    await openEdited(page)
    const mark = page.locator('[data-mockup-anchor] .meta-edited')
    await mark.hover()
    await page.locator('.edit-peek.is-placed').waitFor({ state: 'visible', timeout: 10000 })
    await page.locator('.edit-peek .edit-peek-text').waitFor({ state: 'visible', timeout: 10000 })
    page.keepPointer = true
}

// The chat's "More actions" menu with "Deleted messages" and "Edited messages",
// each with its count, over the edited message.
async function openEditedMenu(page) {
    await openEdited(page)
    await page.getByRole('button', { name: 'More actions' }).click()
    await page.locator('.popover-sheet').filter({ hasText: 'Edited messages' }).waitFor({ state: 'visible', timeout: 10000 })
    await page.mouse.move(0, 0)
    await page.waitForTimeout(300)
}

async function openAvatarHistory(page) {
    await open(page)
    await openGroup(page)
    await page.getByRole('button', { name: 'Chat information' }).click()
    await page.locator('[data-testid="previous-avatars"]').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(300)
    await frameTopEdge(page)
}

// The earlier photos paged in the lightbox, opened from the info panel.
async function openAvatarLightbox(page) {
    await openAvatarHistory(page)
    await page.locator('[data-testid="previous-avatars"] .earlier-photo').first().click()
    await page.locator('[role="dialog"][aria-modal="true"] img').first().waitFor({ state: 'visible', timeout: 10000 })
    await page.mouse.move(0, 0)
    await page.waitForTimeout(400)
}

async function openExportDialog(page, mobile) {
    await open(page)
    await openGroup(page)
    await frameTopEdge(page)
    if (mobile) {
        // On a phone the export action lives in the info panel.
        await page.getByRole('button', { name: 'Chat information' }).click()
        await page.locator('#info-panel').waitFor({ state: 'visible', timeout: 10000 })
        await page.locator('#info-panel .info-action').filter({ hasText: 'Export chat' }).click()
    } else {
        // On a wide screen it sits in the header's "More actions" menu.
        await page.getByRole('button', { name: 'More actions' }).click()
        await page.getByRole('button', { name: 'Export chat' }).click()
    }
    await page.locator('#export-modal-title').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(300)
}

// Both kept deletions in view, at the end of the chat. A deleted message is
// folded to one line until it is opened: the deleted photo is opened (Show)
// and the deleted text above it stays folded, so the picture has one of each.
async function openDeleted(page) {
    await open(page)
    await openGroup(page)
    const pill = page.locator('.deleted-pill').filter({ hasText: 'Deleted photo' }).first()
    await pill.waitFor({ state: 'attached', timeout: 15000 })
    if (CSS || JS) {
        // A design mockup styles the open bubble, so every native fold is
        // opened first; a mockup that folds (F) does it again in its hook.
        for (let guard = 0; guard < 20; guard++) {
            const next = page.locator('.deleted-pill').first()
            if (!(await next.count())) break
            await next.evaluate((el) => el.scrollIntoView({ block: 'center' }))
            await next.click()
        }
    } else {
        await pill.evaluate((el) => el.scrollIntoView({ block: 'center' }))
        await pill.click()
    }
    await page.evaluate(() => document.activeElement?.blur())
    await page.locator('.messages-scroll').first().evaluate((el) => { el.scrollTop = 0 })
    await page.waitForTimeout(1400)
    await frameTopEdge(page)
}

// The chat's "More actions" menu, with "Deleted messages" and its count, over
// the same end of the chat as the deletion picture.
async function openDeletedMenu(page) {
    await openDeleted(page)
    await page.getByRole('button', { name: 'More actions' }).click()
    await page.locator('.popover-sheet').filter({ hasText: 'Deleted messages' }).waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(300)
}

// The chat search in its "Deleted only" mode, opened from that menu item.
async function openDeletedOnly(page) {
    await openDeletedMenu(page)
    await page.locator('.popover-sheet .info-action').filter({ hasText: 'Deleted messages' }).click()
    await page.locator('.deleted-filter-bar').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.locator('.message-row').first().waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(1400)
}

async function openArchivedChats(page) {
    await open(page)
    await page.locator('.cursor-pointer h3').filter({ hasText: 'Archived Chats' }).first().click()
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(500)
}

async function openDatePicker(page) {
    await open(page)
    await openGroup(page)
    const separator = page.locator('.date-separator button').first()
    await separator.evaluate((el) => el.scrollIntoView({ block: 'center' }))
    await frameTopEdge(page)
    await separator.click()
    await page.locator('.flatpickr-calendar').first().waitFor({ state: 'visible', timeout: 10000 })
    // The click that opened it would otherwise leave a hovered day under the pointer.
    await page.mouse.move(0, 0)
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    await page.waitForTimeout(400)
}

// The edited message in its chat: the reply with two earlier versions,
// in the middle of the list. The row is marked data-mockup-anchor, so a
// mockup script can find it and the desktop frame can crop around it.
const EDITED_TEXT = 'It fills up by 8'

async function openEdited(page) {
    await open(page)
    await openGroup(page)
    await centerOn(page, EDITED_TEXT)
    await frameTopEdge(page)
    await page.locator('.message-row').filter({ hasText: EDITED_TEXT }).last()
        .evaluate((el) => { el.dataset.mockupAnchor = '1' })
}

// The demo's photo with live reactions and one taken back, its list opened
// with the chip after the live ones. The row is anchored for editedFrame.
const REMOVED_TEXT = 'Found this view on the way back'

async function openRemovedReactions(page) {
    await open(page)
    await openGroup(page)
    await centerOn(page, REMOVED_TEXT)
    await frameTopEdge(page)
    const row = page.locator('.message-row').filter({ hasText: REMOVED_TEXT }).last()
    await row.evaluate((el) => { el.dataset.mockupAnchor = '1' })
    await row.locator('.reaction-removed-toggle').click()
    await row.locator('.reaction-removed-list').waitFor({ state: 'visible', timeout: 10000 })
    await page.evaluate(() => document.activeElement?.blur())
}

// The demo's photo with no caption whose only reaction was taken back, three
// weeks up the chat. It keeps a bubble frame to hold the row, as it did while
// the reaction stood. Older pages load as the list scrolls to its top.
function findRemovedOnlyPicture() {
    const rows = [...document.querySelectorAll('.message-row')]
    return rows.find((row) => row.querySelector('img')
        && row.querySelector('.reaction-removed-toggle')
        && !row.querySelector('.reaction-chip:not(.reaction-removed-toggle):not(.reaction-removed)'))
}

async function openRemovedReactionsPicture(page) {
    await open(page)
    await openGroup(page)
    for (let i = 0; i < 20; i++) {
        const found = await page.evaluate(`(${findRemovedOnlyPicture})() ? true : false`)
        if (found) break
        // The list is column-reverse: 0 is the newest edge, and the older
        // sentinel sits at -scrollHeight, so scroll there to load another page.
        await page.evaluate(() => {
            const list = document.querySelector('.messages-scroll')
            list?.scrollTo(0, -list.scrollHeight)
        })
        await page.waitForTimeout(800)
    }
    await page.evaluate(`(() => {
        const row = (${findRemovedOnlyPicture})()
        row.dataset.mockupAnchor = '1'
        row.scrollIntoView({ block: 'center' })
    })()`)
    await page.waitForTimeout(1400)
    const row = page.locator('.message-row[data-mockup-anchor="1"]')
    await row.locator('.reaction-removed-toggle').click()
    await row.locator('.reaction-removed-list').waitFor({ state: 'visible', timeout: 10000 })
    await page.evaluate(() => document.activeElement?.blur())
}

// The message column around the anchored row: from 16px left of the avatars
// to the pane's right edge, where outgoing bubbles end, and at least 180px
// above and below the row. It grows to take in anything a mockup marks with
// data-mockup-frame (a popover, a menu, a search bar).
function editedFrame() {
    const list = document.querySelector('.messages-scroll')
    const row = document.querySelector('[data-mockup-anchor]') || list
    const pane = list.getBoundingClientRect()
    const r = row.getBoundingClientRect()
    let top = r.top - 180
    let bottom = r.bottom + 180
    const gutter = row.querySelector('.message-avatar-gutter, .message-bubble')
    let left = (gutter ? gutter.getBoundingClientRect().left : pane.left) - 16
    let right = pane.right
    for (const el of document.querySelectorAll('[data-mockup-frame]')) {
        const f = el.getBoundingClientRect()
        if (!f.width || !f.height) continue
        top = Math.min(top, f.top - 16)
        bottom = Math.max(bottom, f.bottom + 16)
        left = Math.min(left, f.left - 16)
        right = Math.max(right, f.right + 16)
    }
    // Never past the chat column: the sidebar is not part of the picture.
    top = Math.max(0, top)
    bottom = Math.min(window.innerHeight, bottom)
    left = Math.max(pane.left, left)
    right = Math.min(window.innerWidth, right)
    return { x: Math.round(left), y: Math.round(top), width: Math.round(right - left), height: Math.round(bottom - top) }
}

// The peek's picture: the message column from the peek down to the row after
// the edited one, with both edges on whole elements (8px of wallpaper beyond a
// bubble or a day separator), never on half a bubble.
function peekFrame() {
    const list = document.querySelector('.messages-scroll')
    const pane = list.getBoundingClientRect()
    const row = document.querySelector('[data-mockup-anchor]')
    const peek = document.querySelector('.edit-peek')
    const r = row.getBoundingClientRect()
    const p = peek.getBoundingClientRect()
    const items = [...list.querySelectorAll(':scope > .message-row, :scope > .date-separator')]
        .map((el) => el.getBoundingClientRect())
        .filter((b) => b.height > 0)
    let top = Math.min(r.top, p.top) - 16
    let bottom = r.bottom + 8
    const next = items.filter((b) => b.top >= r.bottom - 1).sort((a, b) => a.top - b.top)[0]
    if (next) bottom = next.bottom + 8
    const crossingTop = items.find((b) => b.top < top && b.bottom > top)
    if (crossingTop) top = crossingTop.top - 8
    top = Math.max(pane.top, top)
    bottom = Math.min(pane.bottom, bottom)
    const gutter = row.querySelector('.message-avatar-gutter, .message-bubble')
    const left = Math.max(pane.left, (gutter ? gutter.getBoundingClientRect().left : pane.left) - 16)
    return { x: Math.round(left), y: Math.round(top), width: Math.round(pane.right - left), height: Math.round(bottom - top) }
}

// --- views -----------------------------------------------------------------

const desktopViews = {
    '01-chat-list-desktop': async (page) => {
        await open(page)
    },
    '02-chat-desktop': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'A few shots from the ridge loop', 'start')
        await frameTopEdge(page)
    },
    '03-chat-replies': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'The north lot. It fills up by 8')
        await frameTopEdge(page)
    },
    '06-search': async (page) => {
        await open(page)
        await page.getByRole('combobox', { name: 'Search chats and messages' }).fill('trail')
        await page.locator('#search-results').waitFor({ state: 'visible', timeout: 15000 })
        await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
        await page.waitForTimeout(800)
    },
    '07-media-gallery': async (page) => {
        await open(page)
        await openGroup(page)
        await page.locator('button[title="Shared Media Gallery"]').click()
        await page.waitForTimeout(500)
        await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    },
    '08-transcript': async (page) => {
        await open(page)
        await openGroup(page)
        const btn = page.locator('.message-row .transcript-btn').first()
        await btn.waitFor({ state: 'attached', timeout: 15000 })
        await btn.evaluate((el) => el.scrollIntoView({ block: 'center' }))
        if ((await btn.getAttribute('aria-expanded')) !== 'true') await btn.click()
        await page.locator('.message-row [id^="transcript-"]').first().waitFor({ state: 'visible', timeout: 10000 })
        await btn.evaluate((el) => el.closest('.message-row').scrollIntoView({ block: 'center' }))
        await page.waitForTimeout(400)
        await frameTopEdge(page)
    },
    '09-theme-picker': async (page) => { await openMainMenu(page, 'Theme'); return LEFT_PANEL },
    '10-changes-feed': (page) => openChangesFeed(page),
    '11-status-panel': async (page) => { await openStatusPanel(page); return STATS_CARD },
    '12-edit-history': (page) => openEditHistory(page),
    '13-avatar-history': (page) => openAvatarHistory(page),
    '14-export-dialog': (page) => openExportDialog(page, false),
    '15-deleted': (page) => openDeleted(page),
    '16-archived-chats': async (page) => { await openArchivedChats(page); return SIDEBAR_TOP },
    '17-date-picker': (page) => openDatePicker(page),
    '18-archive-status': async (page) => { await openMainMenu(page, 'Archive status'); return LEFT_PANEL },
    '19-main-menu': async (page) => { await openMainMenu(page); return LEFT_PANEL },
    '20-media-missing': (page) => openMediaMissing(page),
    '21-topics': (page) => openTopics(page),
    '22-admin': (page) => openAdmin(page),
    '24-transcripts': (page) => openTranscripts(page),
    '25-avatar-lightbox': (page) => openAvatarLightbox(page),
    '26-deleted-menu': (page) => openDeletedMenu(page),
    '27-deleted-only': (page) => openDeletedOnly(page),
    '28-chat-album': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'A few shots from the ridge loop', 'center')
        await frameTopEdge(page)
        return rowAcrossPane(page, 'A few shots from the ridge loop')
    },
    '31-edited': async (page) => {
        await openEdited(page)
        return editedFrame
    },
    '32-edit-peek': async (page) => {
        await openEditPeek(page)
        return peekFrame
    },
    '33-edited-menu': (page) => openEditedMenu(page),
    '34-changes-chat': (page) => openChangesForChat(page),
    '35-removed-reactions': async (page) => {
        await openRemovedReactions(page)
        return editedFrame
    },
    '36-removed-reactions-picture': async (page) => {
        await openRemovedReactionsPicture(page)
        return editedFrame
    },
}

const mobileViews = {
    '04-chat-mobile': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'A few shots from the ridge loop', 'start')
        await clearOfScrollButton(page)
        await frameTopEdge(page)
    },
    '05-chat-list-mobile': async (page) => {
        await open(page)
    },
    '10-changes-feed-mobile': (page) => openChangesFeed(page),
    '11-status-panel-mobile': (page) => openStatusPanel(page),
    '12-edit-history-mobile': (page) => openEditHistory(page),
    '13-avatar-history-mobile': (page) => openAvatarHistory(page),
    '14-export-dialog-mobile': (page) => openExportDialog(page, true),
    '15-deleted-mobile': (page) => openDeleted(page),
    '16-archived-chats-mobile': (page) => openArchivedChats(page),
    '17-date-picker-mobile': (page) => openDatePicker(page),
    '19-main-menu-mobile': (page) => openMainMenu(page),
    '20-media-missing-mobile': (page) => openMediaMissing(page),
    '25-avatar-lightbox-mobile': (page) => openAvatarLightbox(page),
    '31-edited-mobile': (page) => openEdited(page),
    '34-changes-chat-mobile': (page) => openChangesForChat(page, true),
    '35-removed-reactions-mobile': (page) => openRemovedReactions(page),
    '36-removed-reactions-picture-mobile': (page) => openRemovedReactionsPicture(page),
}

// A share-link session: its own browser, opened through the link, so the
// menu names it "Shared link" and downloads are off.
const shareViews = {
    '23-share-view': async (page) => {
        await page.goto(`${BASE}/?theme=${encodeURIComponent(args.theme || 'telegram')}#token=${encodeURIComponent(SHARE_TOKEN)}`, { waitUntil: 'load' })
        await page.getByText(GROUP, { exact: true }).first().waitFor({ state: 'visible', timeout: 30000 })
        await openGroup(page)
        await centerOn(page, 'Found this view on the way back')
        await frameTopEdge(page)
        await page.getByRole('button', { name: 'Main menu' }).click()
        await page.mouse.move(0, 0)
        await page.waitForTimeout(400)
    },
}

// The sign-in card: what a visitor sees after `docker compose up` and
// before the first login. Its own context, never signed in.
const signedOutViews = {
    '00-login': async (page) => {
        await page.goto(`${BASE}/?theme=${encodeURIComponent(args.theme || 'telegram')}`, { waitUntil: 'load' })
        await page.locator('.login-card').waitFor({ state: 'visible', timeout: 20000 })
        await page.mouse.move(0, 0)
        await settle(page)
    },
}

async function run(browser, profile, views, signIn = true) {
    const wanted = Object.entries(views).filter(([name]) => !ONLY || ONLY.has(name.slice(0, 2)))
    if (!wanted.length) return
    let context
    if (signIn) {
        context = await newContext(browser, profile)
    } else {
        context = await browser.newContext({ ...profile, reducedMotion: 'reduce', timezoneId: 'UTC', locale: 'en-US' })
        await allowNotifications(context)
        await context.addInitScript(() => {
            try { localStorage.setItem('transcriptNudgeDismissed', '1') } catch (e) { /* ignore */ }
        })
    }
    try {
        for (const [name, view] of wanted) {
            const page = await newPage(context)
            try {
                const clip = await view(page)
                await shot(page, name, clip)
            } catch (e) {
                console.error(`FAILED ${name}: ${e.message.split('\n')[0]}`)
                process.exitCode = 1
            } finally {
                await page.close()
            }
        }
    } finally {
        await context.close()
    }
}

const browser = await chromium.launch({ headless: true })
try {
    await run(browser, DESKTOP, desktopViews)
    await run(browser, MOBILE, mobileViews)
    await run(browser, DESKTOP, signedOutViews, false)
    await run(browser, DESKTOP, shareViews, false)
} finally {
    await browser.close()
}
