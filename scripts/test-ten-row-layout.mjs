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
  // 列表接口按页加载：fixture 总是给 24 行，这里按 page/page_size 切片并补 pagination，
  // 与后端契约一致（前端只请求当前页）。
  const pageSlice = (body, key, url) => {
    const items = body[key]
    const size = Number(url.searchParams.get('page_size') || 10)
    const index = Number(url.searchParams.get('page') || 1)
    return { ...body, [key]: items.slice((index - 1) * size, index * size), pagination: { total: items.length, page: index, page_size: size, counts: { all: items.length } } }
  }
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()
    let body = layoutResponse(path, 'user')
    if (path === '/api/tts/batch-list') {
      body = pageSlice({ ...body, files: Array.from({ length: 24 }, (_, i) => ({ ...body.files[0], name: `chapter-${i}.json` })) }, 'files', url)
    }
    if (path === '/api/tts/merge-list') {
      body = pageSlice({ ...body, packages: Array.from({ length: 24 }, (_, i) => ({ ...body.packages[0], name: `pkg-${i}.json` })) }, 'packages', url)
    }
    if (path === '/api/bgm/chapters') {
      body = pageSlice({ ...body, chapters: Array.from({ length: 24 }, (_, i) => ({ ...body.chapters[0], stem: `chapter-${i}`, timeline: { sections: 3 } })) }, 'chapters', url)
    }
    if (path.endsWith('/script-parse/state') && body?.files) {
      const version = body.source.version
      const chapters = Array.from({ length: 24 }, (_, i) => ({ ...version.chapters[0], seq: i + 1, key: `c${i + 1}`, final_num: i + 1, orig_num: i + 1 }))
      const files = Array.from({ length: 24 }, (_, i) => ({ ...body.files[0], name: `第${String(i + 1).padStart(3, '0')}章_长标题.txt` }))
      const size = Number(url.searchParams.get('page_size') || 10)
      const index = Number(url.searchParams.get('page') || 1)
      body = {
        ...body,
        files: files.slice((index - 1) * size, index * size),
        source: { ...body.source, version: { ...version, chapters: chapters.slice((index - 1) * size, index * size), files: files.map(file => ({ name: file.name, chars: 3000 })) } },
        pagination: { total: 24, page: index, page_size: size, counts: { all: 24 } },
      }
    }
    return route.fulfill({ status: body === null ? 503 : 200, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
  })
  await page.route('https://fonts.googleapis.com/**', route => route.abort())
  for (const path of ['script', 'batch', 'merge', 'bgm']) {
    await page.goto(`${base}/#/${path}`)
    // Routes are lazy-loaded: wait for this route's own table, not the previous
    // route's table that is still on screen while the chunk compiles.
    const tableSelector = path === 'script' ? 'table.wb-chapter-table' : '.production-scroll table.workbench-table'
    await page.waitForSelector(tableSelector)
    const table = page.locator(tableSelector)
    await table.locator('tbody tr').nth(9).waitFor()

    // 契约：固定十行盒子的行高只由视口决定（v-fit-rows），与内容无关；
    // 10 条完整显示，盒子与其裁切祖先都不溢出，分页条在视口内；20/50 条在盒内滚动、盒高不变。
    const measure = () => table.evaluate(element => {
      const box = element.parentElement
      const rows = Array.from(element.tBodies[0].rows)
      const clipped = []
      for (let node = box.parentElement; node && node !== document.body; node = node.parentElement) {
        if (getComputedStyle(node).overflowY !== 'visible' && node.scrollHeight - node.clientHeight > 1) clipped.push(node.className)
      }
      const pager = Array.from(document.querySelectorAll('select[aria-label="每页条数"]')).find(el => el.offsetParent)
      return {
        rows: rows.length,
        rowHeights: [...new Set(rows.map(r => Math.round(r.getBoundingClientRect().height * 100) / 100))],
        box: box.clientHeight,
        boxScroll: box.scrollHeight,
        head: element.tHead.getBoundingClientRect().height,
        clipped,
        pagerBottom: pager ? pager.getBoundingClientRect().bottom : null,
        innerHeight,
      }
    })
    const settle = () => page.waitForTimeout(300)
    for (const height of [1080, 900]) {
      await page.setViewportSize({ width: 1440, height })
      await settle()
      const m = await measure()
      assert.equal(m.rows, 10, `${path}@${height}: ten rows`)
      assert.equal(m.rowHeights.length, 1, `${path}@${height}: equal row heights ${m.rowHeights}`)
      assert.ok(Math.abs(m.box - (m.head + 10 * m.rowHeights[0])) <= 2, `${path}@${height}: box = head + 10 rows (${m.box} vs ${m.head + 10 * m.rowHeights[0]})`)
      assert.ok(m.boxScroll - m.box <= 1, `${path}@${height}: no inner scrollbar at 10 rows`)
      assert.deepEqual(m.clipped, [], `${path}@${height}: no clipped/scrolling ancestor`)
      assert.ok(m.pagerBottom !== null && m.pagerBottom <= m.innerHeight, `${path}@${height}: pager inside viewport (${m.pagerBottom})`)
    }

    // 行内容原地变长（如分析完成后状态文案变化）不得改变行高。
    const before = await measure()
    await table.evaluate(element => {
      for (const cell of element.tBodies[0].rows[0].cells) {
        // 状态文案在原地变长：追加到单元格最深的文本元素里，nowrap + 省略号必须兜住，行高不变。
        let target = cell
        while (target.lastElementChild && !target.matches('input,button,svg')) target = target.lastElementChild
        if (target.matches('input,button,svg')) target = target.parentElement
        target.append(' 原地变长的状态文案，原地变长的状态文案，原地变长的状态文案，原地变长的状态文案')
      }
    })
    await settle()
    const grown = await measure()
    assert.deepEqual(grown.rowHeights, before.rowHeights, `${path}: row height ignores content growth`)
    assert.equal(grown.box, before.box, `${path}: box height ignores content growth`)

    const pager = page.getByRole('combobox', { name: '每页条数' })
    await pager.selectOption('20')
    await page.waitForFunction((selector) => document.querySelector(`${selector} tbody`).rows.length === 20, tableSelector)
    await settle()
    const twenty = await measure()
    assert.equal(twenty.box, before.box, `${path}: box height unchanged at 20 rows`)
    assert.ok(twenty.boxScroll > twenty.box, `${path}: 20 rows scroll inside the box`)
    assert.equal(twenty.rowHeights.length, 1)
    await pager.selectOption('10')
    await page.waitForFunction((selector) => document.querySelector(`${selector} tbody`).rows.length === 10, tableSelector)
    console.log(`${path}: ten rows fit at 1080/900; row height content-independent; 20 → 10 pagination passed`)
  }
} finally {
  await browser.close()
}
