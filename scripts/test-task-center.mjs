import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const { parse, compileTemplate } = require('@vue/compiler-sfc')
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }
const group = (id = 'book', count = 125) => ({ project_id: id, project_name: id, task_count: count, succeeded_count: 0, active_count: count, pausable_count: count, resumable_count: 0, latest: 1, latest_status: 'pending' })
const page = (items, size = 5, total = items.length) => ({ items, total, page: 1, page_size: size })
const summary = { items: [{ category: 'script', project_count: 1, task_count: 125, active_count: 125 }] }

function harness(overrides = {}) {
  const hooks = { activated: [], deactivated: [], unmounted: [] }
  const auth = vue.reactive({ user: { id: 'user-a' } })
  const calls = { summary: [], groups: [], items: [], control: [] }
  const document = { hidden: false, listeners: new Map(), addEventListener(name, fn) { this.listeners.set(name, fn) }, removeEventListener(name) { this.listeners.delete(name) } }
  const timers = new Map()
  let nextTimer = 1
  const defaults = {
    getTaskCenterSummary: async () => summary,
    getTaskCenterGroups: async () => page([group()]),
    getTaskCenterItems: async () => ({ ...page([{ id: 'first' }], 50, 125), counts: group() }),
    controlTaskCenterGroup: async () => ({ changed: 125 }),
    ...overrides,
  }
  const api = {}
  for (const [name, kind] of [['getTaskCenterSummary', 'summary'], ['getTaskCenterGroups', 'groups'], ['getTaskCenterItems', 'items'], ['controlTaskCenterGroup', 'control']]) {
    api[name] = (...args) => { calls[kind].push(args); return defaults[name](...args) }
  }
  const mocks = {
    vue: { ...vue, onActivated: fn => hooks.activated.push(fn), onDeactivated: fn => hooks.deactivated.push(fn), onBeforeUnmount: fn => hooks.unmounted.push(fn) },
    '@/stores/auth': { useAuthStore: () => auth },
    '@/api/tasks': api,
  }
  const modules = new Map()
  function load(name) {
    if (mocks[name]) return mocks[name]
    if (!name.startsWith('@/')) return require(name)
    if (modules.has(name)) return modules.get(name).exports
    const module = { exports: {} }
    modules.set(name, module)
    const source = readFileSync(new URL(`../src/${name.slice(2)}.ts`, import.meta.url), 'utf8')
    runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
      module, exports: module.exports, require: load, document, AbortController,
      setTimeout: (fn, delay) => { const id = nextTimer++; timers.set(id, { fn, delay }); return id },
      clearTimeout: id => timers.delete(id),
    })
    return module.exports
  }
  const scope = vue.effectScope()
  const center = scope.run(() => load('@/composables/useTaskCenter').useTaskCenter())
  return {
    center, calls, auth, document, timers, activate: () => hooks.activated.forEach(fn => fn()),
    deactivate: () => hooks.deactivated.forEach(fn => fn()),
    tick: async () => { const entries = [...timers.values()]; timers.clear(); entries.forEach(value => value.fn()); await flush() },
    hide: () => { document.hidden = true; document.listeners.get('visibilitychange')?.() },
    show: () => { document.hidden = false; document.listeners.get('visibilitychange')?.() },
    close: () => { hooks.unmounted.forEach(fn => fn()); scope.stop() },
  }
}

test('page compiles and always renders navigation without a whole-page loading gate', () => {
  const source = readFileSync(new URL('../src/views/TaskCenter.vue', import.meta.url), 'utf8')
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'TaskCenter.vue', id: 'tasks' }).errors, [])
  assert.doesNotMatch(source, /setTaskCenterOpen|taskStore|listTaskHistory|AUTO_HISTORY_LIMIT/)
  assert.match(source, /v-else-if="items.items.length"/)
  assert.match(source, /v-for="task in items.items"/)
})

