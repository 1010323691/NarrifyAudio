/** Execute the real durable frontend operation against deferred/faulted transports. */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as vue from 'vue'
import ts from 'typescript'

class ApiError extends Error { constructor(status, message = 'rejected') { super(message); this.status = status } }
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }
function harness(api = {}, storage = new Map()) {
  let clock = 0, uuid = 0, next = 0
  const timers = new Map(), unmount = [], deactivate = [], activate = [], calls = []
  const auth = vue.reactive({ user: { id: 'user-a' } })
  const project = vue.reactive({ activeProjectId: 'project-a' })
  const dependencies = {
    vue: { ...vue, onMounted: f => f(), onUnmounted: f => unmount.push(f), onActivated: f => activate.push(f), onDeactivated: f => deactivate.push(f) },
    '@/api/client': { ApiError },
    '@/api/tts': {
      runBatch: async (...args) => { calls.push(args); return { task_ids: ['task-a'] } },
      submitBatchReset: async () => ({ task_id: 'reset-a', task_ids: ['reset-a'] }),
      getSubmission: async () => { throw new ApiError(404) }, ...api,
    },
    '@/api/durableTasks': { getDurableTask: async () => ({ status: 'succeeded' }), ...api },
    '@/stores/auth': { useAuthStore: () => auth }, '@/stores/project': { useProjectStore: () => project },
  }
  const module = { exports: {} }
  const code = ts.transpileModule(readFileSync(new URL('../src/composables/useTtsSubmission.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
  runInNewContext(code, { module, exports: module.exports, require: n => dependencies[n],
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    crypto: { randomUUID: () => `key-${++uuid}` }, Date: { now: () => clock }, document: { hidden: false },
    window: { addEventListener() {}, removeEventListener() {} },
    setTimeout: (fn, ms) => { const id = ++next; timers.set(id, { fn, time: clock + ms }); return id }, clearTimeout: id => timers.delete(id),
  })
  const instance = () => module.exports.useTtsSubmission()
  const op = instance()
  async function tick(ms) {
    const until = clock + ms
    while (true) {
      const due = [...timers.entries()].filter(([, v]) => v.time <= until).sort((a, b) => a[1].time - b[1].time)[0]
      if (!due) break
      clock = due[1].time; timers.delete(due[0]); due[1].fn(); await flush()
    }
    clock = until; await flush()
  }
  return { op, instance, calls, storage, auth, project, tick, deactivate, activate, stop: () => unmount.forEach(f => f()) }
}

test('click locks synchronously, blocks duplicate clicks and explains a five-second wait', async () => {
  const request = deferred(), calls = []
  const h = harness({ runBatch: (...args) => { calls.push(args); return request.promise } })
  const started = h.op.submit(['one.json'])
  assert.equal(h.op.locked.value, true)
  assert.match(h.op.label.value, /正在提交/)
  await h.op.submit(['one.json']); assert.equal(calls.length, 1)
  await h.tick(5000); assert.match(h.op.label.value, /服务器正在处理/)
  request.resolve({ task_ids: ['task-a'] }); await started
  assert.equal(h.op.locked.value, true)
  h.op.settled(['task-a']); assert.equal(h.op.locked.value, false)
  h.stop()
})

test('lost response retries only the original key and stays locked across page reload', async () => {
  const calls = [], storage = new Map()
  const api = { runBatch: async (...args) => { calls.push(args); throw new TypeError('lost response') } }
  const first = harness(api, storage)
  await first.op.submit(['one.json']); assert.match(first.op.label.value, /确认提交结果/)
  first.stop()
  const replay = harness(api, storage); await flush(); await replay.tick(1000)
  assert.equal(replay.op.locked.value, true)
  assert.ok(calls.length >= 2)
  assert.ok(calls.every(call => call[1] === calls[0][1] && call[2] === 'project-a'))
  replay.stop()
})

test('recovered receipt survives beyond the task replay window and unlocks only at completion', async () => {
  let status = 'running'
  const h = harness({ getSubmission: async () => ({ task_ids: ['task-a'], project_id: 'project-a', statuses: { 'task-a': status } }) })
  await h.op.submit(['one.json']); await h.tick(1000)
  assert.equal(h.op.locked.value, true)
  status = 'cancelled'; await h.tick(2000)
  assert.equal(h.op.locked.value, false)
  h.stop()
})

test('late project-A response cannot populate project B and the submitted project stays frozen', async () => {
  const request = deferred(), calls = []
  const h = harness({ runBatch: (...args) => { calls.push(args); return request.promise }, getSubmission: async () => { throw new TypeError('offline') } })
  const started = h.op.submit(['one.json'])
  h.project.activeProjectId = 'project-b'
  request.resolve({ task_ids: ['task-a'] }); await started
  assert.equal(h.op.receipt.value, null); assert.equal(h.op.locked.value, false)
  assert.equal(calls[0][2], 'project-a')
  h.project.activeProjectId = 'project-a'; await flush()
  assert.equal(h.op.locked.value, true)
  h.stop()
})

test('two consumers share the lock; hidden pages stop recovery polling', async () => {
  const request = deferred(), calls = []
  const h = harness({ runBatch: (...args) => { calls.push(args); return request.promise } })
  const second = h.instance(), started = h.op.submit(['one.json'])
  await second.submit(['two.json']); assert.equal(calls.length, 1)
  h.deactivate.forEach(f => f()); await h.tick(20000); assert.equal(calls.length, 1)
  request.resolve({ task_ids: ['task-a'] }); await started; h.stop()
})

test('a successful reset is persisted and never repeated after a lost synthesis response', async () => {
  let resets = 0
  const calls = [], storage = new Map()
  const api = { submitBatchReset: async () => { resets++; return { task_id: 'reset-a', task_ids: ['reset-a'] } },
    runBatch: async (...args) => { calls.push(args); throw new TypeError('offline') } }
  const h = harness(api, storage); await h.op.submit(['one.json'], 'reset'); h.stop()
  const replay = harness(api, storage); await flush(); await replay.tick(1000)
  assert.equal(resets, 1)
  assert.ok(calls.length >= 2 && calls.every(call => call[1] === calls[0][1]))
  replay.stop()
})
