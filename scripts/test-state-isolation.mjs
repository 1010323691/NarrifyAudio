import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const { createPinia, setActivePinia } = require('pinia')
const root = new URL('../', import.meta.url)
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

function harness(overrides = {}) {
  setActivePinia(createPinia())
  const api = {
    '@/api/config': { getConfig: async () => ({ ui: { theme: 'light' } }), patchConfig: async () => ({}) },
    '@/api/tasks': {
      controlTask: async () => ({}),
      controlTaskCategory: async () => ({ changed: 0, tasks: [] }),
      listTasks: async () => [],
      streamAllTasks: () => () => {},
    },
    '@/api/client': { http: { get: async () => ({ status: 'running' }) } },
    '@/api/project': {},
    ...overrides,
  }
  const modules = new Map()
  function load(name) {
    if (api[name]) return api[name]
    if (name === './client' && api['@/api/client']) return api['@/api/client']
    if (!name.startsWith('@/')) return require(name)
    if (modules.has(name)) return modules.get(name).exports
    const module = { exports: {} }
    modules.set(name, module)
    const path = fileURLToPath(new URL(`src/${name.slice(2)}.ts`, root))
    const { outputText } = ts.transpileModule(readFileSync(path, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    })
    runInNewContext(outputText, {
      module, exports: module.exports, require: load,
      document: { documentElement: { classList: { toggle() {} } } },
      setTimeout, clearTimeout, DOMException, AbortController,
    }, { filename: path })
    return module.exports
  }
  return load
}

test('configuration from a previous account cannot reappear after reset', async () => {
  const old = deferred()
  const load = harness({ '@/api/config': { getConfig: () => old.promise } })
  const settings = load('@/stores/settings').useSettingsStore()
  const pending = settings.load()
  settings.reset()
  old.resolve({ ui: { theme: 'dark' }, secret: 'previous-account' })
  await pending
  assert.equal(settings.config, null)
  assert.equal(settings.loaded, false)
})

test('a delayed save cannot overwrite the next project configuration', async () => {
  const old = deferred()
  const load = harness({ '@/api/config': {
    patchConfig: () => old.promise,
    getConfig: async () => ({ ui: { theme: 'light' }, project: 'B' }),
  } })
  const settings = load('@/stores/settings').useSettingsStore()
  const pending = settings.save({ ui: { theme: 'dark' } })
  settings.reset()
  await settings.load()
  old.resolve({ ui: { theme: 'dark' }, project: 'A' })
  assert.equal(await pending, false)
  assert.equal(settings.config.project, 'B')
  assert.equal(settings.saving, false)
})

test('out-of-order configuration loads retain the newest response', async () => {
  const first = deferred()
  let calls = 0
  const load = harness({ '@/api/config': {
    getConfig: () => ++calls === 1 ? first.promise : Promise.resolve({ ui: { theme: 'light' }, version: 2 }),
  } })
  const settings = load('@/stores/settings').useSettingsStore()
  const old = settings.load()
  await settings.load()
  first.resolve({ ui: { theme: 'dark' }, version: 1 })
  await old
  assert.equal(settings.config.version, 2)
})

test('project refresh clears pipeline state when the active project changes', async () => {
  const load = harness({ '@/api/project': {
    getActiveProject: async () => ({ set: true, project_id: 'B' }),
    listProjects: async () => [],
  } })
  const project = load('@/stores/project').useProjectStore()
  const pipeline = load('@/stores/pipelineState').usePipelineStateStore()
  project.setCurrent({ set: true, project_id: 'A' })
  pipeline.activeScript = 'A.json'
  await project.refresh()
  assert.equal(pipeline.activeScript, '')
  assert.equal(project.activeProjectId, 'B')
})

test('A to B to A project switches clear each previous pipeline result', async () => {
  const load = harness({ '@/api/project': {
    selectProject: async (id) => ({ set: true, project_id: id }),
  } })
  const project = load('@/stores/project').useProjectStore()
  const pipeline = load('@/stores/pipelineState').usePipelineStateStore()

  project.setCurrent({ set: true, project_id: 'A' })
  pipeline.setActiveScript('A.json')
  await project.select('B')
  assert.equal(project.activeProjectId, 'B')
  assert.equal(pipeline.activeScript, '')

  pipeline.setActiveScript('B.json')
  await project.select('A')
  assert.equal(project.activeProjectId, 'A')
  assert.equal(pipeline.activeScript, '')
})

