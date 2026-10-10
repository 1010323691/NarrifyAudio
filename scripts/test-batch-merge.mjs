// 批量合并疑似角色：图推导（纯函数）与 useBatchMerge 的选中/展开/竞态/冲突回归（node:test + vm 沙箱）。
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const cache = new Map()
function load(name, api) {
  const key = name
  if (!cache.has(key) || name === 'useBatchMerge') {
    const code = ts.transpileModule(readFileSync(new URL(`../src/composables/${name}.ts`, import.meta.url), 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText
    const module = { exports: {} }
    runInNewContext(code, {
      module, exports: module.exports, Intl, DOMException, Array, Object, Set, Map,
      require: id => id === 'vue' ? vue : id === '@/api/tts' ? api : id === './batchMergeGraph' ? load('batchMergeGraph') : {},
    })
    if (name === 'batchMergeGraph') cache.set(key, module.exports)
    else return module.exports
  }
  return cache.get(key)
}
// vm 沙箱里产生的数组与测试领域不同源，先 JSON 往返再比较。
const same = (actual, expected) => assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)), expected)
const G = load('batchMergeGraph')

const role = (name, n = 1) => ({ name, gender: '', line_count: n, sample: `${name}的话`, preview: '', voice_config: true, cloned: false })
const link = target => ({ target, basis: '名字包含' })
const graph = (over = {}) => ({
  version: 1, script: '', roles: ['a', 'b', 'c', 'd', 'e'].map(n => role(n, 10)),
  links: { c: link('a'), d: link('c'), e: link('d') }, records: [], vetoes: [], new_candidates: {}, ...over,
})
const record = (source, target) => ({ id: `${source}-${target}`, source, target, merged_at: '2026-01-01T00:00:00Z', line_count: 3, undoable: true, reason: '', children: [] })

test('targets sort: 待复核 first, then pending desc, zero last, then pinyin', () => {
  const g = graph({
    links: { c: link('a'), d: link('c'), e: link('d'), x: link('张三'), y: link('张三'), z: link('b2') },
    records: [record('m', 'b'), record('n', 'b2')], new_candidates: { b2: ['z'] },
  })
  const names = G.listTargets(g, []).map(e => [e.name, e.pending, e.tag])
  same(names.map(n => n[0]), ['b2', 'a', '张三', 'c', 'd', 'b'])
  assert.equal(G.badgeCount(G.listTargets(g, [])), 5)
})

test('tags: 已合并 vs 待复核 needs both a record and unseen candidates', () => {
  const g = graph({ records: [record('m', 'a')], new_candidates: { a: ['c'], c: ['d'] } })
  const tag = name => G.listTargets(g, []).find(e => e.name === name).tag
  assert.equal(tag('a'), 'review')
  assert.equal(tag('c'), 'none') // new candidates without a record: no tag, only the row marker
})

test('session targets stay listed with 0 pending; merged-away roles disappear', () => {
  const g = graph({ links: {}, records: [record('c', 'a')] })
  const names = G.listTargets(g, ['b', 'c']).map(e => e.name)
  same(names, ['a', 'b'])
  same(G.filterTargets(G.listTargets(g, ['b']), '', true).map(e => e.name), [])
  same(G.filterTargets(G.listTargets(g, ['b']), 'B', false).map(e => e.name), ['b'])
})

test('tree rows: 上游 N counts every level; collapsed rows show 含已选 / 含新增', () => {
  const g = graph({ new_candidates: { a: ['e'] } })
  const rows = G.visibleRows(g, n => role(n), 'a', new Set(), new Set(['e']))
  assert.equal(rows.length, 1)
  same([rows[0].name, rows[0].upstreamCount, rows[0].selectedBelow, rows[0].containsNew, rows[0].isNew], ['c', 2, 1, true, false])
  const open = G.visibleRows(g, n => role(n), 'a', new Set(['c', 'd']), new Set(['e']))
  same(open.map(r => [r.name, r.depth, r.isNew, r.selectedBelow, r.containsNew]), [['c', 0, false, 0, false], ['d', 1, false, 0, false], ['e', 2, true, 0, false]])
})

