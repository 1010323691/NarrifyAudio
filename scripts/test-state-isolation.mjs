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
  const { waitForDurableTask } = load('@/api/persistentTasks')
  const controller = new AbortController()
  const waiting = waitForDurableTask('task-1', 50, controller.signal)
  await Promise.resolve()
  controller.abort()
  await assert.rejects(waiting, { name: 'AbortError' })
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(calls, 1)
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