test('aborting a durable task wait stops its next polling request', async () => {
  let calls = 0
  const load = harness({ '@/api/client': {
    http: { get: async () => { calls += 1; return { status: 'running' } } },
  } })
  const { waitForDurableTask } = load('@/api/durableTasks')
  const controller = new AbortController()
  const waiting = waitForDurableTask('task-1', 50, controller.signal)
  await Promise.resolve()
  controller.abort()
  await assert.rejects(waiting, { name: 'AbortError' })
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(calls, 1)
})

test('audio task reattachment is restricted to the active project', () => {
  const load = harness()
  const { findActiveDurableTask } = load('@/api/durableTasks')
  const tasks = [
    { id: 'task-A', project_id: 'A', task_type: 'audio.cut', status: 'running' },
    { id: 'task-B', project_id: 'B', task_type: 'audio.cut', status: 'running' },
    { id: 'task-C', project_id: 'A', task_type: 'audio.cut', status: 'succeeded' },
  ]
  assert.equal(findActiveDurableTask(tasks, 'B', 'audio.cut')?.id, 'task-B')
  assert.equal(findActiveDurableTask(tasks, 'C', 'audio.cut'), undefined)
})

test('task control confirms the status over the SSE stream, not from the POST response', async () => {
  // The v1 control POST returns the lean durable dict (not a UI TaskSnapshot, and a
  // running task's cancel first lands as the intermediate 'cancelling'), so 5a-2
  // deliberately discards the response body — the stream's frames are the
  // confirmation channel. This case pins that contract on both sides.
  let emit
  const load = harness({ '@/api/tasks': {
    controlTask: async () => ({ id: 'task-1', progress: 0 }),
    listTasks: async () => [],
    streamAllTasks: (onEvent) => { emit = onEvent; return () => {} },
  } })
  const store = load('@/stores/task').useTaskStore()
  store.bindProject('proj-1')
  await store.control('task-1', 'cancel')
  assert.equal(store.tasks.find((task) => task.id === 'task-1'), undefined)
  emit({ task_id: 'task-1', type: 'snapshot_all', tasks: [{ id: 'task-1', status: 'running', progress: 0 }] })
  emit({ task_id: 'task-1', type: 'status', status: 'cancelled' })
  assert.equal(store.tasks.find((task) => task.id === 'task-1')?.status, 'cancelled')
})

test('a rerun supersedes its terminal row over the stream and reset clears the memory', async () => {
  // The stream's ``superseded`` frame is the ONLY signal that the server stopped
  // showing a terminal row because a re-run of the same entry replaced it: the
  // store must drop the row AND remember its id (the task centre merges rows
  // from /history that the live stream never tracked). reset() must clear the
  // memory, or a new account's rows would be filtered out by the old one's.
  let emit
  const load = harness({ '@/api/tasks': {
    controlTask: async () => ({}),
    listTasks: async () => [],
    streamAllTasks: (onEvent) => { emit = onEvent; return () => {} },
  } })
  const store = load('@/stores/task').useTaskStore()
  store.bindProject('proj-1')
  store.setTaskCenterOpen(true) // opens the stream; the harness captures its emitter
  emit({ task_id: 'task-1', type: 'snapshot_all', tasks: [{ id: 'task-1', status: 'succeeded' }] })
  assert.equal(store.tasks.map((task) => task.id).join(','), 'task-1')
  emit({ task_id: 'task-1', type: 'superseded' })
  emit({ task_id: 'task-2', type: 'snapshot', task: { id: 'task-2', status: 'pending' } })
  assert.equal(store.tasks.map((task) => task.id).join(','), 'task-2')
  assert.equal(store.supersededIds.has('task-1'), true)
  assert.equal(store.supersededIds.has('task-2'), false)
  // A reconnect replays the server's visible view: the superseded terminal
  // row must not be resurrected by the additive upsert path.
  emit({ task_id: 'task-2', type: 'snapshot_all', tasks: [{ id: 'task-2', status: 'pending' }] })
  assert.equal(store.tasks.map((task) => task.id).join(','), 'task-2')
  store.reset()
  assert.equal(store.tasks.length, 0)
  assert.equal(store.supersededIds.size, 0)
  // Reconnect WITHOUT a live superseded frame (the new stream session never
  // tracked the old row): the replayed snapshot is the only truth, so a
  // terminal row the server no longer lists must be pruned from the store.
  store.bindProject('proj-1')
  store.setTaskCenterOpen(true)
  emit({ task_id: 'task-9', type: 'snapshot_all', tasks: [{ id: 'task-9', status: 'succeeded' }] })
  assert.equal(store.tasks.map((task) => task.id).join(','), 'task-9')
  emit({ task_id: 'task-8', type: 'snapshot_all', tasks: [{ id: 'task-8', status: 'pending' }] })
  assert.equal(store.tasks.map((task) => task.id).join(','), 'task-8')
})