test('initial load reads summary then a single group page; delayed items cannot block first-level content', async () => {
  const initial = deferred(), details = deferred()
  const h = harness({ getTaskCenterSummary: () => initial.promise, getTaskCenterItems: () => details.promise })
  try {
    h.activate()
    assert.equal(h.center.sections.value.length, 8)
    assert.equal(h.calls.groups.length, 0)
    assert.equal(h.calls.items.length, 0)
    initial.resolve(summary)
    await flush()
    assert.equal(h.calls.groups.length, 1)
    assert.equal(h.calls.items.length, 0)
    h.center.openGroup(group())
    assert.equal(h.center.selectedCounts.value.pausable_count, 125)
    assert.equal(h.center.items.value, null)
    assert.equal(h.center.groups.value.items[0].project_id, 'book')
    assert.equal(h.calls.items.length, 1)
    details.resolve({ ...page([{ id: 'task' }], 50, 125), counts: group() })
    await flush()
    assert.equal(h.calls.items.length, 1)
    assert.equal(h.center.items.value.total, 125)
  } finally { h.close() }
})

test('default category prefers activity, then history', async () => {
  for (const active of [true, false]) {
    const h = harness({ getTaskCenterSummary: async () => ({ items: [
      { category: 'script', project_count: 1, task_count: 2, active_count: 0 },
      { category: 'tts', project_count: 1, task_count: 1, active_count: active ? 1 : 0 },
    ] }) })
    try {
      h.activate(); await flush()
      assert.equal(h.center.browsingSection.value.id, active ? 'tts' : 'script')
    } finally { h.close() }
  }
})

test('switching category cancels old requests and caches only the matching page', async () => {
  const old = deferred()
  const h = harness({ getTaskCenterGroups: category => category === 'script' ? old.promise : Promise.resolve(page([group('tts')])) })
  try {
    h.activate(); await flush()
    const signal = h.calls.groups[0][2]
    h.center.selectCategory('tts'); await flush()
    assert.equal(signal.aborted, true)
    assert.equal(h.center.groups.value.items[0].project_id, 'tts')
    old.resolve(page([group('old-script')])); await flush()
    assert.equal(h.center.groups.value.items[0].project_id, 'tts')
    h.center.selectCategory('script')
    assert.equal(h.center.groups.value, null)
    h.center.selectCategory('tts')
    assert.equal(h.center.groups.value.items[0].project_id, 'tts')
  } finally { h.close() }
})

test('account changes clear cached content and discard an uncooperative late response', async () => {
  const old = deferred()
  let n = 0
  const h = harness({ getTaskCenterGroups: () => ++n === 1 ? old.promise : Promise.resolve(page([group('user-b-book')])) })
  try {
    h.activate(); await flush()
    h.auth.user = { id: 'user-b' }
    assert.equal(h.center.summary.value, null)
    assert.equal(h.center.groups.value, null)
    assert.equal(h.calls.groups[0][2].aborted, true)
    await flush()
    old.resolve(page([group('user-a-book')])); await flush()
    assert.equal(h.center.groups.value.items[0].project_id, 'user-b-book')
    h.auth.user = null
    assert.equal(h.center.summary.value, null)
    assert.equal(h.center.groups.value, null)
    assert.equal(h.timers.size, 0)
  } finally { h.close() }
})

test('local refresh failure preserves data, other regions update, retry recovers', async () => {
  let fail = false
  const h = harness({ getTaskCenterGroups: async () => { if (fail) throw new Error('offline'); return page([group()]) } })
  try {
    h.activate(); await flush()
    h.center.openGroup(group()); await flush()
    fail = true
    await h.tick()
    assert.equal(h.center.groups.value.items[0].project_id, 'book')
    assert.equal(h.center.groupsError.value, 'offline')
    assert.equal(h.calls.summary.length, 2)
    assert.equal(h.calls.items.length, 2)
    fail = false
    await h.center.refreshGroups()
    assert.equal(h.center.groupsError.value, '')
    assert.ok([...h.timers.values()].every(timer => timer.delay === 5000))
  } finally { h.close() }
})

