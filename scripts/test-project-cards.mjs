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
  const id = vue.ref(['p'])
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
  const source = readFileSync(new URL('../src/composables/useProjectCardProgress.ts', import.meta.url), 'utf8')
  const module = { exports: {} }
  runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    module, exports: module.exports, require: name => mocks[name], AbortController, document: doc,
    setTimeout: (fn, delay) => { const token = Symbol(); timers.set(token, { fn, delay }); return token }, clearTimeout: token => timers.delete(token),
  })
  const scope = vue.effectScope()
  const c = scope.run(() => module.exports.useProjectCardProgress(id))
  return { c, calls, auth, id, doc, timers, activate: () => { hooks.mounted.forEach(fn => fn()); hooks.activated.forEach(fn => fn()) },
    deactivate: () => hooks.deactivated.forEach(fn => fn()), reactivate: () => hooks.activated.forEach(fn => fn()),
    hide: () => { doc.hidden = true; doc.listeners.get('visibilitychange')?.() }, show: () => { doc.hidden = false; doc.listeners.get('visibilitychange')?.() },
    tick: async () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(item => item.fn()); await flush() }, close: () => { hooks.unmounted.forEach(fn => fn()); scope.stop() } }
}
test('cards render progress slots without full snapshots or full progress reads', () => {
  const source = readFileSync(new URL('../src/views/Dashboard.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /setTaskCenterOpen|listDurableTasks|useTaskStore|getProjectProgressSummary/)
  const { descriptor } = parse(source)
  assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: 'Dashboard.vue', id: 'cards' }).errors, [])
  assert.doesNotMatch(source, /v-else class="project-empty"/)
})
test('a slow audio project cannot block other cards and audio concurrency is one', async () => {
  const audio = deferred()
  const h = harness({ progress: (_, __, section) => section === 'production' ? audio.promise : Promise.resolve(data) })
  try {
    h.id.value = ['p', 'q', 'r']; h.activate(); await flush()
    assert.equal(h.c.summaries.value.q.stage_completion['02_split_text'].percent, 50)
    assert.equal(h.c.tasks.value.r.failure_count, 0)
    assert.equal(h.calls.filter(call => call.section === 'production').length, 1)
    assert.equal(h.calls.length, 10)
    audio.resolve({ stage_completion: {} }); await flush()
    assert.equal(h.calls.length, 12)
    assert.ok(h.calls.every(call => call.section))
  } finally { h.close() }
})
test('request concurrency is bounded and refresh cannot duplicate in-flight reads', async () => {
  const pending = deferred()
  const h = harness({ progress: () => pending.promise, tasks: () => pending.promise })
  try {
    h.id.value = Array.from({length:12}, (_, i) => String(i)); h.activate(); await flush()
    assert.equal(h.calls.length, 4)
    h.c.refresh(); await flush(); assert.equal(h.calls.length, 4)
    h.deactivate(); assert.ok(h.calls.every(call => call.signal.aborted))
  } finally { h.close() }
})
test('page and account changes abort and discard stale responses, logout stops requests', async () => {
  const pending = deferred()
  const h = harness({ progress: project => project === 'p' ? pending.promise : Promise.resolve(data) })
  try {
    h.activate(); await flush(); h.id.value = ['q']; await flush()
    assert.ok(h.calls.filter(call => call.project === 'p' && call.section !== 'tasks').every(call => call.signal.aborted))
    pending.resolve(data); await flush()
    assert.equal(h.c.summaries.value.p, undefined)
    assert.equal(h.c.summaries.value.q.stage_completion['02_split_text'].percent, 50)
    h.auth.user = null; await flush()
    assert.equal(Object.keys(h.c.summaries.value).length, 0)
    assert.equal(h.timers.size, 0)
    const count = h.calls.length; h.c.refresh(); assert.equal(h.calls.length, count)
    h.auth.user = { id: 'other' }; await flush()
    assert.equal(h.c.tasks.value.q.failure_count, 0)
  } finally { h.close() }
})
test('failed refresh retains data and exposes a card retry, hide and deactivate stop polling', async () => {
  let fail = false
  const h = harness({ progress: (_, __, section) => section === 'text' && fail ? Promise.reject(new Error('offline')) : Promise.resolve(data) })
  try {
    h.activate(); await flush(); fail = true; await h.tick()
    assert.equal(h.c.summaries.value.p.stage_completion['02_split_text'].percent, 50)
    assert.equal(h.c.errors.value.p.text, 'offline')
    fail = false; h.c.refresh('p'); await flush()
    assert.equal(h.c.errors.value.p.text, undefined)
    h.hide(); assert.equal(h.timers.size, 0)
    const count = h.calls.length; await h.tick(); assert.equal(h.calls.length, count)
    h.show(); await flush(); assert.equal(h.calls.length, count + 4)
    h.deactivate(); assert.equal(h.timers.size, 0)
    h.reactivate(); await flush(); assert.equal(h.calls.length, count + 8)
  } finally { h.close() }
})
test('polling an earlier audio card cannot starve later cards in the initial queue', async () => {
  const pending = []
  const h = harness({ progress: (project, signal, section) => {
    if (section !== 'production') return Promise.resolve(data)
    const request = deferred(); pending.push(request); return request.promise
  } })
  try {
    h.id.value = ['p', 'q', 'r', 's']; h.activate(); await flush()
    pending[0].resolve(data); await flush()
    // Queue the first card again while the second card is still calculating.
    await h.tick()
    pending[1].resolve(data); await flush()
    assert.deepEqual(h.calls.filter(call => call.section === 'production').map(call => call.project), ['p','q','r'])
    pending[2].resolve(data); await flush()
    assert.deepEqual(h.calls.filter(call => call.section === 'production').map(call => call.project), ['p','q','r','s'])
  } finally { h.close() }
})
test('active-context refresh after deletion cannot overwrite a new login or project selection', async () => {
  const source = readFileSync(new URL('../src/views/Dashboard.vue', import.meta.url), 'utf8')
  const fn = source.slice(source.indexOf('async function syncActiveProject('), source.indexOf('async function load(force'))
  for (const change of ['none', 'account', 'selection', 'busy']) {
    const pending = deferred(), applied = []
    const auth = {user:{id:'u'}}, projectStore = {current:{project_id:'deleted'},busy:false,setCurrent:value=>applied.push(value)}
    const module = {exports:{}}
    runInNewContext(ts.transpileModule(fn + '\nexports.sync = syncActiveProject', {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText, {
      module,exports:module.exports,auth,projectStore,getActiveProject:()=>pending.promise,
    })
    const request = module.exports.sync()
    if (change === 'account') auth.user = {id:'other'}
    if (change === 'selection') projectStore.current = {project_id:'new'}
    if (change === 'busy') projectStore.busy = true
    pending.resolve({set:false,project_id:''}); await request
    assert.equal(applied.length, change === 'none' ? 1 : 0, change)
  }
})
test('deleting an active project still synchronizes shared context after navigation away', async () => {
  const source = readFileSync(new URL('../src/views/Dashboard.vue', import.meta.url), 'utf8')
  const sync = source.slice(source.indexOf('async function syncActiveProject('), source.indexOf('async function createProject()'))
  const remove = source.slice(source.indexOf('async function requestDelete('), source.indexOf('function activate()'))
  const pending = deferred(), applied = [], reads = []
  const module = {exports:{}}
  const context = {
    module,exports:module.exports,auth:{user:{id:'u'}},document:{hidden:false},viewActive:true,
    projectStore:{current:{project_id:'deleted'},busy:false,setCurrent:value=>applied.push(value)},
    deletingProjectId:{value:''},showConfirm:()=>Promise.resolve(true),deleteProject:()=>pending.promise,
    getActiveProject:()=>{reads.push('active');return Promise.resolve({set:false,project_id:''})},
    loadProjectPage:()=>{reads.push('page');return Promise.resolve()},toast:()=>{},
  }
  runInNewContext(ts.transpileModule(sync + remove + '\nexports.remove = requestDelete', {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText, context)
  const request = module.exports.remove({id:'deleted',name:'book'})
  await flush(); context.viewActive = false
  pending.resolve({ok:true}); await request
  assert.deepEqual(reads,['active'])
  assert.equal(applied.length,1)
  assert.equal(context.deletingProjectId.value,'')
})
