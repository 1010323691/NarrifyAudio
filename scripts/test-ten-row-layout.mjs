// Run against Vite with isolated API data; no production requests are sent.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { layoutResponse } from './fixtures/mobile-layout-data.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.NARRIFY_LAYOUT_URL || 'http://127.0.0.1:5173'
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const body = layoutResponse(path, 'user')
    if (path === '/api/tts/batch-status') {
      body.files = Array.from({ length: 24 }, (_, i) => ({ ...body.files[0], name: `chapter-${i}.json` }))
    }
    if (path.startsWith('/api/files/list/03_parsed')) {
      body.items = Array.from({ length: 24 }, (_, i) => ({ ...body.items[0], name: `chapter-${i}.json` }))
    }
    return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
  })
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  for (const path of ['script', 'batch']) {
    await page.goto(`${base}/#/${path}`)
    // Routes are lazy-loaded: wait for this route's own table, not the previous
    // route's table that is still on screen while the chunk compiles.
    const tableSelector = path === 'batch' ? '.production-scroll table.workbench-table' : 'table.wb-chapter-table'
    await page.waitForSelector(tableSelector)
    const table = page.locator(tableSelector)
    await table.locator('tbody tr').nth(9).waitFor()
    // Resize the mounted page so the observer must recalculate the height.
    for (const height of [1080, 920, 901, 900, 899, 880, 850]) {
      await page.setViewportSize({ width: 1440, height })
      await page.waitForFunction((selector) => {
        const el = document.querySelector(selector)
        return el && el.parentElement.scrollHeight === el.parentElement.clientHeight
      }, tableSelector)
      const dimensions = await table.evaluate(element => {
        const viewport = element.parentElement
        viewport.scrollTop = 100
        return {
          rows: element.tBodies[0].rows.length,
          client: viewport.clientHeight,
          scroll: viewport.scrollHeight,
          scrollTop: viewport.scrollTop,
          bottom: element.getBoundingClientRect().bottom,
          viewportBottom: viewport.getBoundingClientRect().top + viewport.clientHeight,
        }
      })
      assert.equal(dimensions.rows, 10)
      assert.equal(dimensions.scroll, dimensions.client, `${path} at ${height}px`)
      assert.equal(dimensions.scrollTop, 0)
      assert.ok(dimensions.bottom <= dimensions.viewportBottom, 'last row and border fit completely')
    }
    const pager = page.getByRole('combobox', { name: '每页条数' })
    await pager.selectOption('20')
    await page.waitForFunction((selector) => document.querySelector(`${selector} tbody`).rows.length === 20, tableSelector)
    assert.ok(await table.evaluate(element => element.parentElement.scrollHeight > element.parentElement.clientHeight))
    await pager.selectOption('10')
    await page.waitForFunction((selector) => {
      const el = document.querySelector(selector)
      return el.tBodies[0].rows.length === 10 && el.parentElement.scrollHeight === el.parentElement.clientHeight
    }, tableSelector)
    console.log(`${path}: ten rows fit at seven viewport heights; 20 → 10 pagination passed`)
  }
} finally {
  await browser.close()
}
