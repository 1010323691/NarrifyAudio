// Run against Vite with isolated fixtures; never submit a production task.
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
    const errors = [], reads = [], streams = [], mutations = []
    let release
    const gate = new Promise(resolve => { release = resolve })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), path = url.pathname
      if (!path.startsWith('/api/')) return route.continue()
      let body = layoutResponse(path, 'user')
      if (!['GET', 'HEAD'].includes(route.request().method())) mutations.push(path)
      if (path === '/api/v1/tasks/stream') {
        streams.push(url.search)
        return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"type":"snapshot_all","tasks":[]}\n\n' })
      }
      if (path === '/api/v1/tasks/overview') body = { statuses: [], failures: [], failure_count: 0 }
      if (path.endsWith('/summary')) {
        const section = url.searchParams.get('section')
        reads.push(section)
        if (section === 'production') await gate
        const ratio = { completed: 1, total: 2, unit: '章节', percent: 50 }
        body = { project_id: 'demo', stage_completion: section === 'text' ? { '02_split_text': ratio } : section === 'catalog' ? { '03_parsed_json': ratio, '04_voice_profiles': ratio } : { '05_audio_chunk': ratio, '06_audio_merge': ratio, '08_bgm': ratio, '07_output': { completed: 0, total: 0, unit: '分集', percent: null } } }
      }
      return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
    })
    await page.route('https://fonts.googleapis.com/**', route => route.abort())
    const start = performance.now()
    await page.goto(`${base}/#/projects/demo`, { waitUntil: 'domcontentloaded' })
    try { await page.locator('.stage-row').first().waitFor({ timeout: 8000 }) } catch (error) { console.log({ url: page.url(), body: await page.locator('body').innerText(), errors }); if (output) await page.screenshot({ path: `${output}/overview-error.png` }); throw error }
    assert.equal(await page.locator('.stage-row').count(), 8)
    assert.equal(await page.locator('.stage-row').first().isEnabled(), true)
    await page.locator('.stage-row').first().getByText('50%', { exact: true }).waitFor()
    const summaryMs = performance.now() - start
    assert.ok(await page.getByText('暂无失败任务', { exact: true }).isVisible())
    assert.ok(await page.locator('.stage-row').nth(3).getByText('正在读取进度', { exact: true }).isVisible())
    assert.deepEqual([...reads].sort(), ['catalog', 'production', 'text'])
    assert.ok(streams.every(query => query.includes('project_id=')), 'overview must not open a full global task stream')
    assert.deepEqual(mutations, [])
    if (output) await page.screenshot({ path: `${output}/overview-partial-${viewport.width}.png` })
    release()
    await page.locator('.stage-row').nth(3).getByText('50%', { exact: true }).waitFor()
    assert.deepEqual(errors, [])
    await page.locator('.stage-row').first().click()
    await page.waitForURL('**/#/text')
    assert.deepEqual(mutations, [])
    await page.close()
    console.log(`Overview independently rendered while audio was delayed: ${viewport.width}×${viewport.height}, first text summary ${Math.round(summaryMs)}ms (fixture/local browser only)`)
  }
} finally { await browser.close() }
