// Run against Vite. Every API request is intercepted with isolated layout data.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { layoutResponse } from './fixtures/mobile-layout-data.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.NARRIFY_LAYOUT_URL || 'http://127.0.0.1:5173'
const output = process.env.NARRIFY_LAYOUT_SCREENSHOTS
const routes = {
  user: ['dashboard', 'projects/demo', 'trash', 'tasks', 'resources', 'usage', 'text', 'script', 'voices', 'batch', 'preview', 'merge', 'bgm', 'settings'],
  admin: ['admin', 'admin?tab=performance', 'admin?tab=users', 'admin?tab=resources', 'admin?tab=settings', 'admin?tab=tasks', 'admin?tab=logs', 'admin/music'],
  guest: ['login', 'admin/login', 'access-denied'],
}
const viewports = [
  { width: 320, height: 640, phone: true },
  { width: 390, height: 844, phone: true },
  { width: 600, height: 900, phone: true },
  { width: 844, height: 390, phone: true },
  { width: 1024, height: 900, phone: false },
  { width: 1440, height: 900, phone: false },
]

// Intentional horizontal scrolling inside tables/nav is allowed. Content that
// overflows without such a container is inaccessible on a phone and must fail.
async function checkBounds(page, label, selector = '.app-content') {
  const issues = await page.evaluate(selector => {
    const host = document.querySelector(selector) || document.querySelector('main')
    const issues = []
    for (const element of host.querySelectorAll('*')) {
      const rect = element.getBoundingClientRect()
      if (rect.width < 1 || rect.height < 1 || (rect.right <= innerWidth + 0.5 && rect.left >= -0.5)) continue
      let contained = false
      for (let parent = element.parentElement; parent && parent !== host; parent = parent.parentElement) {
        if (['auto', 'scroll', 'hidden'].includes(getComputedStyle(parent).overflowX) && parent.scrollWidth > parent.clientWidth) {
          contained = true
          break
        }
      }
      if (!contained) issues.push(element.tagName + '.' + String(element.className))
    }
    return [...new Set(issues)]
  }, selector)
  assert.deepEqual(issues, [], label + ': content exceeds viewport')
}
async function checkDialog(page, locator) {
  const rect = await locator.boundingBox()
  assert.ok(rect, 'dialog is visible')
  const viewport = page.viewportSize()
  assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x + rect.width <= viewport.width + 1 && rect.y + rect.height <= viewport.height + 1, 'dialog fits viewport')
}

if (output) await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
let checked = 0
try {
  for (const viewport of viewports) {
    for (const [role, paths] of Object.entries(routes)) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        hasTouch: viewport.phone,
      })
      const page = await context.newPage()
      const errors = []
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/api/**', route => {
        const path = new URL(route.request().url()).pathname
        // Vite modules under /src/api/ are source files, not backend requests.
        if (!path.startsWith('/api/')) return route.continue()
        const body = layoutResponse(path, role)
        return route.fulfill({
          status: body === null ? 503 : 200,
          contentType: 'application/json',
          body: JSON.stringify(body === null ? { detail: '隔离验收：接口暂不可用' } : body),
        })
      })
      await page.route('https://fonts.googleapis.com/**', route => route.abort())
      for (const path of paths) {
        const label = `${viewport.width}×${viewport.height} / ${path}`
        await page.goto(`${base}/#/${path}`, { waitUntil: 'domcontentloaded' })
        if (role !== 'guest') await page.locator('.app-content').waitFor()
        else await page.locator('main').waitFor()
        await page.waitForTimeout(200)
        await checkBounds(page, label)
        assert.deepEqual(errors.splice(0), [], label + ': browser errors')
        if (role !== 'guest') {
          await page.getByRole('button', { name: /账户菜单/ }).click()
          await page.waitForTimeout(250)
          await checkDialog(page, page.locator('.app-account__menu'))
          await page.keyboard.press('Escape')
          await page.locator('.app-account__menu').waitFor({ state: 'hidden' })
        }
        if (path === 'resources') {
          await page.locator('.rc-tabs button').nth(1).click()
          await page.locator('.rc-project-name').first().click()
          await page.locator('.rc-file-name button').first().click()
          await checkDialog(page, page.getByRole('dialog'))
          await checkBounds(page, label + ' resource preview', '.rc-preview-host')
          await page.keyboard.press('Escape')
          await page.locator('.rc-tabs button').nth(2).click()
          await checkBounds(page, label + ' storage')
        }
        if (viewport.phone && path === 'projects/demo') {
          assert.ok(await page.locator('.stage-row').evaluateAll(rows => rows.every(row => {
            const progress = row.querySelector('.stage-completion')
            return progress.getBoundingClientRect().bottom <= row.getBoundingClientRect().bottom + 1
          })), label + ': progress stays within its stage row')
        }
        if (path === 'tasks') {
          await page.locator('.task-center__folder').first().click()
          await checkDialog(page, page.getByRole('dialog'))
          await checkBounds(page, label + ' task details')
          await page.keyboard.press('Escape')
        }
        if (['text', 'script', 'voices', 'batch', 'merge', 'bgm'].includes(path)) {
          const table = page.locator('table.workbench-table').first()
          await table.waitFor()
          if (viewport.phone) {
            await page.locator('.mobile-table-hint').first().waitFor()
            // Includes many-page pagination on text, script and voices.
            const pager = page.locator('.mobile-pager').first()
            await pager.locator('.pager-current').waitFor()
            const next = pager.getByRole('button', { name: '下一页' })
            if (await next.isEnabled()) await next.click()
            await checkBounds(page, label + ' next page')
          }
        }
        if (path === 'preview') {
          await page.locator('.preview-page > .grid > :first-child .cursor-pointer').first().click()
          await page.locator('.preview-page > .grid > :nth-child(2) .cursor-pointer').first().click()
          await page.locator('.preview-page textarea').first().waitFor()
          await checkBounds(page, label + ' line editor')
        }
        if (viewport.phone && ['voices', 'batch', 'merge', 'bgm'].includes(path)) {
          const opener = page.locator(path === 'voices' ? '.voice-name' : '.production-name').first()
          if (await opener.count()) {
            await opener.click()
            await checkDialog(page, page.getByRole('dialog'))
            await page.keyboard.press('Escape')
          }
        }
        if (path === 'admin?tab=settings') {
          const tabs = page.locator('.settings-nav button')
          for (let index = 0; index < await tabs.count(); index++) {
            await tabs.nth(index).click()
            await page.waitForTimeout(100)
            await checkBounds(page, label + ' settings ' + index)
          }
        }
        if (output && viewport.width === 390) {
          await page.evaluate(() => {
            const content = document.querySelector('.app-content')
            if (content) content.scrollTop = 0
          })
          await page.screenshot({ path: join(output, path.replaceAll('/', '-').replaceAll('?', '-') + '.png'), animations: 'disabled' })
        }
        assert.deepEqual(errors.splice(0), [], label + ': interaction errors')
        checked++
        console.log('PASS ' + label)
      }
      await context.close()
    }
  }
} finally {
  await browser.close()
}
console.log(`Verified ${checked} route/viewport combinations, account menus, pagination, task details, preview editor and admin settings.`)