test('a delayed task category control cannot repopulate state after reset', async () => {
  const old = deferred()
  const load = harness({ '@/api/tasks': {
    controlTask: async () => ({}),
    controlTaskCategory: () => old.promise,
    listTasks: async () => [],
    streamAllTasks: () => () => {},
  } })
  const store = load('@/stores/task').useTaskStore()
  const pending = store.controlCategory('project-1', 'script', 'pause')
  store.reset()
  old.resolve({ changed: 1, tasks: [{ id: 'old-task', status: 'paused', error_code: 'manual_pause' }] })
  assert.equal(await pending, 1)
  assert.equal(store.tasks.length, 0)
})

test('a create response from a signed-out account cannot select its project', async () => {
  const old = deferred()
  let selections = 0
  const load = harness({ '@/api/project': {
    createProject: () => old.promise,
    selectProject: async () => { selections += 1; return {} },
    getActiveProject: async () => ({}), listProjects: async () => [],
  } })
  const project = load('@/stores/project').useProjectStore()
  const pending = project.create('Old account project')
  project.reset()
  old.resolve({ id: 'old-project' })
  await pending
  assert.equal(selections, 0)
  assert.equal(project.current, null)
})

test('preview line state: previewReady keys off the draft, never the disk line', () => {
  // previewReady 的口径 = staged 内容 == 页内草稿（绝不与磁盘 03 比——磁盘正是修改前旧值）。
  const load = harness()
  const { isPreviewReady, isDirty, restoreDraft, lineStatus, saveState, buildEdits } =
    load('@/utils/previewLineState')
  // 沙箱（vm 领域）里构造的对象原型与测试文件不同 realm——JSON 往返归一化后再比。
  const realm = (value) => JSON.parse(JSON.stringify(value))
  const disk = { text: '原台词', speaker: 'A', instruct: '' }
  const draft = { text: '新台词', speaker: 'B', instruct: '愤怒地' }
  const staged = { ...draft, ok: true, reason: '', rendered_at: 'r1', fingerprint: 'fp', file: '0001.mp3' }
  assert.equal(isDirty(draft, disk), true)
  assert.equal(isPreviewReady(staged, draft), true)
  // 与磁盘不一致 ≠ 未就绪——与磁盘比会永远卡在「不可保存」。
  assert.equal(isPreviewReady(staged, disk), false)
  // 渲染后又改了草稿 → 旧产物，按 dirty 处理（不是 failed）。
  const reedited = { ...draft, text: '再改' }
  assert.equal(isPreviewReady(staged, reedited), false)
  assert.equal(lineStatus({ disk, draft: reedited, staged, rendering: false, renderFailed: false }), 'dirty')
  // 无暂存 / 暂存失败 → 不是 previewReady；失败句 → failed。
  assert.equal(isPreviewReady(null, draft), false)
  assert.equal(lineStatus({ disk, draft, staged: { ...staged, ok: false, reason: '超时（已隔离）' }, rendering: false, renderFailed: false }), 'failed')
  // 优先级：rendering > failed > previewReady > dirty > normal。
  assert.equal(lineStatus({ disk, draft, staged, rendering: true, renderFailed: false }), 'rendering')
  assert.equal(lineStatus({ disk, draft, staged: null, rendering: false, renderFailed: true }), 'failed')
  assert.equal(lineStatus({ disk, draft, staged, rendering: false, renderFailed: false }), 'previewReady')
  assert.equal(lineStatus({ disk, draft: disk, staged: null, rendering: false, renderFailed: false }), 'normal')
  // F5 恢复：staged.ok 且 ≠ 磁盘 → 以 staged 为草稿；staged == 磁盘或失败 → 磁盘行。
  assert.deepEqual(realm(restoreDraft(staged, disk)), draft)
  assert.deepEqual(realm(restoreDraft({ ...staged, ok: true, ...disk }, disk)), disk)
  assert.deepEqual(realm(restoreDraft(null, disk)), disk)
  assert.deepEqual(realm(restoreDraft({ ...staged, ok: false }, disk)), disk)
  // 保存门禁：无修改 → 不可保存；修改句全部 previewReady → 可保存；有 pending → 不可。
  const rows = [
    { index: 0, disk, draft, staged, rendering: false },
    { index: 1, disk: { ...disk }, draft: { ...disk }, staged: null, rendering: false },
  ]
  assert.deepEqual(realm(saveState(rows.map(({ disk: d, draft: f, staged: s, rendering: r }) => ({ disk: d, draft: f, staged: s, rendering: r })))), { ready: true, dirtyCount: 1, pending: 0 })
  const pendingRow = rows.map((r) => (r.index === 0 ? { ...r, staged: null } : r))
  const pendingGate = saveState(pendingRow.map(({ disk: d, draft: f, staged: s, rendering: r }) => ({ disk: d, draft: f, staged: s, rendering: r })))
  assert.equal(pendingGate.ready, false)
  assert.equal(pendingGate.pending, 1)
  const renderingGate = saveState(pendingRow.map((r) => ({ disk: r.disk, draft: r.draft, staged: r.staged, rendering: true })))
  assert.equal(renderingGate.ready, false)
  // edits：partial triple（只带被改字段，strip 口径）；全同句不产生 edit。
  assert.deepEqual(realm(buildEdits(rows)), [
    { index: 0, text: '新台词', speaker: 'B', instruct: '愤怒地' },
  ])
  assert.deepEqual(realm(buildEdits([{ index: 3, disk, draft: { ...disk, text: '  新台词 ' } }])), [
    { index: 3, text: '新台词' },
  ])
  assert.deepEqual(realm(buildEdits([{ index: 9, disk, draft: disk }])), [])
})


