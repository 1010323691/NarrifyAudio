/** Faulted transports exercise the real merge operation, not a mirrored state machine. */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as vue from 'vue'
import ts from 'typescript'
class ApiError extends Error { constructor(status) { super('rejected'); this.status = status } }
function harness(api, storage = new Map(), committed = () => {}) {
  let uuid = 0
  const auth = vue.reactive({ user: { id: 'u' } }), project = vue.reactive({ activeProjectId: 'p' })
  const deactivate = [], stop = [], receipts = [], calls = []
  const dependencies = {
    vue: { ...vue, onBeforeUnmount: f => stop.push(f), onDeactivated: f => deactivate.push(f) },
    '@/api/client': { ApiError }, '@/api/tts': { runMerge: async (...args) => {
      calls.push(args); return api ? api(...args) : receipt(args[0])
    } },
    '@/stores/auth': { useAuthStore: () => auth }, '@/stores/project': { useProjectStore: () => project },
    '@/utils/uuid': { randomUuid: () => `key-${++uuid}` },
  }
  const module = { exports: {} }
  const code = ts.transpileModule(readFileSync(new URL('../src/composables/useMergeSubmission.ts', import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  runInNewContext(code, { module, exports: module.exports, require: n => dependencies[n],
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
  })
  const op = module.exports.useMergeSubmission(r => { receipts.push(r); committed(r) })
  return { op, storage, calls, receipts, auth, project, deactivate, stop: () => stop.forEach(f => f()) }
}
const names = n => Array.from({ length: n }, (_, i) => `chapter-${i}`)
const receipt = n => ({ batch_id: 'batch', task_ids: n.map(x => `task-${x}`), packages: n.map(x => ({ package: x, task_id: `task-${x}` })) })
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve } }

test('620 chapters use one request; >1000 chunks advance only on receipt', async () => {
  const h = harness(); await h.op.submit(names(620))
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0][0].length, 620)
  assert.equal(h.op.submitting.value, false); assert.equal(h.storage.size, 0)
  await h.op.submit(names(2001))
  assert.deepEqual(h.calls.slice(1).map(c => c[0].length), [1000, 1000, 1])
  assert.equal(new Set(h.calls.map(c => c[1])).size, 4)
  h.stop()
})

test('partial acceptance and lost response reuse only the failed chunk and its key after reload', async () => {
  let fail = true
  const storage = new Map()
  const api = async n => { if (fail && n[0] === 'chapter-1000') throw new TypeError('lost response'); return receipt(n) }
  const h = harness(api, storage); await h.op.submit(names(1500))
  assert.equal(h.receipts.length, 1); assert.equal(h.op.remaining.value, 500)
  assert.match(h.op.label.value, /1000\/1500/)
  const uncertain = h.calls[1][1]; h.stop(); fail = false
  const reload = harness(api, storage); await reload.op.submit()
  assert.equal(reload.calls.length, 1); assert.equal(reload.calls[0][1], uncertain)
  assert.equal(reload.calls[0][0][0], 'chapter-1000'); assert.equal(reload.op.remaining.value, 0)
  reload.stop()
})

test('duplicate clicks and scope switch cannot dispatch remaining batches into a new account/project', async () => {
  const wait = deferred(), h = harness(() => wait.promise)
  const first = h.op.submit(names(1500)); await h.op.submit(names(1500))
  assert.equal(h.calls.length, 1); h.project.activeProjectId = 'other'
  wait.resolve(receipt(names(1000))); await first
  assert.equal(h.calls.length, 1); assert.equal(h.receipts.length, 0)
  assert.equal(h.op.remaining.value, 0)
  h.project.activeProjectId = 'p'
  assert.equal(h.op.remaining.value, 500)
  assert.equal(h.calls[0][2], 'p')
  h.auth.user = { id: 'other-user' }
  assert.equal(h.op.remaining.value, 0); h.stop()
})

test('confirmed callbacks can fail without changing submission success or blocking the next chunk', async () => {
  const h = harness(null, new Map(), () => { throw new Error('refresh failed') })
  await h.op.submit(names(1001))
  assert.equal(h.calls.length, 2); assert.equal(h.op.failure.value, '')
  assert.equal(h.op.submitting.value, false); assert.equal(h.op.remaining.value, 0); h.stop()
})

test('definitive failure leaves earlier batches accepted and retries the remaining names', async () => {
  let fail = true
  const h = harness(async n => { if (fail && n[0] === 'chapter-1000') throw new ApiError(409); return receipt(n) })
  await h.op.submit(names(1001)); assert.equal(h.op.remaining.value, 1)
  const key = h.calls[1][1]; fail = false
  await h.op.submit(names(2000)); assert.equal(h.calls[2][0].length, 1)
  assert.equal(h.calls[2][1], key); assert.equal(h.op.remaining.value, 0); h.stop()
})


test('malformed receipt never advances or discards the retry key', async () => {
  let valid = false
  const h = harness(async n => valid ? receipt(n) : { task_ids: [] })
  await h.op.submit(['one']); assert.equal(h.op.remaining.value, 1)
  const key = h.calls[0][1]; valid = true; await h.op.submit()
  assert.equal(h.calls[1][1], key); assert.equal(h.op.remaining.value, 0); h.stop()
})