test('pending refreshes never overlap, hidden or deactivated pages cancel polling', async () => {
  const wait = deferred()
  const h = harness({ getTaskCenterSummary: () => wait.promise })
  try {
    h.activate()
    h.center.refreshAll(); h.center.refreshAll()
    assert.equal(h.calls.summary.length, 1)
    h.hide()
    assert.equal(h.calls.summary[0][0].aborted, true)
    assert.equal(h.timers.size, 0)
    wait.resolve(summary); await flush()
    assert.equal(h.center.summary.value, null)
    h.show(); await flush()
    assert.equal(h.calls.summary.length, 2)
    h.deactivate()
    assert.equal(h.timers.size, 0)
    assert.equal(h.document.listeners.size, 0)
    h.center.refreshAll(); await flush()
    assert.equal(h.calls.summary.length, 2)
  } finally { h.close() }
})

test('task pages and filters are bounded, clamp empty pages and discard closed dialog requests', async () => {
  const h = harness({ getTaskCenterItems: async (category, project, filter, index) => ({
    ...page([{ id: `page-${index}-${filter}` }], 50, 51), page: index, counts: group(),
  }) })
  try {
    h.activate(); await flush()
    h.center.openGroup(group()); await flush()
    h.center.taskPage.value = 2; await flush()
    assert.equal(h.center.items.value.items.length, 1)
    assert.equal(h.center.items.value.items[0].id, 'page-2-all')
    h.center.selectFilter('active'); await flush()
    assert.equal(h.center.taskPage.value, 1)
    assert.equal(h.calls.items.at(-1)[2], 'active')
    h.center.taskPage.value = 4; await flush()
    assert.equal(h.center.taskPage.value, 2)
    assert.equal(h.calls.items.at(-1)[3], 2)
    h.center.closeGroup()
    assert.equal(h.center.items.value, null)
    const before = h.calls.items.length
    await h.tick()
    assert.equal(h.calls.items.length, before)
  } finally { h.close() }
})

test('batch controls target the whole group and immediately refresh all loaded regions', async () => {
  const h = harness()
  try {
    h.activate(); await flush()
    h.center.openGroup(group()); await flush()
    assert.equal(h.center.selectedCounts.value.pausable_count, 125)
    await h.center.control('pause'); await flush()
    assert.deepEqual(h.calls.control[0].slice(0, 3), ['book', 'script', 'pause'])
    assert.equal(h.calls.summary.length, 2)
    assert.equal(h.calls.groups.length, 2)
    assert.equal(h.calls.items.length, 2)
    assert.equal(h.center.controllingCategory.value, null)
  } finally { h.close() }
})

test('closing a pending dialog discards its response and a late control cannot affect the next account', async () => {
  const details = deferred(), control = deferred()
  const h = harness({ getTaskCenterItems: () => details.promise, controlTaskCenterGroup: () => control.promise })
  try {
    h.activate(); await flush()
    h.center.openGroup(group())
    const signal = h.calls.items[0][4]
    h.center.closeGroup()
    assert.equal(signal.aborted, true)
    details.resolve({ ...page([{ id: 'late' }], 50), counts: group() })
    await flush()
    assert.equal(h.center.items.value, null)
    h.center.openGroup(group()); await flush()
    const pending = h.center.control('cancel')
    const controlSignal = h.calls.control[0][3]
    h.auth.user = { id: 'user-b' }
    await flush()
    const reads = h.calls.summary.length
    control.resolve({ changed: 125 }); await pending; await flush()
    assert.equal(controlSignal.aborted, true)
    assert.equal(h.calls.summary.length, reads)
    assert.equal(h.center.selectedGroup.value, null)
    assert.equal(h.center.controlError.value, '')
  } finally { h.close() }
})
