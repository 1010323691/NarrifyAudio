// Actual dashboard, isolated read fixtures: hold all audio summaries across a page change.
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
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport})
    page.setDefaultTimeout(8000)
    const reads = [], errors = [], mutations = [], streams = []
    let deleted = false
    let release
    const gate = new Promise(resolve => {release = resolve})
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), path = url.pathname
      if (!path.startsWith('/api/')) return route.continue()
      reads.push({path, page:url.searchParams.get('page'), section:url.searchParams.get('section')})
      if (!['GET','HEAD'].includes(route.request().method())) mutations.push(path)
      let body = layoutResponse(path,'user')
      if (path === '/api/v1/projects/active') body = deleted ? {set:false,exists:false,project_id:'',project_name:'',path:'',dirs:{}} : {...body,project_id:'p1-0',project_name:'CARD-1-0'}
      if (path === '/api/v1/projects/p1-0' && route.request().method() === 'DELETE') { deleted = true; body = {ok:true} }
      if (path === '/api/v1/tasks/stream') {
        streams.push(url.search)
        return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"snapshot_all","tasks":[]}\n\n'})
      }
      if (path === '/api/v1/projects') {
        const p = Number(url.searchParams.get('page') || 1)
        body = {items:Array.from({length:12},(_,i)=>({...layoutResponse(path,'user')[0],id:`p${p}-${i}`,name:`CARD-${p}-${i}`})).filter(row=>!deleted || row.id !== 'p1-0'),pagination:{page:p,page_size:12,total:24}}
      }
      if (path.endsWith('/summary')) {
        const section = url.searchParams.get('section')
        assert.ok(section, 'no full summary allowed')
        if (section === 'production') await gate
        const ratio = {completed:1,total:2,unit:'章节',percent:50}
        body = {project_id:path.split('/')[4],stage_completion:section === 'text' ? {'02_split_text':ratio} : section === 'catalog' ? {'03_parsed_json':ratio,'04_voice_profiles':ratio} : {'05_audio_chunk':ratio,'06_audio_merge':ratio,'08_bgm':ratio,'07_output':{completed:0,total:0,unit:'分集',percent:null}}}
      }
      return route.fulfill({status:body === null ? 503 : 200,contentType:'application/json',body:JSON.stringify(body ?? {})})
    })
    await page.route('https://fonts.googleapis.com/**', route => route.abort())
    const start = performance.now()
    await page.goto(`${base}/#/dashboard`,{waitUntil:'domcontentloaded'})
    try { await page.getByText('CARD-1-0',{exact:true}).waitFor() } catch (e) { console.log(errors, reads, await page.locator('body').innerText()); throw e }
    const cardsMs = Math.round(performance.now()-start)
    assert.equal(await page.locator('.project-card').count(),12)
    try { await page.locator('.project-card').last().locator('.project-stage').first().getByText('50%',{exact:true}).waitFor() } catch(e) { console.log(errors, reads, (await page.locator('body').innerText()).slice(-4000)); throw e }
    assert.ok(!reads.some(r=>r.page==='2'), 'page 2 must not be prefetched')
    assert.ok(streams.every(query=>query.includes('project_id=')), 'no full-user task stream')
    assert.ok(!reads.some(r=>r.path==='/api/v1/tasks'), 'no full task list')
    if (output) await page.screenshot({path:`${output}/project-cards-partial-${viewport.width}.png`})
    await page.getByRole('button',{name:'下一页',exact:true}).click()
    await page.getByText('CARD-2-0',{exact:true}).waitFor()
    try { await page.locator('.project-card').last().locator('.project-stage').first().getByText('50%',{exact:true}).waitFor() } catch(e) { console.log(errors, reads, (await page.locator('body').innerText()).slice(-4000)); throw e }
    assert.ok(reads.some(r=>r.page==='2'))
    assert.equal(await page.locator('.project-card').getByText('CARD-1-0',{exact:true}).count(),0)
    release()
    await page.locator('.project-card').last().locator('.project-stage').nth(3).getByText('50%',{exact:true}).waitFor()
    assert.equal(await page.locator('.project-card').getByText('CARD-1-0',{exact:true}).count(),0)
    assert.deepEqual(errors,[])
    assert.deepEqual(mutations,[])
    await page.getByRole('button',{name:'上一页',exact:true}).click()
    await page.getByRole('button',{name:'删除项目 CARD-1-0',exact:true}).click()
    const activeReads = reads.filter(r=>r.path==='/api/v1/projects/active').length
    await page.getByRole('alertdialog').getByRole('button',{name:'删除项目',exact:true}).click()
    await page.waitForFunction(()=>!document.querySelector('.app-nav__project-title'))
    assert.ok(reads.filter(r=>r.path==='/api/v1/projects/active').length > activeReads, 'delete must sync active context')
    assert.equal(await page.locator('.project-card').getByText('CARD-1-0',{exact:true}).count(),0)
    assert.deepEqual(mutations,['/api/v1/projects/p1-0'])
    assert.deepEqual(errors,[])
    await page.close()
    console.log(`Project cards visible ${cardsMs}ms, light counts and paging usable with audio held: ${viewport.width}×${viewport.height} (fixture/local browser)`)
  }
} finally { await browser.close() }
