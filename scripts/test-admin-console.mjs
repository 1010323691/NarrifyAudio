import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'
const require = createRequire(import.meta.url)
const vue = require('vue')

// Load real admin console code (the shared loader composable plus a view's
// <script setup>) into a vm sandbox with stubbed lifecycle hooks and APIs.
function source(path) {
  const raw = readFileSync(new URL(path, import.meta.url), 'utf8')
  return path.endsWith('.vue') ? raw.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1] : raw
}
function compile(code) {
  return ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

function harness(view, exports, api = {}, { query = {} } = {}) {
  const deactivated = []
  const mounted = []
  const toasts = []
  const timers = new Map()
  let nextTimer = 0
  const context = {
    AbortController, document: { visibilityState: 'visible' },
    setInterval(fn) { timers.set(++nextTimer, fn); return nextTimer }, clearInterval(id) { timers.delete(id) },
  }
  const vueStub = { ...vue, onMounted(fn) { mounted.push(fn) }, onActivated() {}, onDeactivated(fn) { deactivated.push(fn) }, onBeforeUnmount() {} }
  function load(code, requireFn) {
    const module = { exports: {} }
    runInNewContext(compile(code), { ...context, module, exports: module.exports, require: requireFn })
    return module.exports
  }
  const loaderModule = load(source('../src/composables/useAdminLoader.ts'), name => {
    if (name === 'vue') return vueStub
    throw new Error(`unexpected import ${name}`)
  })
  const module = load(source(view) + `\nexport { ${exports.join(', ')} };`, name => {
    if (name === 'vue') return vueStub
    if (name === 'vue-router') return { useRoute: () => vue.reactive({ query }) }
    if (name === '@/composables/useAdminLoader') return loaderModule
    if (name === '@/stores/clientDisplay') return { useClientDisplayStore: () => ({ load: async () => {}, save: async () => true, logsEnabled: false }) }
    if (name === '@/components/ui/toast') return { useToast: () => ({ push(item) { toasts.push(item) } }) }
    if (name === '@/components/ui/dialog') return { showConfirm: async () => true }
    if (name === '@/api/admin') return {
      userPage: async () => ({ items: [], pagination: { total: 0, page: 1, page_size: 20, counts: {} } }),
      getStorageSettings: async () => ({ root_path: '' }), getQuotaSettings: async () => ({ initial_units: 0 }),
      getRegistrationSettings: async () => ({ enabled: true }), getRuntimeSettings: async () => ({}),
      getOverview: async () => ({ services: [] }), getTaskMetrics: async () => ({}), getThroughput: async () => ({}), getMetricsHistory: async () => null,
      ...api,
    }
    if (name.startsWith('@/utils/')) return new Proxy({}, { get: () => () => '' })
    return { default: {} }
  })
  return {
    ...module, loaderModule, toasts,
    mount: () => mounted.forEach(fn => fn()),
    deactivate: () => deactivated.forEach(fn => fn()),
    poll: () => [...timers.values()].forEach(fn => fn()),
    timerCount: () => timers.size,
  }
}
const USERS = ['loader', 'users', 'usersPagination', 'userSearch', 'userRole', 'userState', 'userSort', 'userPage', 'lastAdmin']
const users = (api, options) => harness('../src/views/admin/AdminUsers.vue', USERS, api, options)
const CONFIG = ['loader', 'registration', 'toggleRegistration', 'saveToggle', 'savingToggle', 'submitQuota', 'savingQuota']
const config = api => harness('../src/views/admin/AdminConfig.vue', CONFIG, api)
const page = items => ({ items, pagination: { total: items.length, page: 1, page_size: 20, counts: { active_admins: 1 } } })

test('a late refresh cannot overwrite the newest user data', async () => {
  const old = deferred(); let calls = 0
  const h = users({ userPage: () => ++calls === 1 ? old.promise : Promise.resolve(page([{ id: 'new' }])) })
  const pending = h.loader.load(); await h.loader.load(); old.resolve(page([{ id: 'old' }])); await pending
  assert.equal(h.users.value[0].id, 'new'); assert.equal(h.loader.loading.value, false)
})

test('leaving a cached view invalidates its in-flight response', async () => {
  const old = deferred(); const h = users({ userPage: () => old.promise })
  const pending = h.loader.load(); h.deactivate(); old.resolve(page([{ id: 'old' }])); await pending
  assert.equal(h.users.value.length, 0); assert.equal(h.loader.loaded.value, false); assert.equal(h.loader.loading.value, false)
})

test('failed first loads expose retry feedback without displaying an empty success state', async () => {
  const h = users({ userPage: async () => { throw new Error('unavailable') } }); await h.loader.load()
  assert.equal(h.loader.error.value, 'unavailable'); assert.equal(h.loader.loading.value, false); assert.equal(h.loader.loaded.value, false)
})

test('filters are sent to the server and reset pagination to the first page', async () => {
  const calls = []
  const h = users({ userPage: async options => { calls.push({ ...options }); return page([]) } })
  await h.loader.load(); h.userPage.value = 3; await vue.nextTick(); await new Promise(resolve => setImmediate(resolve))
  h.userRole.value = 'admin'; h.userSearch.value = 'alice'; await vue.nextTick(); await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.userPage.value, 1)
  assert.deepEqual(calls.at(-1), { page: 1, page_size: 10, search: 'alice', role: 'admin', state: 'all', sort: 'default' })
})

