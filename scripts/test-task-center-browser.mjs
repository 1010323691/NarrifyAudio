// Run against Vite; all API data is isolated and intercepted.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir } from 'node:fs/promises'
import { layoutResponse } from './fixtures/mobile-layout-data.mjs'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.NARRIFY_LAYOUT_URL || 'http://127.0.0.1:5173'
const output = process.env.NARRIFY_LAYOUT_SCREENSHOTS
if (output) await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport })
    const errors = [], centerRequests = [], streams = [], pageSizes = []
    let releaseSummary, releaseItems
    const summaryGate = new Promise(resolve => { releaseSummary = resolve })
    const itemsGate = new Promise(resolve => { releaseItems = resolve })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url())
      const path = url.pathname
      if (!path.startsWith('/api/')) return route.continue()
      if (path === '/api/v1/tasks/stream') {
        streams.push(url.search)
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: 'data: {"type":"snapshot_all","tasks":[]}\n\n' })
      }
      if (path.startsWith('/api/v1/tasks/center/')) centerRequests.push(path)
      let body = layoutResponse(path, 'user')
      if (path.endsWith('/center/summary')) await summaryGate
      if (path.endsWith('/center/items')) {
        await itemsGate
        const index = Number(url.searchParams.get('page') || 1)
        const size = Number(url.searchParams.get('page_size') || 10)
        pageSizes.push(size)
        body = { ...body, total: 5000, counts: { ...body.counts, task_count: 5000 }, page: index, page_size: size,
          items: Array.from({ length: size }, (_, i) => ({ ...body.items[0], id: `${index}-${i}`, label: `第 ${(index - 1) * size + i + 1} 章 · 测试章节` })) }
      }
      return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
    })
    await page.route('https://fonts.googleapis.com/**', route => route.abort())
    await page.goto(`${base}/#/tasks`, { waitUntil: 'domcontentloaded' })
    await page.locator('.task-center__stage').first().waitFor()
    assert.equal(await page.locator('.task-center__stage').count(), 8)
    assert.equal(await page.locator('.task-center__stage-count').first().textContent(), '…')
    assert.equal(centerRequests.filter(path => path.endsWith('/groups')).length, 0)
    if (output) await page.screenshot({ path: `${output}/task-center-shell-${viewport.width}.png` })
    releaseSummary()
    await page.locator('.task-center__folder').first().waitFor()
    assert.equal(centerRequests.filter(path => path.endsWith('/items')).length, 0)
    if (output) await page.screenshot({ path: `${output}/task-center-summary-${viewport.width}.png` })
    await page.locator('.task-center__folder').first().click()
    await page.getByRole('dialog').waitFor()
    assert.equal(await page.locator('.task-center__task-row').count(), 0)
    assert.ok(await page.getByRole('dialog').getByText('正在读取当前页任务…').isVisible())
    if (output) await page.screenshot({ path: `${output}/task-center-items-loading-${viewport.width}.png` })
    releaseItems()
    await page.locator('.task-center__task-row').nth(9).waitFor()
    // 默认每页 10 条，服务端按页返回，只请求当前页。
    assert.equal(await page.locator('.task-center__task-row').count(), 10)
    assert.ok(pageSizes.every(size => size === 10), `default page size 10, got ${pageSizes}`)
    if (output) await page.screenshot({ path: `${output}/task-center-items-${viewport.width}.png` })
    const dialog = await page.getByRole('dialog').boundingBox()
    assert.ok(dialog.x >= 0 && dialog.y >= 0 && dialog.x + dialog.width <= viewport.width + 1 && dialog.y + dialog.height <= viewport.height + 1)
    await page.getByRole('dialog').getByRole('navigation', { name: '分页' }).getByRole('button', { name: '下一页' }).click()
    await page.getByText('第 11 章 · 测试章节', { exact: true }).waitFor()
    assert.equal(await page.locator('.task-center__task-row').count(), 10)
    await page.keyboard.press('Escape')
    await page.getByRole('dialog').waitFor({ state: 'hidden' })
    assert.deepEqual(errors, [])
    assert.ok(streams.every(query => query.includes('project_id=')), 'task center must not open a global full snapshot stream')
    await page.close()
    console.log(`Task center progressive load and pagination verified: ${viewport.width}×${viewport.height}`)
  }
} finally { await browser.close() }
