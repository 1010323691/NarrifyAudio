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
