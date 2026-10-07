// Real Vue pages with isolated responses; no production task is submitted.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdir } from 'node:fs/promises'
import { layoutResponse } from './fixtures/mobile-layout-data.mjs'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const base = process.env.NARRIFY_LAYOUT_URL || 'http://127.0.0.1:5173'
const output = process.env.NARRIFY_LAYOUT_SCREENSHOTS
if (output) await mkdir(output, { recursive: true })
const cases = [
  ['batch', '/api/tts/batch-list', '.production-scroll tbody tr', 'user'],
  ['merge', '/api/tts/merge-list', '.production-scroll tbody tr', 'user'],
  ['bgm', '/api/bgm/chapters', '.production-scroll tbody tr', 'user'],
  ['voices', '/api/tts/voices', '.voice-scroll tbody tr', 'user'],
  ['text', '/text-format/state', 'table.wb-chapter-table tbody tr', 'user'],
  ['script', '/script-parse/state', 'table.wb-chapter-table tbody tr', 'user'],
  ['preview', '/api/tts/batch-list', null, 'user'],
  ['dashboard', '/api/v1/projects', '.project-card', 'user'],
  ['trash', '/api/v1/projects/trash', null, 'user'],
  ['usage', '/api/v1/quota/transactions', '.usage-table tbody tr', 'user'],
  ['resources', '/api/v1/resources', null, 'user'],
  ['admin/music', '/api/music/library', 'tbody tr', 'admin'],
  ['admin?tab=users', '/api/v1/admin/users', '.users-table tbody tr', 'admin'],
  ['admin?tab=tasks', '/api/v1/admin/tasks', '.wide-table tbody tr', 'admin'],
  ['admin?tab=logs', '/api/v1/admin/events', null, 'admin'],
]
const browser = await chromium.launch({ headless: true })
try {
  for (const [path, target, selector, role] of cases) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const errors = [], requests = [], mutations = []
    page.on('pageerror', error => errors.push(error.message))
    function reply(url) {
      const name = url.pathname
      const p = Number(url.searchParams.get('page') || 1), size = Number(url.searchParams.get('page_size') || (name.includes('/admin/') ? 50 : 10))
      const meta = { total: 125, page: p, page_size: size, counts: { all: 125, pending: 125, marked: 0, adjusted: 0, ready: 0, active: 125, active_admins: 2, complete: 125, enabled: 125 } }
      let body = layoutResponse(name, role)
      const rows = template => Array.from({ length: size }, (_, i) => ({ ...template, id: `PAGE-${p}-${i}`, name: `PAGE-${p}-${i}`, stem: `PAGE-${p}-${i}`, title: `PAGE-${p}-${i}`, seq: (p - 1) * size + i + 1, key: `PAGE-${p}-${i}` }))
      if (name === '/api/v1/projects' || name === '/api/v1/projects/trash') return { items: rows(body[0]), pagination: meta }
      if (name === '/api/tts/batch-list') return { files: rows(layoutResponse('/api/tts/batch-status', role).files[0]), pagination: meta }
      if (name === '/api/tts/merge-list') return { packages: rows(layoutResponse('/api/tts/merge-status', role).packages[0]), pagination: meta }
      if (name === '/api/bgm/chapters') return { ...body, chapters: rows(body.chapters[0]), pagination: meta }
      if (name === '/api/tts/voices') return { ...body, speakers: rows(body.speakers[0]), pagination: meta }
      if (name.endsWith('/text-format/state')) {
        const chapters = rows(body.version.chapters[0]).map(ch => ({ ...ch, file: { name: ch.name + '.txt', chars: 3000, file_id: ch.id } }))
        return { ...body, version: { ...body.version, chapters, files: chapters.map(ch => ch.file) }, pagination: meta }
      }
      if (name.endsWith('/script-parse/state')) {
        const refs = rows(body.source.version.chapters[0]).map(ch => ({ ...ch, name: ch.name + '.txt' }))
        return { ...body, chapter_refs: refs, source: { ...body.source, version: { ...body.source.version, chapters: refs, files: refs.map(ch => ({ name: ch.name, file_id: ch.id })) } }, files: refs.map(ch => ({ name: ch.name, input: { name: ch.name, sha256: ch.id, size: 3000 }, latest_task: null, result: null, result_status: null })), pagination: meta }
      }
      if (name === '/api/v1/quota/transactions') return { items: rows({ kind: 'consume', amount: -2, available_after: 1000, created_at: '2026-10-06T00:00:00Z', note: `PAGE-${p}` }), pagination: meta, daily: [1,2,3,4,5,6,7], registered_storage_bytes: 12000 }
      if (name === '/api/v1/resources') return { ...body, projects: rows(body.projects[0]).map(row => ({ ...row, project_id: row.id })), pagination: meta }
      if (name === '/api/music/library') return { ...body, tracks: Object.fromEntries(rows(Object.values(body.tracks)[0]).map(row => [row.name + '.mp3', row])), pagination: meta }
      if (name === '/api/v1/admin/users') return { items: rows(body[0]).map(row => ({ ...row, username: row.name, display_name: row.name })), pagination: meta }
      if (name === '/api/v1/admin/tasks') return { items: rows(body[0]), pagination: meta }
      if (name === '/api/v1/admin/events') return { items: rows(body[0]).map(row => ({ ...row, message: row.name })), pagination: meta }
      return body
    }
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url())
      if (!url.pathname.startsWith('/api/')) return route.continue()
      requests.push({ path: url.pathname, page: url.searchParams.get('page'), size: url.searchParams.get('page_size'), keys: url.searchParams.get('keys_only') })
      if (!['GET','HEAD'].includes(route.request().method())) mutations.push(url.pathname)
      if (url.pathname === '/api/v1/tasks/stream') return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"type":"snapshot_all","tasks":[]}\n\n' })
      const body = reply(url)
      return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
    })
    await page.route('https://fonts.googleapis.com/**', route => route.abort())
    await page.goto(`${base}/#/${path}`, { waitUntil: 'domcontentloaded' })
    const pager = page.getByRole('button', { name: '下一页', exact: true }).first()
    try { await pager.waitFor({ timeout: 10000 }) }
    catch (error) { console.log(path, errors, requests, (await page.locator('body').innerText()).slice(-2500)); throw error }
    await page.waitForFunction(() => { const button = document.querySelector('button[aria-label="下一页"]'); return button ? !button.disabled : [...document.querySelectorAll('button')].some(b => b.textContent.trim() === '下一页' && !b.disabled) })
    if (selector) assert.ok(await page.locator(selector).count() <= 50, path)
    const reads = requests.filter(r => r.path.endsWith(target))
    assert.ok(reads.length > 0 && reads.every(r => r.page === '1'), `${path}: initial request must be page 1`)
    assert.ok(!requests.some(r => r.keys === 'true'), `${path}: no automatic all-key selection`)
    await pager.click()
    await page.waitForFunction(() => document.body.textContent.includes('PAGE-2-'))
    assert.ok(requests.some(r => r.path.endsWith(target) && r.page === '2'), `${path}: page 2 must be fetched at click time`)
    assert.deepEqual(mutations, [], `${path}: browsing must never submit tasks`)
    assert.deepEqual(errors, [], path)
    if (output) await page.screenshot({ path: `${output}/${path.replace(/[^a-z]/g, '-')}-page2.png` })
    await page.close()
    console.log(`Server paging verified: ${path}`)
  }
} finally { await browser.close() }
