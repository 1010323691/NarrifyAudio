// Exercise actual Vue feedback against isolated delayed responses; no real task is submitted.
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
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = [], submissions = []
  page.on('pageerror', error => errors.push(error.message))
  let release
  const response = new Promise(resolve => { release = resolve })
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/tts/batch') {
      submissions.push({ key: request.headers()['idempotency-key'], body: request.postDataJSON() })
      await response
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ batch_id: 'batch', task_id: 'tts-task', task_ids: ['tts-task'] }) })
    }
    if (path.startsWith('/api/v1/tasks/submissions/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not yet committed"}' })
    if (path === '/api/v1/tasks/stream') return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"type":"snapshot_all","tasks":[]}\n\n' })
    let body = layoutResponse(path, 'user')
    if (path === '/api/tts/batch-status' || path === '/api/tts/batch-list') body = { ...body, files: body.files.map(row => ({ ...row, remaining: row.total - row.completed, is_script: true })) }
    if (path === '/api/tts/batch-list') body = { ...body, pagination: { total: 1, page: 1, page_size: 10, counts: { all: 1, pending: 1, complete: 0 } } }
    return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
  })
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  await page.goto(`${base}/#/batch`, { waitUntil: 'domcontentloaded' })
  const start = page.getByRole('button', { name: '增量合成', exact: true })
  await start.waitFor()
  if (!await start.isEnabled()) await page.locator('.production-scroll tbody input[type="checkbox"]').first().check()
  await start.click()
  const immediate = page.getByRole('button', { name: '正在提交合成任务…', exact: true })
  await immediate.waitFor()
  assert.equal(await immediate.isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: '重新合成所选', exact: true }).isDisabled(), true)
  if (output) await page.screenshot({ path: `${output}/tts-submitting.png` })
  await page.getByRole('button', { name: '服务器正在处理提交，请稍候…', exact: true }).waitFor({ timeout: 7000 })
  assert.equal(submissions.length, 1)
  assert.ok(submissions[0].key)
  assert.equal(submissions[0].body.project_id, 'demo')
  if (output) await page.screenshot({ path: `${output}/tts-submission-waiting.png` })
  release()
  await page.getByRole('button', { name: '已提交，等待任务执行', exact: true }).waitFor()
  assert.equal(submissions.length, 1)
  assert.deepEqual(errors, [])
  console.log('Actual Vue feedback verified: immediate disable, 5-second label, one keyed request.')
} finally { await browser.close() }
