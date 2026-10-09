/** Exercise the actual durable submission client under transport and scope faults. */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as vue from 'vue'
import ts from 'typescript'

class ApiError extends Error { constructor(status, message = 'rejected') { super(message); this.status = status } }
let uuid = 0
const names = n => Array.from({ length: n }, (_, i) => `chapter-${i}`)
const receipt = items => ({ batch_id: `batch-${items[0] ?? 'empty'}`, task_ids: items.map(n => `task-${n}`) })
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve } }
function harness(api, storage = new Map(), storageFailure = false) {
  const auth = vue.reactive({ user: { id: 'u' } }), project = vue.reactive({ activeProjectId: 'p' }), calls = []
  const dependencies = {
    vue, '@/api/client': { ApiError, http: { post: async (...args) => { calls.push(args); return api ? api(...args) : receipt(args[1].chapters) } } },
    '@/stores/auth': { useAuthStore: () => auth }, '@/stores/project': { useProjectStore: () => project },
    '@/utils/uuid': { randomUuid: () => `key-${++uuid}` },
  }
  const module = { exports: {} }
  const code = ts.transpileModule(readFileSync(new URL('../src/api/batchSubmission.ts', import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  runInNewContext(code, { module, exports: module.exports, require: n => dependencies[n], DOMException,
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => {
      if (storageFailure) throw new Error('storage disabled'); storage.set(k, v)
    }, removeItem: k => storage.delete(k) },
  })
  return { ...module.exports, calls, storage, auth, project }
}
const route = '/api/bgm/mix'
const submit = (h, count) => h.submitTaskBatches(route, { chapters: names(count) }, 'chapters')

test('2001 entries are sequential 1000/1000/1 requests with one stable key per chunk', async () => {
  const h = harness(); const result = await submit(h, 2001)
  assert.deepEqual(h.calls.map(c => c[1].chapters.length), [1000, 1000, 1])
  assert.equal(new Set(h.calls.map(c => c[2].headers['Idempotency-Key'])).size, 3)
  assert.equal(result.task_ids.length, 2001); assert.equal(h.storage.size, 0)
  assert.ok(h.calls.every(c => c[1].project_id === 'p'))
})

test('lost second response restores original payload and key after reload without repeating chunk one', async () => {
  const storage = new Map(), accepted = new Map()
  let lose = true
  const api = async (_route, body, options) => {
    const key = options.headers['Idempotency-Key']; const value = accepted.get(key) || receipt(body.chapters); accepted.set(key, value)
    if (body.chapters[0] === 'chapter-1000' && lose) throw new TypeError('response lost')
    return value
  }
  const h = harness(api, storage)
  await assert.rejects(submit(h, 1500), /response lost/)
  const uncertain = h.calls[1][2].headers['Idempotency-Key']
  lose = false
  const reload = harness(api, storage)
  reload.restoreSubmissions('u:p', [route])
  assert.equal(Object.values(reload.pendingSubmissions.value)[0].offset, 1000)
  const result = await reload.resumeSubmission('u:p', route)
  assert.equal(reload.calls.length, 1); assert.equal(reload.calls[0][2].headers['Idempotency-Key'], uncertain)
  assert.equal(result.task_ids.length, 1500); assert.equal(accepted.size, 2); assert.equal(storage.size, 0)
})

test('duplicate clicks, account switch and switch-back do not dispatch remaining old batches', async () => {
  const wait = deferred(), h = harness(() => wait.promise)
  const first = submit(h, 1500); const duplicate = submit(h, 1500)
  assert.equal(h.calls.length, 1)
  h.auth.user = { id: 'other' }; h.auth.user = { id: 'u' }
  wait.resolve(receipt(names(1000)))
  await assert.rejects(first, { name: 'AbortError' }); await assert.rejects(duplicate, { name: 'AbortError' })
  assert.equal(h.calls.length, 1)
  assert.equal(Object.values(h.pendingSubmissions.value)[0].offset, 1000)
  h.project.activeProjectId = 'q'
  h.restoreSubmissions('u:q', [route])
  assert.equal(Object.values(h.pendingSubmissions.value).filter(op => op.scope === 'u:q').length, 0)
})

test('async all-role resolution is invalidated even if the user switches away and back', async () => {
  const wait = deferred(), h = harness()
  const first = h.submitTaskBatches(route, { chapters: null }, 'chapters', undefined, () => wait.promise)
  h.project.activeProjectId = 'q'; h.project.activeProjectId = 'p'; wait.resolve(names(100))
  await assert.rejects(first, { name: 'AbortError' }); assert.equal(h.calls.length, 0); assert.equal(h.storage.size, 0)
})

test('partial acceptance of new-only roles does not repeat filtered selections', async () => {
  const h = harness((_route, body) => receipt(body.speakers.slice(0, 2)))
  const result = await h.submitTaskBatches('/api/tts/make-clones', { speakers: names(1500), new_only: true }, 'speakers')
  assert.equal(result.task_ids.length, 4); assert.equal(h.calls.length, 2); assert.equal(h.storage.size, 0)
})

test('parse receipts retain all chapter mappings across batches', async () => {
  const h = harness((_route, body) => {
    const result = receipt(body.files.map(f => f.name)); return { ...result, files: body.files.map((f, i) => ({ name: f.name, task_id: result.task_ids[i] })) }
  })
  const result = await h.submitTaskBatches('/parse', { files: names(1500).map(name => ({ name, sha256: 'version' })) }, 'files')
  assert.equal(result.files.length, 1500); assert.equal(result.files[1499].name, 'chapter-1499')
})

test('uncertain intent cannot be replaced or cleared, while an atomic 409 rejection may be cleared', async () => {
  const h = harness(() => { throw new TypeError('lost') })
  await assert.rejects(submit(h, 100), /lost/)
  await assert.rejects(submit(h, 101), /上次提交/)
  assert.throws(() => h.discardRejectedSubmission('u:p', route), /尚未确认/)
  assert.equal(h.calls.length, 1)
  const rejected = harness(() => { throw new ApiError(409) })
  await assert.rejects(submit(rejected, 100), /rejected/)
  rejected.discardRejectedSubmission('u:p', route); assert.equal(rejected.storage.size, 0)
})

test('malformed receipts and unavailable durable storage never create a new replay key', async () => {
  const h = harness(() => ({ batch_id: 'bad', task_ids: ['duplicate', 'duplicate'] }))
  await assert.rejects(submit(h, 100), /回执不完整/)
  const key = h.calls[0][2].headers['Idempotency-Key']
  await assert.rejects(submit(h, 100), /回执不完整/)
  assert.equal(h.calls[1][2].headers['Idempotency-Key'], key)
  const disabled = harness(null, new Map(), true)
  await assert.rejects(submit(disabled, 100), /保存提交进度/); assert.equal(disabled.calls.length, 0)
})