test('client log display defaults off and synchronizes the shared setting', async () => {
  let enabled = true
  const load = harness({ '@/api/client': { http: {
    get: async () => ({ enabled }),
    patch: async (_url, patch) => { enabled = patch.enabled; return { enabled } },
  } } })
  const display = load('@/stores/clientDisplay').useClientDisplayStore()
  assert.equal(display.logsEnabled, false)
  await display.load()
  assert.equal(display.logsEnabled, true)
  await display.save(false)
  assert.equal(enabled, false)
  enabled = true
  await display.load()
  assert.equal(display.logsEnabled, true)
})

test('late log display reads cannot undo an admin save or account reset', async () => {
  const old = deferred()
  const load = harness({ '@/api/client': { http: {
    get: () => old.promise,
    patch: async () => ({ enabled: false }),
  } } })
  const display = load('@/stores/clientDisplay').useClientDisplayStore()
  const pending = display.load()
  await display.save(false)
  old.resolve({ enabled: true })
  await pending
  assert.equal(display.logsEnabled, false)
  const stale = display.load()
  display.reset()
  await stale
  assert.equal(display.logsEnabled, false)
  assert.equal(display.loaded, false)
})

test('log display polling does not supersede a pending admin save', async () => {
  const pending = deferred()
  let reads = 0
  const load = harness({ '@/api/client': { http: {
    get: async () => { reads++; return { enabled: false } },
    patch: () => pending.promise,
  } } })
  const display = load('@/stores/clientDisplay').useClientDisplayStore()
  const saving = display.save(true)
  await display.load()
  assert.equal(reads, 0)
  pending.resolve({ enabled: true })
  assert.equal(await saving, true)
  assert.equal(display.logsEnabled, true)
  assert.equal(display.saving, false)
})
