import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const { parse, compileTemplate } = require('@vue/compiler-sfc')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
const flush = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)); await vue.nextTick() }
const emptyEntries = { items: [], total: 0, size_bytes: 0, complete: true, snapshots: [], page: 1, page_size: 50, incomplete_projects: [] }

function harness(overrides = {}, query = { project: 'a', view: 'files' }) {
  const route = vue.reactive({ query })
  const hooks = { activated: [], deactivated: [], unmounted: [] }
  const calls = { selections: [], submits: [], acquired: 0, released: 0 }
  const task = vue.reactive({ tasks: [], snapshotVersion: 0, connectionStatus: 'connected', refresh: async () => {}, acquireGlobalScope() { calls.acquired++; return () => { calls.released++ } } })
  const api = {
    getResourceOverview: async () => ({ projects: [{ project_id: 'a', name: '项目A', stale: false }, { project_id: 'b', name: '项目B', stale: false }], exports: [] }),
    getResourceEntries: async () => ({ ...emptyEntries }),
    getCleanupPreview: async () => ({ projects: [], items: [], total: 0 }),
    submitResourceTask: async (project, type, payload) => { calls.submits.push({ project, type, payload }); return { id: `op-${calls.submits.length}` } },
    ...overrides,
  }
  const mocks = {
    vue: { ...vue, onActivated: fn => hooks.activated.push(fn), onDeactivated: fn => hooks.deactivated.push(fn), onBeforeUnmount: fn => hooks.unmounted.push(fn) },
    'vue-router': { useRoute: () => route, useRouter: () => ({ replace: async target => { route.query = typeof target === 'string' ? {} : target.query }, push: async () => {} }) },
    '@/stores/project': { useProjectStore: () => ({ activeProjectId: 'a', select: async id => calls.selections.push(id) }) },
    '@/stores/task': { useTaskStore: () => task },
    '@/components/ui/toast': { useToast: () => ({ push() {} }) },
    '@/components/ui/dialog': { showConfirm: async () => true },
    '@/api/resources': api,
    '@/api/project': { deleteProject: async () => {} },
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
      module, exports: module.exports, require: load, AbortController, setTimeout, clearTimeout,
    })
    return module.exports
  }
  const center = load('@/composables/useResourceCenter').useResourceCenter()
  return { center, route, hooks, calls, task, close: () => hooks.unmounted.forEach(fn => fn()) }
}