test('new candidates sort first and their branches default open', () => {
  const g = graph({ links: { c: link('a'), d: link('c'), f: link('a') }, new_candidates: { a: ['d'] } })
  same([...G.defaultExpanded(g, 'a')], ['c'])
  const rows = G.visibleRows(g, n => role(n, n === 'f' ? 99 : 1), 'a', G.defaultExpanded(g, 'a'), new Set())
  same(rows.map(r => r.name), ['c', 'd', 'f'])
})

test('cycles and self links never loop', () => {
  const g = graph({ links: { c: link('a'), a: link('c'), d: link('d') } })
  const rows = G.visibleRows(g, n => role(n), 'a', new Set(['c']), new Set())
  same(rows.map(r => r.name), ['c'])
  assert.equal(G.upstreamAll(G.childrenOf(g), 'a').has('a'), false)
})

test('merge plan: indirect flag, totals and orphans', () => {
  const g = graph()
  const plan = G.mergePlan(g, n => ({ ...role(n, 5), cloned: n === 'c' }), 'a', new Set(['c', 'e']))
  same(plan.items.map(i => [i.name, i.indirect]), [['c', false], ['e', true]])
  assert.equal(plan.lines, 10)
  same(plan.cloned, ['c'])
  same(plan.orphans, ['d']) // d points at the merged c but is not merged itself
})

function setup(overrides = {}) {
  const calls = []
  let version = 1
  const api = {
    getMergeGraph: async () => { calls.push('graph'); return graph({ version }) },
    mergeBatch: async (_s, target, sources, v) => { calls.push(['merge', target, sources, v]); version++; return { graph: graph({ version, links: { d: link('a'), e: link('d') }, records: [record('c', 'a')] }), sources, replaced: 3, rematched: {}, orphans: [], target, files: [] } },
    undoMerge: async () => ({ graph: graph() }),
    vetoLink: async () => ({ graph: graph() }),
    restoreLink: async () => ({ graph: graph() }),
    markMergeReviewed: async (_s, target) => { calls.push(['seen', target]); return { ok: true, cleared: true, version } },
    ...overrides,
  }
  const scope = { current: true }
  const batch = load('useBatchMerge', api).useBatchMerge({ script: '__all__', captureScope: () => { const ok = scope.current; return () => ok && scope.current } })
  return { batch, calls, api, scope }
}

test('clicking a name selects and opens; unselecting keeps it open; arrow only expands', async () => {
  const { batch } = setup()
  await batch.openDialog()
  batch.selectTarget('a')
  batch.toggleSelect('c')
  same([...batch.selected.value], ['c'])
  assert.equal(batch.expanded.value.has('c'), true)
  batch.toggleSelect('c')
  assert.equal(batch.selected.value.size, 0)
  assert.equal(batch.expanded.value.has('c'), true)
  batch.toggleExpand('d') // arrow: expands without selecting
  assert.equal(batch.selected.value.size, 0)
  batch.toggleSelect('e')
  batch.toggleExpand('c') // collapse: selection below survives
  same([...batch.selected.value], ['e'])
  assert.equal(batch.rows.value[0].selectedBelow, 1)
  assert.equal(batch.plan.value.items.length, 1)
})

test('switching target clears selection; leaving marks new candidates reviewed once', async () => {
  const { batch, calls, api } = setup()
  api.getMergeGraph = async () => graph({ records: [record('m', 'a')], new_candidates: { a: ['d'] } })
  await batch.openDialog()
  batch.selectTarget('a')
  assert.equal(batch.newCount.value, 1)
  assert.equal(batch.targets.value.find(e => e.name === 'a').tag, 'review')
  batch.toggleSelect('c')
  batch.selectTarget('b')
  assert.equal(batch.selected.value.size, 0)
  same(calls.filter(c => c[0] === 'seen'), [['seen', 'a']])
  assert.equal(batch.targets.value.find(e => e.name === 'a').tag, 'merged')
  batch.closeDialog()
  assert.equal(calls.filter(c => c[0] === 'seen').length, 1)
})

