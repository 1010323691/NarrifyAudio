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
    '@/api/workspace': {},
    ...overrides,
  }
  const modules = new Map()
  function load(name) {
    if (api[name]) return api[name]
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

test('workspace refresh clears pipeline state when the active project changes', async () => {
  const load = harness({ '@/api/workspace': {
    getWorkspace: async () => ({ set: true, workspace_id: 'B' }),
    listManagedProjects: async () => [],
  } })
  const workspace = load('@/stores/workspace').useWorkspaceStore()
  const pipeline = load('@/stores/pipelineState').usePipelineStateStore()
  workspace.setCurrent({ set: true, workspace_id: 'A' })
  pipeline.activeScript = 'A.json'
  await workspace.refresh()
  assert.equal(pipeline.activeScript, '')
  assert.equal(workspace.activeProjectId, 'B')
})

test('a create response from a signed-out account cannot select its project', async () => {
  const old = deferred()
  let selections = 0
  const load = harness({ '@/api/workspace': {
    createManagedWorkspace: () => old.promise,
    selectWorkspace: async () => { selections += 1; return {} },
    getWorkspace: async () => ({}), listManagedProjects: async () => [],
  } })
  const workspace = load('@/stores/workspace').useWorkspaceStore()
  const pending = workspace.create('Old account project')
  workspace.reset()
  old.resolve({ id: 'old-project' })
  await pending
  assert.equal(selections, 0)
  assert.equal(workspace.current, null)
})