test('resource page and dialogs compile with Vue template compiler', () => {
  for (const file of ['views/MyResources.vue', ...['ResourceProjectMatrix', 'ResourceFileBrowser', 'ResourcePreviewDrawer', 'ResourceActionDialog', 'ResourceStorageView', 'ResourceTaskBrief', 'ResourceStructuredValue'].map(name => `components/resources/${name}.vue`)]) {
    const { descriptor, errors } = parse(readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8'))
    assert.deepEqual(errors, [])
    assert.deepEqual(compileTemplate({ source: descriptor.template.content, filename: file, id: file }).errors, [])
  }
})

test('switching a pending text preview to media clears loading and discards the old result', async () => {
  const source = readFileSync(new URL('../src/components/resources/ResourcePreviewDrawer.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  for (const kind of ['audio', 'image', null]) {
    const delayed = deferred()
    const props = vue.reactive({ entry: { id: 'text', preview_kind: 'text' }, pinned: true })
    const module = { exports: {} }
    const mocks = {
      vue: { ...vue, onBeforeUnmount() {} },
      '@/api/resources': { getResourcePreview: () => delayed.promise, resourceFileUrl: () => '/preview' },
      '@/composables/useAudioBus': { useAudioBus: () => ({ claim() {}, release() {} }) },
    }
    const code = ts.transpileModule(`${descriptor.scriptSetup.content}\nmodule.exports = { loading, preview };`, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText
    runInNewContext(code, {
      module, exports: module.exports, require: name => mocks[name] || {},
      defineProps: () => props, defineEmits: () => () => {}, document: { activeElement: null }, AbortController,
    })
    assert.equal(module.exports.loading.value, true)
    props.entry = { id: `next-${kind}`, preview_kind: kind }
    await flush()
    assert.equal(module.exports.loading.value, false)
    delayed.resolve({ content: 'old text' })
    await flush()
    assert.equal(module.exports.loading.value, false)
    assert.equal(module.exports.preview.value, null)
  }
})

test('production materials cannot be selected or exported and returning to products resets the scope', async () => {
  const h = harness({ getResourceEntries: async () => ({ ...emptyEntries, items: [{ id: 'middle', kind: 'file', can_package: false }], total: 1 }) }, { project: 'a', view: 'files', tab: 'materials' })
  await flush()
  assert.equal(h.center.category.value, 'production')
  h.center.toggleSelected('middle')
  h.center.togglePageSelected()
  assert.equal(h.center.selection.value.size, 0)
  await h.center.prepareExport(true)
  assert.equal(h.center.modal.value, null)
  h.center.setTab('projects')
  await flush()
  assert.equal(h.center.category.value, 'deliverables')
  assert.equal(h.calls.selections.length, 0)
  h.close()
})

test('page selection includes only deliverable entries', async () => {
  const h = harness({ getResourceEntries: async () => ({ ...emptyEntries, items: [{ id: 'product', kind: 'file', can_package: true }, { id: 'middle', kind: 'file', can_package: false }], total: 2 }) })
  await flush()
  h.center.togglePageSelected()
  assert.deepEqual([...h.center.selection.value], ['product'])
  h.center.toggleSelected('middle')
  assert.deepEqual([...h.center.selection.value], ['product'])
  h.close()
})

test('a newly completed task refreshes products even when its running frame was missed', async () => {
  const h = harness()
  await flush()
  h.task.tasks = [{ id: 'fast-mix', project_id: 'a', task_type: 'bgm.mix', status: 'succeeded', created_at: new Date().toISOString(), result: { deliveries: [{}] } }]
  await flush()
  assert.match(h.center.actionResult.value, /成品已生成/)
  await new Promise(resolve => setTimeout(resolve, 700))
  assert.equal(h.calls.submits.length, 1)
  assert.equal(h.calls.submits[0].type, 'resources.scan')
  h.close()
})

test('a late project export response cannot replace the most recent product scope', async () => {
  const first = deferred(), second = deferred()
  let calls = 0
  const h = harness({ getResourceEntries: () => (++calls === 1 ? first.promise : second.promise) }, {})
  await flush()
  const a = h.center.prepareProjectExport({ project_id: 'a', snapshot: { complete: true } })
  const b = h.center.prepareProjectExport({ project_id: 'b', snapshot: { complete: true } })
  second.resolve({ ...emptyEntries, total: 2, snapshots: [{ project_id: 'b', snapshot_id: 'b-snapshot' }] })
  await b
  first.resolve({ ...emptyEntries, total: 1, snapshots: [{ project_id: 'a', snapshot_id: 'a-snapshot' }] })
  await a
  assert.equal(h.center.exportScope.value.project_id, 'b')
  assert.equal(h.center.exportCount.value, 2)
  h.close()
})

test('browsing another project keeps the production project and releases its global subscription', async () => {
  const h = harness()
  await flush()
  h.center.openProject('b')
  await flush()
  assert.equal(h.center.projectId.value, 'b')
  assert.equal(h.calls.selections.length, 0)
  assert.equal(h.calls.acquired, 1)
  h.close()
  assert.equal(h.calls.released, 1)
})

test('scope changes clear old rows and late responses cannot overwrite the newest scope', async () => {
  const first = deferred(), second = deferred()
  let call = 0
  const h = harness({ getResourceEntries: () => (++call === 1 ? first.promise : second.promise) })
  await flush()
  h.center.entries.value = { ...emptyEntries, items: [{ id: 'old', kind: 'file' }] }
  h.route.query = { project: 'b', view: 'files' }
  await flush()
  assert.equal(h.center.entries.value, null)
  second.resolve({ ...emptyEntries, items: [{ id: 'new', kind: 'file' }] })
  await flush()
  first.resolve({ ...emptyEntries, items: [{ id: 'old', kind: 'file' }] })
  await flush()
  assert.equal(h.center.entries.value.items[0].id, 'new')
  h.close()
})

test('cleanup preview pagination rejects an out-of-order response', async () => {
  const first = deferred(), second = deferred()
  let call = 0
  const h = harness({ getCleanupPreview: () => (++call === 1 ? first.promise : second.promise) })
  await flush()
  const opening = h.center.prepareCleanup(['a'])
  const newer = h.center.loadCleanup(2)
  second.resolve({ projects: [], items: [{ id: 'page-2' }], total: 100 })
  await newer
  first.resolve({ projects: [], items: [{ id: 'page-1' }], total: 100 })
  await opening
  assert.equal(h.center.cleanupPage.value, 2)
  assert.equal(h.center.cleanup.value.items[0].id, 'page-2')
  h.close()
})

test('adjacent production completions coalesce into one scan for their explicit projects', async () => {
  const h = harness()
  await flush()
  h.task.tasks = [{ id: 'a-task', project_id: 'a', task_type: 'tts.batch', status: 'running' }, { id: 'b-task', project_id: 'b', task_type: 'tts.batch', status: 'running' }]
  await flush()
  h.task.tasks.forEach(item => { item.status = 'succeeded' })
  await flush()
  await new Promise(resolve => setTimeout(resolve, 700))
  assert.equal(h.calls.submits.length, 1)
  assert.equal(h.calls.submits[0].type, 'resources.scan')
  assert.equal([...h.calls.submits[0].payload.project_ids].sort().join(','), 'a,b')
  assert.equal(h.calls.selections.length, 0)
  h.close()
})

test('leaving the resource page invalidates outstanding reads', async () => {
  const delayed = deferred()
  const h = harness({ getResourceOverview: () => delayed.promise })
  h.hooks.deactivated.forEach(fn => fn())
  delayed.resolve({ projects: [{ project_id: 'old-user' }], exports: [] })
  await flush()
  assert.equal(h.center.overview.value, null)
  h.close()
})

test('directory export confirms recursive file count rather than counting folders', async () => {
  const h = harness({ getResourceEntries: async query => ({ ...emptyEntries, total: query.directory_mode ? 2 : 15, size_bytes: query.directory_mode ? 0 : 3000, snapshots: [{ project_id: 'a', snapshot_id: 'snapshot' }] }) }, { project: 'a', view: 'files', mode: 'directory', path: '02_split_text' })
  await flush()
  assert.equal(h.center.entries.value.total, 2)
  await h.center.prepareExport(true)
  assert.equal(h.center.exportCount.value, 15)
  assert.equal(h.center.exportBytes.value, 3000)
  assert.equal(h.center.exportScope.value.directory_mode, false)
  h.close()
})

test('historical completed operations stay in storage instead of crowding the resource home', async () => {
  const h = harness()
  await flush()
  h.task.tasks = [{ id: 'old-export', task_type: 'resources.package', status: 'succeeded', project_id: 'a' }]
  await flush()
  assert.equal(h.center.operationTasks.value.length, 0)
  h.task.tasks.push({ id: 'live-export', task_type: 'resources.package', status: 'running', project_id: 'b' })
  assert.equal(h.center.operationTasks.value.length, 1)
  h.close()
})


test('refreshing all resources submits every owned project despite a twelve-project page', async () => {
  const ids = Array.from({ length: 57 }, (_, i) => `project-${i}`)
  const h = harness({
    getResourceOverview: async () => ({ projects: ids.slice(0, 12).map(project_id => ({ project_id, name: project_id, stale: false })), exports: [], pagination: { total: 57, page: 1, page_size: 12, counts: { all: 57 } } }),
    getResourceProjectIds: async () => ({ project_ids: ids }),
  }, {})
  await flush()
  await h.center.refreshResources()
  assert.equal(h.center.overview.value.projects.length, 12)
  assert.deepEqual(Array.from(h.calls.submits.at(-1).payload.project_ids), ids)
  h.close()
})
