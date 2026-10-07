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
    if (path.startsWith('/api/files/list/05_audio_chunk')) {
      body.items = Array.from({ length: 24 }, (_, i) => ({ ...body.items[0], name: `pkg-${i}.json`, is_dir: true }))
    }
    if (path === '/api/tts/merge-status') {
      body.packages = Array.from({ length: 24 }, (_, i) => ({ ...body.packages[0], name: `pkg-${i}.json` }))
    }
    if (path === '/api/bgm/chapters') {
      body.chapters = Array.from({ length: 24 }, (_, i) => ({ ...body.chapters[0], stem: `chapter-${i}` }))
    }
    return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
  })
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  for (const path of ['script', 'batch', 'merge', 'bgm']) {
    if (path === 'bgm') {
      await page.route('**/api/bgm/chapters**', route => {
        const body = layoutResponse(new URL(route.request().url()).pathname, 'user')
        if (body?.chapters) {
          body.chapters = Array.from({ length: 24 }, (_, i) => ({ ...body.chapters[0], stem: `chapter-${i}`, timeline: { sections: 3 } }))
        }
        return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
      })
    }
    await page.goto(`${base}/#/${path}`)
    // Routes are lazy-loaded: wait for this route's own table, not the previous
    // route's table that is still on screen while the chunk compiles.
    const tableSelector = path === 'script' ? 'table.wb-chapter-table' : '.production-scroll table.workbench-table'
    await page.waitForSelector(tableSelector)
    const table = page.locator(tableSelector)
    await table.locator('tbody tr').nth(9).waitFor()
    // Resize the mounted page so the observer must recalculate the height.
    for (const height of [1080, 920, 901, 900, 899, 880, 850]) {
      // BGM rows carry a 10px timeline line: below 880px the viewport is
      // infeasible for ten 37px rows, so the floor + bounded overflow is the
      // expected steady state instead of an exact fit.
      const floorState = path === 'bgm' && height <= 880
      await page.setViewportSize({ width: 1440, height })
      await page.waitForFunction(({ selector, floorState }) => {
        const el = document.querySelector(selector)
        const viewport = el.parentElement
        if (!el || !viewport) return false
        if (!floorState) return viewport.scrollHeight === viewport.clientHeight
        const row = el.tBodies[0].rows[0]
        return el.style.getPropertyValue('--list-row-height') === '28px' &&
          Array.from(el.tBodies[0].rows).every(r => r.offsetHeight === row.offsetHeight)
      }, { selector: tableSelector, floorState })
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
      if (floorState) {
        assert.ok(dimensions.scroll - dimensions.client > 0 && dimensions.scroll - dimensions.client < 50, 'bounded overflow at the floor')
      } else {
        assert.equal(dimensions.scroll, dimensions.client, `${path} at ${height}px`)
      }
      assert.equal(dimensions.scrollTop, floorState ? dimensions.scroll - dimensions.client : 0)
      assert.ok(dimensions.bottom <= dimensions.viewportBottom + 50, 'last row stays within the bounded overflow')
    }
    const pager = page.getByRole('combobox', { name: '每页条数' })
    await pager.selectOption('20')
    await page.waitForFunction((selector) => document.querySelector(`${selector} tbody`).rows.length === 20, tableSelector)
    assert.ok(await table.evaluate(element => element.parentElement.scrollHeight > element.parentElement.clientHeight))
    await pager.selectOption('10')
    await page.waitForFunction(({ selector, floorState }) => {
      const el = document.querySelector(selector)
      if (!el || el.tBodies[0].rows.length !== 10) return false
      if (!floorState) return el.parentElement.scrollHeight === el.parentElement.clientHeight
      const row = el.tBodies[0].rows[0]
      return el.style.getPropertyValue('--list-row-height') === '28px' &&
        Array.from(el.tBodies[0].rows).every(r => r.offsetHeight === row.offsetHeight)
    }, { selector: tableSelector, floorState: path === 'bgm' })
    // [P7] regression: BGM row content ("N 个音乐段" / "已锁定") appears in
    // place after analysis settles — no DOM structure change. The live-tbody
    // size observation must re-fit the variable; without it the variable stays
    // at its pre-growth value even though the rows now render taller.
    if (path === 'bgm') {
      await page.setViewportSize({ width: 1440, height: 1080 })
      // The variable converges a frame after the resize (RO callback), so
      // wait until the value is stable before sampling the pre-growth fit.
      await page.waitForFunction(selector => {
        return new Promise(resolve => {
          const el = document.querySelector(selector)
          if (!el || !el.tBodies[0]?.rows.length) return resolve(false)
          const beforeSample = el.style.getPropertyValue('--list-row-height')
          setTimeout(() => resolve(el.style.getPropertyValue('--list-row-height') === beforeSample), 400)
        })
      }, tableSelector)
      const before = await table.evaluate(element => element.style.getPropertyValue('--list-row-height'))
      assert.ok(parseFloat(before) > 28, `growth needs headroom above the floor (got ${before})`)
      await table.evaluate(element => {
        const cells = element.tBodies[0].rows[0].cells
        for (const cell of cells) {
          const line = document.createElement('span')
          line.className = 'block text-[10px] text-muted-foreground'
          line.textContent = '原地撑高探针'
          cell.appendChild(line)
        }
      })
      await page.waitForFunction(selector => {
        return new Promise(resolve => {
          const el = document.querySelector(selector)
          if (!el) return resolve(false)
          const sample = el.style.getPropertyValue('--list-row-height')
          setTimeout(() => resolve(el.style.getPropertyValue('--list-row-height') === sample), 400)
        })
      }, tableSelector)
      const after = await table.evaluate(element => element.style.getPropertyValue('--list-row-height'))
      const dimensions = await table.evaluate(element => {
        const viewport = element.parentElement
        return {
          bottom: element.getBoundingClientRect().bottom,
          viewportBottom: viewport.getBoundingClientRect().top + viewport.clientHeight,
        }
      })
      assert.notEqual(after, before, 'in-place row growth re-measures the fit')
      assert.ok(dimensions.bottom <= dimensions.viewportBottom + 50, 'post-growth overflow stays bounded')
      console.log(`${path}: in-place row growth re-fit ${before} -> ${after}`)
    }
    console.log(`${path}: ten rows fit at seven viewport heights; 20 → 10 pagination passed`)
  }
} finally {
  await browser.close()
}
