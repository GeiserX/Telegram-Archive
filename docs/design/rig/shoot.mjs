// Screenshot rig for the viewer mockups.
//
// Logs in to a running viewer seeded with scripts/generate_dummy_db.py, opens
// the same views every time and saves one PNG per view. A CSS file, when given,
// is injected on every page so a mockup can restyle the app without touching it.
//
// Usage:
//   node docs/design/rig/shoot.mjs --css <file|none> --out <dir> --port <n>
//        [--theme <name>] [--only 02,08]
//
// Credentials come from VIEWER_USERNAME and VIEWER_PASSWORD, with the demo
// defaults admin and demo-not-a-secret.

import { createRequire } from 'node:module'
import { mkdirSync, readFileSync } from 'node:fs'
import { resolve, join } from 'node:path'

const require = createRequire(import.meta.url)
const PLAYWRIGHT = process.env.PLAYWRIGHT_PATH || 'playwright'
const { chromium } = require(PLAYWRIGHT)

function parseArgs(argv) {
    const args = { css: 'none', out: null, port: null, theme: null, only: null }
    for (let i = 0; i < argv.length; i++) {
        const key = argv[i].replace(/^--/, '')
        if (!(key in args)) throw new Error(`unknown option ${argv[i]}`)
        args[key] = argv[++i]
    }
    if (!args.out || !args.port) {
        throw new Error('usage: shoot.mjs --css <file|none> --out <dir> --port <n> [--theme <name>] [--only 02,08]')
    }
    return args
}

const args = parseArgs(process.argv.slice(2))
const BASE = `http://127.0.0.1:${args.port}`
const OUT = resolve(args.out)
const CSS = args.css && args.css !== 'none' ? readFileSync(resolve(args.css), 'utf8') : null
const ONLY = args.only ? new Set(args.only.split(',').map((s) => s.trim().padStart(2, '0'))) : null
const USER = process.env.VIEWER_USERNAME || 'admin'
const PASS = process.env.VIEWER_PASSWORD || 'demo-not-a-secret'
const GROUP = 'Weekend Hikers'
mkdirSync(OUT, { recursive: true })

const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }
const MOBILE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }

// --- page helpers ----------------------------------------------------------

async function injectCss(page) {
    if (!CSS) return
    const present = await page.evaluate(() => !!document.getElementById('mockup-override')).catch(() => false)
    if (!present) await page.addStyleTag({ content: CSS }).then((h) => h.evaluate((el) => { el.id = 'mockup-override' }))
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

async function shot(page, name) {
    await settle(page)
    const file = join(OUT, `${name}.png`)
    await page.screenshot({ path: file, animations: 'disabled', caret: 'hide' })
    console.log(`wrote ${file}`)
}

async function newPage(context) {
    const page = await context.newPage()
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
    await page.waitForTimeout(400)
}

async function newContext(browser, profile) {
    const context = await browser.newContext({ ...profile, reducedMotion: 'reduce', timezoneId: 'UTC', locale: 'en-US' })
    // The transcription nudge only shows on a fresh browser with no server set;
    // dismissing it keeps every view on the chat itself.
    await context.addInitScript(() => {
        try { localStorage.setItem('transcriptNudgeDismissed', '1') } catch (e) { /* ignore */ }
    })
    const res = await context.request.post(`${BASE}/api/login`, { data: { username: USER, password: PASS } })
    if (!res.ok()) throw new Error(`login failed: HTTP ${res.status()}`)
    return context
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
    },
    '03-chat-replies': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'The north lot. It fills up by 8')
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
    },
    '09-theme-picker': async (page) => {
        await open(page)
        await openGroup(page)
        await page.getByRole('button', { name: 'Choose color theme' }).click()
        await page.waitForTimeout(300)
    },
}

const mobileViews = {
    '04-chat-mobile': async (page) => {
        await open(page)
        await openGroup(page)
        await centerOn(page, 'A few shots from the ridge loop', 'start')
    },
    '05-chat-list-mobile': async (page) => {
        await open(page)
    },
}

async function run(browser, profile, views) {
    const wanted = Object.entries(views).filter(([name]) => !ONLY || ONLY.has(name.slice(0, 2)))
    if (!wanted.length) return
    const context = await newContext(browser, profile)
    try {
        for (const [name, view] of wanted) {
            const page = await newPage(context)
            try {
                await view(page)
                await shot(page, name)
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
} finally {
    await browser.close()
}
