import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const source = readFileSync(new URL('../src/components/settings/GpuScheduler.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const compiled = ts.transpileModule(source + '\nexport { draft, status, error, refreshError, busy, message, refresh, save, recover };', {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const config = () => ({ enabled: false, min_service_runtime: 300, max_wait_time: 300, llm_start_script_path: 'C:\\模型 服务\\start.ps1', llm_stop_script_path: '' })
const snapshot = state => ({ enabled: true, state, stale: false })
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

function harness(mode = 'paths', overrides = {}) {
  const auth = vue.reactive({ user: { id: 'account-a', role: 'admin' } })
  const calls = []
  const unmount = []
  const api = {
    getGpuSchedulerStatus: async () => snapshot('IDLE'),
    getGpuSchedulerConfig: async () => ({ config: config() }),
    updateGpuSchedulerConfig: async patch => { calls.push(patch); return { config: { ...config(), ...patch } } },
    recoverGpuScheduler: async () => ({ accepted: true }),
    ...overrides,
  }
  const module = { exports: {} }
  runInNewContext(compiled, {
    module, exports: module.exports, defineProps: () => ({ mode }),
    setInterval: () => 1, clearInterval() {}, document: { visibilityState: 'visible' },
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {}, onBeforeUnmount(fn) { unmount.push(fn) } }
      if (name === '@/stores/auth') return { useAuthStore: () => auth }
      if (name === '@/api/admin') return api
      return { default: {} }
    },
  })
  return { ...module.exports, auth, calls, unmount: () => unmount.forEach(fn => fn()) }
}

test('script paths use only the administrator GPU config channel', async () => {
  const h = harness()
  await h.refresh(true)
  await h.save()
  assert.deepEqual(Object.keys(h.calls[0]).sort(), ['llm_start_script_path', 'llm_stop_script_path'])
  assert.equal(h.calls[0].llm_start_script_path, config().llm_start_script_path)
})

test('parameter saves preserve separately edited script paths', async () => {
  const h = harness('parameters')
  await h.refresh(true)
  h.draft.value.enabled = true
  h.draft.value.min_service_runtime = 600
  await h.save()
  assert.equal(h.calls[0].enabled, true)
  assert.equal(h.calls[0].min_service_runtime, 600)
  assert.equal('llm_start_script_path' in h.calls[0], false)
})

test('previous account status cannot reappear after account change', async () => {
  const old = deferred()
  let count = 0
  const h = harness('status', { getGpuSchedulerStatus: () => ++count === 1 ? old.promise : Promise.resolve(snapshot('TTS_ACTIVE')) })
  const pending = h.refresh()
  h.auth.user = { id: 'account-b', role: 'admin' }
  await vue.nextTick()
  await vue.nextTick()
  old.resolve(snapshot('LLM_ACTIVE'))
  await pending
  assert.equal(h.status.value.state, 'TTS_ACTIVE')
})

test('an unmounted panel ignores a late config response', async () => {
  const old = deferred()
  const h = harness('paths', { getGpuSchedulerConfig: () => old.promise })
  const pending = h.refresh(true)
  await vue.nextTick()
  h.unmount()
  old.resolve({ config: config() })
  await pending
  assert.equal(h.draft.value, null)
})

test('out-of-order status polls retain the newest response', async () => {
  const old = deferred()
  let count = 0
  const h = harness('status', { getGpuSchedulerStatus: () => ++count === 1 ? old.promise : Promise.resolve(snapshot('TTS_ACTIVE')) })
  const pending = h.refresh()
  await h.refresh()
  old.resolve(snapshot('LLM_ACTIVE'))
  await pending
  assert.equal(h.status.value.state, 'TTS_ACTIVE')
})

test('save validation errors remain visible after a successful status poll', async () => {
  const h = harness('paths', { updateGpuSchedulerConfig: async () => { throw new Error('script missing') } })
  await h.refresh(true)
  await h.save()
  await h.refresh()
  assert.equal(h.error.value, 'script missing')
  assert.equal(h.busy.value, false)
})

test('recovery requests report acceptance and refresh state', async () => {
  let called = false
  const h = harness('status', { recoverGpuScheduler: async () => { called = true; return { accepted: true } } })
  await h.recover()
  assert.equal(called, true)
  assert.match(h.message.value, /恢复请求已提交/)
})