test('mutations cannot run twice while a confirmation or request is pending', async () => {
  const wait = deferred(); const h = users(); let calls = 0
  const action = async () => { calls++; await wait.promise }
  const first = h.loader.runAction(action); await h.loader.runAction(action)
  assert.equal(calls, 1); assert.equal(h.loader.actionBusy.value, true)
  wait.resolve(); await first; assert.equal(h.loader.actionBusy.value, false)
})

test('the final active administrator remains protected', () => {
  const h = users(); const admin = { role: 'admin', is_active: true }
  h.usersPagination.value = { total: 1, page: 1, page_size: 20, counts: { active_admins: 1 } }
  assert.equal(h.lastAdmin(admin), true)
  h.usersPagination.value = { total: 2, page: 1, page_size: 20, counts: { active_admins: 2 } }
  assert.equal(h.lastAdmin(admin), false)
  assert.equal(h.lastAdmin({ role: 'admin', is_active: false }), false)
})

test('registration saves block external refreshes and keep the next toggle direction', async () => {
  const save = deferred(); let enabled = true; let reads = 0
  const h = config({
    getRegistrationSettings: async () => { reads++; return { enabled } },
    updateRegistrationSettings: async next => { await save.promise; enabled = next; return { enabled } },
  })
  await h.loader.load()
  const pending = h.loader.runAction(h.toggleRegistration)
  await h.loader.load(); assert.equal(reads, 1)
  save.resolve(); await pending
  assert.equal(h.registration.value.enabled, false)
  await h.loader.runAction(h.toggleRegistration)
  assert.equal(h.registration.value.enabled, true)
})

test('a settings read begun before a mutation cannot restore the previous registration value', async () => {
  const old = deferred(); let enabled = true; let reads = 0
  const h = config({
    getRegistrationSettings: () => ++reads === 2 ? old.promise : Promise.resolve({ enabled }),
    updateRegistrationSettings: async next => { enabled = next; return { enabled } },
  })
  await h.loader.load()
  const pending = h.loader.load()
  await vue.nextTick()
  await h.loader.runAction(h.toggleRegistration)
  old.resolve({ enabled: true }); await pending
  assert.equal(h.registration.value.enabled, false)
  assert.equal(h.loader.loading.value, false)
})

test('immediate switches retain confirmed state and own their pending feedback', async () => {
  const save = deferred(); let calls = 0; let reads = 0
  const h = config({
    getRegistrationSettings: async () => { reads++; return { enabled: true } },
    updateRegistrationSettings: () => { calls++; return save.promise },
  })
  await h.loader.load()
  const pending = h.saveToggle('registration')
  assert.equal(h.savingToggle.value, 'registration')
  await h.submitQuota()
  assert.equal(h.savingQuota.value, false)
  assert.equal(h.registration.value.enabled, true)
  await h.saveToggle('logs'); await h.saveToggle('registration')
  assert.equal(calls, 1); assert.equal(h.savingToggle.value, 'registration')
  save.resolve({ enabled: false }); await pending
  assert.equal(h.registration.value.enabled, false)
  assert.equal(h.savingToggle.value, null); assert.equal(h.loader.actionBusy.value, false)
  assert.equal(reads, 1)
})

test('failed immediate saves restore interaction, keep the confirmed state and raise a toast', async () => {
  const h = config({
    getRegistrationSettings: async () => ({ enabled: true }),
    updateRegistrationSettings: async () => { throw new Error('save unavailable') },
  })
  await h.loader.load(); await h.saveToggle('registration')
  assert.equal(h.registration.value.enabled, true)
  assert.equal(h.loader.error.value, 'save unavailable')
  assert.equal(h.savingToggle.value, null); assert.equal(h.loader.actionBusy.value, false)
  assert.equal(h.toasts.at(-1).description, 'save unavailable')
})

test('slow monitoring responses survive multiple polling ticks', async () => {
  const response = deferred(); let calls = 0
  const h = harness('../src/views/admin/AdminOverview.vue', ['loader', 'overview'], { getOverview: () => { calls++; return response.promise } })
  h.mount(); h.poll(); h.poll(); h.poll()
  assert.equal(calls, 1)
  response.resolve({ services: [] })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(h.loader.loaded.value, true)
  assert.equal(h.loader.loading.value, false)
  h.poll(); assert.equal(calls, 2)
})

test('turning auto-refresh off stops polling and the choice is shared by every view', async () => {
  const h = harness('../src/views/admin/AdminOverview.vue', ['loader'])
  h.mount(); assert.equal(h.timerCount(), 1)
  h.loaderModule.refreshInterval.value = 0; await vue.nextTick()
  assert.equal(h.timerCount(), 0)
  h.loaderModule.refreshInterval.value = 30000; await vue.nextTick()
  assert.equal(h.timerCount(), 1)
})

test('non-polling views never schedule a refresh timer', () => {
  const h = users(); h.mount()
  assert.equal(h.timerCount(), 0)
})