test('merge sends version + selection, replaces the graph and clears the selection', async () => {
  const { batch, calls } = setup()
  await batch.openDialog()
  batch.selectTarget('a')
  batch.toggleSelect('c')
  const result = await batch.merge()
  same(calls.find(c => c[0] === 'merge'), ['merge', 'a', ['c'], 1])
  assert.equal(result.replaced, 3)
  assert.equal(batch.selected.value.size, 0)
  assert.equal(batch.graph.value.version, 2)
  assert.equal(batch.records.value.length, 1)
  assert.equal(batch.targets.value.find(e => e.name === 'a').tag, 'merged')
})

test('version conflict refetches, prunes vanished selections and reports', async () => {
  const { batch, api } = setup()
  await batch.openDialog()
  batch.selectTarget('a')
  batch.toggleSelect('c')
  api.mergeBatch = async () => { const e = new Error('角色数据已变化，已刷新，请重新确认'); e.status = 409; throw e }
  api.getMergeGraph = async () => graph({ version: 5, links: { d: link('a') } })
  assert.equal(await batch.merge(), null)
  assert.match(batch.error.value, /角色数据已变化/)
  assert.equal(batch.graph.value.version, 5)
  assert.equal(batch.selected.value.size, 0)
  assert.equal(batch.busy.value, false)
})

test('late responses after a scope switch or reset are discarded', async () => {
  let release
  const gate = new Promise(r => { release = r })
  const { batch, scope } = setup({ getMergeGraph: async () => { await gate; return graph({ version: 9 }) } })
  const pending = batch.refresh()
  scope.current = false
  release(); await pending
  assert.equal(batch.graph.value, null)

  const second = setup({ getMergeGraph: async () => { await gate; return graph({ version: 9 }) } })
  const opening = second.batch.openDialog()
  second.batch.reset()
  await opening
  assert.equal(second.batch.graph.value, null)
  assert.equal(second.batch.open.value, false)
})

test('a merge reply for a switched project never lands', async () => {
  let release
  const gate = new Promise(r => { release = r })
  const { batch, scope, api } = setup()
  await batch.openDialog()
  batch.selectTarget('a'); batch.toggleSelect('c')
  const base = api.mergeBatch
  api.mergeBatch = async (...args) => { await gate; return base(...args) }
  const request = batch.merge()
  scope.current = false
  release()
  assert.equal(await request, null)
  assert.equal(batch.graph.value.version, 1)
})

test('hasAny follows links or records; badge counts targets with pending roles', async () => {
  const { batch, api } = setup()
  assert.equal(batch.hasAny.value, false)
  await batch.refresh(true)
  assert.equal(batch.hasAny.value, true)
  assert.equal(batch.badge.value, 3)
  api.getMergeGraph = async () => graph({ links: {}, records: [], version: 2 })
  await batch.refresh(true)
  assert.equal(batch.hasAny.value, false)
})

test('a stale refresh reply never rolls the graph back; list reloads are throttled', async () => {
  let release
  const gate = new Promise(r => { release = r })
  let calls = 0
  const { batch, api } = setup()
  await batch.openDialog()
  batch.selectTarget('a'); batch.toggleSelect('c')
  const base = api.getMergeGraph
  api.getMergeGraph = async () => { calls++; await gate; return graph({ version: 1 }) }
  const stale = batch.refresh(true)
  api.getMergeGraph = base
  await batch.merge() // graph is now version 2
  release(); await stale
  assert.equal(batch.graph.value.version, 2)
  api.getMergeGraph = async () => { calls++; return graph({ version: 9 }) }
  await batch.refresh() // within the throttle window: no request
  assert.equal(calls, 1)
})
