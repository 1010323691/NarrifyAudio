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
const data = { project_id: 'p', stage_completion: { '02_split_text': { completed: 1, total: 2, unit: '章节', percent: 50 } } }
function harness(overrides = {}) {
  const auth = vue.reactive({ user: { id: 'u' } })
  const id = vue.ref('p')
  const hooks = { mounted: [], activated: [], deactivated: [], unmounted: [] }
  const timers = new Map()
  const doc = { hidden: false, listeners: new Map(), addEventListener(name, fn) { this.listeners.set(name, fn) }, removeEventListener(name) { this.listeners.delete(name) } }
  const calls = []
  const mocks = {
    vue: { ...vue, onMounted: fn => hooks.mounted.push(fn), onActivated: fn => hooks.activated.push(fn), onDeactivated: fn => hooks.deactivated.push(fn), onUnmounted: fn => hooks.unmounted.push(fn) },
    '@/stores/auth': { useAuthStore: () => auth },
    '@/api/project': { getProjectProgressSummary: (project, signal, section) => { calls.push({ project, signal, section }); return overrides.progress?.(project, signal, section) ?? Promise.resolve(section === 'text' ? data : { stage_completion: {} }) } },
    '@/api/tasks': { getProjectOverviewTasks: (project, signal) => { calls.push({ project, signal, section: 'tasks' }); return overrides.tasks?.(project, signal) ?? Promise.resolve({ statuses: [], failures: [], failure_count: 0 }) } },
  }
  const source = readFileSync(new URL('../src/composables/useProjectOverview.ts', import.meta.url), 'utf8')
  const module = { exports: {} }
  runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    module, exports: module.exports, require: name => mocks[name], AbortController, document: doc,
    setTimeout: (fn, delay) => { const token = Symbol(); timers.set(token, { fn, delay }); return token }, clearTimeout: token => timers.delete(token),
  })
  const scope = vue.effectScope()
  const c = scope.run(() => module.exports.useProjectOverview(id))
  return { c, calls, auth, id, doc, timers, activate: () => { hooks.mounted.forEach(fn => fn()); hooks.activated.forEach(fn => fn()) },
    deactivate: () => hooks.deactivated.forEach(fn => fn()), reactivate: () => hooks.activated.forEach(fn => fn()),
    hide: () => { doc.hidden = true; doc.listeners.get('visibilitychange')?.() }, show: () => { doc.hidden = false; doc.listeners.get('visibilitychange')?.() },
    tick: async () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(item => item.fn()); await flush() }, close: () => { hooks.unmounted.forEach(fn => fn()); scope.stop() } }
}
test('overview navigation renders without an all-page loading gate or full task subscription', () => {
  const source = readFileSync(new URL('../src/views/ProjectOverview.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /setTaskCenterOpen|listDurableTasks|useTaskStore|Promise.allSettled/)
  const { descriptor } = parse(source)
  const result = compileTemplate({ source: descriptor.template.content, filename: 'ProjectOverview.vue', id: 'overview' })
  assert.deepEqual(result.errors, [])
  assert.doesNotMatch(source, /v-if="loading"[^>]*[\s\S]{0,180}正在读取制作进度/)
})
test('slow audio progress cannot block text counts, task status, or independent refresh', async () => {
  const slow = deferred()
  const h = harness({ progress: (_, __, section) => section === 'production' ? slow.promise : Promise.resolve(section === 'text' ? data : { stage_completion: {} }) })
  try {
    h.activate(); await flush()
    assert.equal(h.calls.length, 4)
    assert.equal(h.c.completion.value['02_split_text'].percent, 50)
    assert.equal(h.c.tasks.value.failure_count, 0)
    h.c.refresh(); await flush()
    assert.equal(h.calls.filter(call => call.section === 'production').length, 1)
    assert.equal(h.timers.size, 3)
    slow.resolve({ stage_completion: {} }); await flush()
    assert.equal(h.timers.size, 4)
  } finally { h.close() }
})
test('refresh failure retains existing data and exposes an independent retry', async () => {
  let fail = false
  const h = harness({ progress: (_, __, section) => section === 'text' ? fail ? Promise.reject(new Error('offline')) : Promise.resolve(data) : Promise.resolve({ stage_completion: {} }) })
  try {
    h.activate(); await flush(); fail = true
    await h.tick()
    assert.equal(h.c.completion.value['02_split_text'].percent, 50)
    assert.equal(h.c.errors.value.length, 1)
    fail = false
    await h.c.errors.value[0].retry(); await flush()
    assert.equal(h.c.errors.value.length, 0)
  } finally { h.close() }
})
test('project and account changes abort and discard old responses', async () => {
  const slow = deferred()
  const h = harness({ progress: project => project === 'p' ? slow.promise : Promise.resolve({ stage_completion: {} }) })
  try {
    h.activate(); h.id.value = 'new'; await flush()
    assert.ok(h.calls.slice(1, 4).every(call => call.signal.aborted))
    slow.resolve(data); await flush()
    assert.equal(h.c.completion.value['02_split_text'], undefined)
    h.auth.user = null; await flush()
    assert.equal(h.timers.size, 0)
    assert.equal(h.c.tasks.value, null)
    h.auth.user = { id: 'other' }; await flush()
    assert.equal(h.c.tasks.value.failure_count, 0)
  } finally { h.close() }
})
test('hide and deactivate stop timers, cancel requests and resume immediately', async () => {
  const h = harness()
  try {
    h.activate(); await flush()
    h.hide(); assert.equal(h.timers.size, 0)
    await h.tick(); assert.equal(h.calls.length, 4)
    h.show(); await flush(); assert.equal(h.calls.length, 8)
    h.deactivate(); assert.equal(h.timers.size, 0)
    h.c.refresh(); assert.equal(h.calls.length, 8)
    h.reactivate(); await flush(); assert.equal(h.calls.length, 12)
  } finally { h.close() }
})
