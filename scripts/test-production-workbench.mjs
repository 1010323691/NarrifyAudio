import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const { parse, compileTemplate } = require('@vue/compiler-sfc')

test('production templates compile with the actual Vue template compiler', () => {
  for (const file of ['components/WorkbenchToolbar.vue', 'components/WorkbenchActionBar.vue', 'components/ProductionWorkbench.vue', 'views/TextFormat.vue', 'views/ScriptParse.vue', 'views/voices/VoicesWorkbench.vue', 'views/Voices.vue', 'views/BatchTTS.vue', 'views/Merge.vue', 'views/BGM.vue']) {
    const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
    const { descriptor, errors } = parse(source)
    assert.deepEqual(errors, [])
    const result = compileTemplate({ source: descriptor.template.content, filename: file, id: file })
    assert.deepEqual(result.errors, [], file)
  }
})
function deferred() {
  let resolve
  const promise = new Promise((done) => {
    resolve = done
  })
  return { promise, resolve }
}

test('nested production dialogs retain the outer scroll lock and restore each opener', async () => {
  const module = { exports: {} }
  const host = { style: { overflow: 'auto' } }
  const doc = { activeElement: null, querySelector: () => host }
  const control = (name) => ({ isConnected: true, focus() { doc.activeElement = this }, name })
  const outerOpener = control('chapter'), innerOpener = control('change music')
  const source = readFileSync(new URL('../src/composables/useWorkbenchDialog.ts', import.meta.url), 'utf8')
  runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, {
    module, exports: module.exports, document: doc,
    require: () => ({ ...vue, onDeactivated() {}, onBeforeUnmount() {} }),
  })
  const outer = vue.ref(false), inner = vue.ref(false)
  const panel = vue.ref({ querySelector: () => control('first control') })
  module.exports.useWorkbenchDialog(outer, panel, () => {}, () => outerOpener)
  module.exports.useWorkbenchDialog(inner, panel, () => {}, () => innerOpener)
  outer.value = true
  await vue.nextTick()
  inner.value = true
  await vue.nextTick()
  inner.value = false
  await vue.nextTick()
  await vue.nextTick()
  assert.equal(host.style.overflow, 'hidden')
  assert.equal(doc.activeElement, innerOpener)
  outer.value = false
  await vue.nextTick()
  await vue.nextTick()
  assert.equal(host.style.overflow, 'auto')
  assert.equal(doc.activeElement, outerOpener)
})
function harness(view, api = {}) {
  const hooks = { activate: [], deactivate: [], unmount: [] }
  const auth = vue.reactive({ user: { id: 'user-a' } })
  const project = vue.reactive({ activeProjectId: 'project-a' })
  const taskStore = vue.reactive({
    projectTasks: [],
    activeTasks: () => [],
    refresh: async () => {},
    control: async () => {},
  })
  const settings = vue.reactive({ loaded: true, config: { tts: { batch_concurrency: 4 }, ui: {} } })
  const calls = []
  const overrides = {
    vue: {
      ...vue,
      onMounted() {},
      onActivated(fn) {
        hooks.activate.push(fn)
      },
      onDeactivated(fn) {
        hooks.deactivate.push(fn)
      },
      onBeforeUnmount(fn) {
        hooks.unmount.push(fn)
      },
      onUnmounted(fn) {
        hooks.unmount.push(fn)
      },
    },
    'vue-router': { useRouter: () => ({ push() {} }) },
    '@/stores/auth': { useAuthStore: () => auth },
    '@/stores/project': { useProjectStore: () => project },
    '@/stores/task': { useTaskStore: () => taskStore },
    '@/stores/settings': { useSettingsStore: () => settings },
    '@/stores/pipelineState': {
      usePipelineStateStore: () => vue.reactive({ activeScript: '', recordMerge() {} }),
    },
    '@/composables/useProjectGate': { useProjectGate: () => ({ projectSet: vue.ref(true) }) },
    '@/composables/useDurableTaskWait': {
      useDurableTaskWait: () => ({ wait: async () => ({ status: 'succeeded' }) }),
    },
    '@/components/ui/toast': { useToast: () => ({ push: (value) => calls.push(value) }) },
    '@/components/ui/dialog': { showConfirm: async () => true },
    '@/api/files': { listDir: async () => ({ items: [] }), ...api },
    '@/api/tts': {
      batchStatusFiles: async () => ({ files: [] }),
      mergeStatusPackages: async () => ({ packages: [] }),
      runBatch: async (value) => {
        calls.push(value)
        return { task_id: 'new-task' }
      },
      runMerge: async (value) => {
        calls.push(value)
      },
      ...api,
    },
    '@/api/bgm': {
      getChapters: async () => ({ chapters: [], mode: 'random' }),
      mixChapters: async (value) => {
        calls.push(value)
      },
      ...api,
    },
    '@/api/music': { getLibrary: async () => ({ tracks: {} }) },
  }
  const cache = new Map()
  function evaluate(source) {
    const module = { exports: {} }
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText
    runInNewContext(code, {
      module,
      exports: module.exports,
      DOMException,
      console,
      setInterval: () => 1,
      clearInterval() {},
      setTimeout,
      clearTimeout,
      document: { activeElement: null, querySelector: () => null },
      window: { setTimeout, clearTimeout },
      require(name) {
        if (overrides[name]) return overrides[name]
        if (name.includes('useWorkbench') || name.includes('useLabelDerivedTasks')) {
          const base = name.split('/').pop()
          if (!cache.has(base))
            cache.set(
              base,
              evaluate(
                readFileSync(new URL(`../src/composables/${base}.ts`, import.meta.url), 'utf8'),
              ),
            )
          return cache.get(base)
        }
        return { default: {} }
      },
    })
    return module.exports
  }
  const exports = {
    BatchTTS:
      'refreshRows, refreshStatusesOnly, fileNames, statuses, filesError, filesLoading, rows, workRows, selected, selectedNames, selectedRemaining, selectedTotal, doRun, doRunAll, status, busy, retryBatch, taskId',
    Merge:
      'refreshRows, pkgNames, pkgStats, diskMp3, rows, workRows, selected, selectedNames, selectionReady, selectAllIncludingDone, doRun, rowsLoading, rowsError',
    BGM: 'refreshRows, chapterRows, rows, workRows, selected, selectedMixable, doMix, doMixRow, loading, loadError, mode, switchMode, matchFeedback, doRematchSelected',
  }
  const script = readFileSync(new URL(`../src/views/${view}.vue`, import.meta.url), 'utf8').match(
    /<script setup lang="ts">([\s\S]*?)<\/script>/,
  )[1]
  return {
    ...evaluate(script + `\nexport { ${exports[view]} };`),
    auth,
    project,
    hooks,
    taskStore,
    calls,
  }
}
const file = (name, complete = false) => ({
  name,
  total: 10,
  completed: complete ? 10 : 3,
  remaining: complete ? 0 : 7,
  complete,
  speakers: 1,
  ready: 1,
  missing: [],
})
const chapter = (stem, ready = true) => ({
  stem,
  narration_exists: ready,
  mix_exists: false,
  assignment: ready ? { music: null } : null,
  music_missing: false,
  segment_music_missing: false,
  segment_analysis: null,
  timeline: null,
})

test('synthesis refresh is atomic and the newest response wins', async () => {
  const first = deferred(),
    second = deferred()
  let n = 0
  const h = harness('BatchTTS', {
    listDir: () => (++n === 1 ? first.promise : second.promise),
    batchStatusFiles: async (names) => ({ files: names.map((name) => file(name)) }),
  })
  const old = h.refreshRows(),
    latest = h.refreshRows()
  second.resolve({ items: [{ name: 'new.json', is_dir: false }] })
  await latest
  first.resolve({ items: [{ name: 'old.json', is_dir: false }] })
  await old
  assert.equal(h.fileNames.value[0], 'new.json')
  assert.equal(h.statuses.value[0].name, 'new.json')
  assert.equal(h.filesLoading.value, false)
})

for (const change of ['project', 'account', 'leave'])
  test(`late synthesis response is discarded after ${change}`, async () => {
    const pending = deferred()
    const h = harness('BatchTTS', { listDir: () => pending.promise })
    const request = h.refreshRows()
    if (change === 'project') h.project.activeProjectId = 'project-b'
    else if (change === 'account') h.auth.user = { id: 'user-b' }
    else h.hooks.deactivate.forEach((fn) => fn())
    pending.resolve({ items: [{ name: 'late.json', is_dir: false }] })
    await request
    assert.equal(h.fileNames.value.length, 0)
    assert.equal(h.statuses.value.length, 0)
  })

test('a rapid A to B to A project switch invalidates the original request', async () => {
  const pending = deferred()
  const h = harness('BatchTTS', { listDir: () => pending.promise })
  const request = h.refreshRows()
  h.project.activeProjectId = 'project-b'
  h.project.activeProjectId = 'project-a'
  pending.resolve({ items: [{ name: 'stale.json', is_dir: false }] })
  await request
  assert.equal(h.fileNames.value.length, 0)
})

test('selection summary preserves incremental and full synthesis semantics', () => {
  const h = harness('BatchTTS')
  h.fileNames.value = ['pending.json', 'done.json']
  h.statuses.value = [file('pending.json'), file('done.json', true)]
  h.selected['pending.json'] = true
  h.selected['done.json'] = true
  assert.equal(h.selectedRemaining.value, 7)
  assert.equal(h.selectedTotal.value, 20)
})

test('a file disappearing during refresh is removed from selection', async () => {
  const h = harness('BatchTTS')
  h.selected['removed.json'] = true
  await h.refreshRows()
  assert.equal(h.selected['removed.json'], undefined)
})

test('a previous merged file is hidden when synthesis is incomplete', () => {
  const h = harness('Merge')
  h.pkgNames.value = ['chapter']
  h.pkgStats.value = { chapter: file('chapter') }
  h.diskMp3.value = { chapter: 'chapter.mp3' }
  assert.equal(h.rows.value[0].merged, undefined)
  h.pkgStats.value.chapter = file('chapter', true)
  assert.equal(h.rows.value[0].merged, 'chapter.mp3')
})

test('bulk merge selects complete inputs and blocks a drifting selection', async () => {
  const h = harness('Merge')
  h.pkgNames.value = ['ready', 'blocked']
  h.pkgStats.value = { ready: file('ready', true), blocked: file('blocked') }
  h.selectAllIncludingDone()
  assert.equal(h.selected.ready, true)
  assert.equal(h.selected.blocked, undefined)
  h.pkgStats.value.ready.complete = false
  assert.equal(h.selectionReady.value, false)
  await h.doRun()
  assert.equal(h.calls.length, 0)
})

test('BGM mixing submits only the explicitly summarized eligible scope', async () => {
  const h = harness('BGM')
  h.chapterRows.value = [chapter('ready'), chapter('missing', false)]
  h.selected.ready = true
  h.selected.missing = true
  assert.equal(h.selectedMixable.value.join(','), 'ready')
  await h.doMix()
  assert.equal(h.calls[0].join(','), 'ready')
})

test('switching BGM modes never submits matching or changes existing assignments', async () => {
  const h = harness('BGM', { matchChapters: async () => { throw new Error('unexpected submission') } })
  h.chapterRows.value = [chapter('existing')]
  const before = JSON.stringify(h.chapterRows.value)
  h.switchMode('segment')
  assert.equal(h.mode.value, 'segment')
  assert.equal(JSON.stringify(h.chapterRows.value), before)
  await h.refreshRows()
  assert.equal(h.mode.value, 'segment')
  h.switchMode('random')
  assert.equal(h.mode.value, 'random')
  assert.equal(h.calls.length, 0)
  await h.refreshRows()
  assert.equal(h.mode.value, 'random')
})

test('BGM queued matching is distinguished from running and blocks conflicting mode changes', () => {
  const h = harness('BGM')
  h.taskStore.projectTasks = [{ id: 'match', task_type: 'bgm.match', module: 'bgm', status: 'queued' }]
  assert.match(h.matchFeedback.value, /已排队/)
  h.switchMode('segment')
  assert.equal(h.mode.value, 'random')
  h.taskStore.projectTasks[0].status = 'running'
  assert.match(h.matchFeedback.value, /正在执行/)
  h.taskStore.projectTasks[0].status = 'cancelled'
  h.switchMode('segment')
  assert.equal(h.mode.value, 'segment')
})

test('segment mode never hides running tasks or missing music', () => {
  const h = harness('BGM')
  h.mode.value = 'segment'
  h.chapterRows.value = [{ ...chapter('running'), music_missing: true }]
  h.taskStore.projectTasks = [
    { id: 't', module: 'bgm-segment', label: '分析：running', status: 'running', seq: 1 },
  ]
  assert.equal(h.rows.value[0].label, '段落分析中')
  h.taskStore.projectTasks = []
  assert.equal(h.rows.value[0].label, '曲目已删除')
})

test('real merge API labels attach pending and failed tasks to their chapter', () => {
  const h = harness('Merge')
  const pkg = 'QA chapter：long name'
  h.pkgNames.value = [pkg]
  h.pkgStats.value = { [pkg]: { total: 3, completed: 3, remaining: 0, complete: true } }
  const task = { id: 'merge-task', module: 'merge', label: `merge-audio: ${pkg}`, status: 'pending', seq: 1 }
  h.taskStore.projectTasks = [task]
  assert.equal(h.rows.value[0].task?.id, task.id)
  assert.equal(h.rows.value[0].ready, false)
  h.taskStore.projectTasks[0].status = 'failed'
  assert.equal(h.rows.value[0].failedTask?.id, task.id)
  assert.equal(h.rows.value[0].label, '合并失败')
})

test('rapid single chapter clicks create one mixing submission', async () => {
  const pending = deferred()
  const h = harness('BGM', {
    mixChapters: (value) => {
      h.calls.push(value)
      return pending.promise
    },
  })
  h.chapterRows.value = [chapter('ready')]
  const first = h.doMixRow('ready'),
    duplicate = h.doMixRow('ready')
  await duplicate
  assert.equal(h.calls.length, 1)
  pending.resolve({})
  await first
})

test('retry attaches the batch only after the failed task has been requeued', async () => {
  const pending = deferred()
  const h = harness('BatchTTS')
  h.taskStore.projectTasks = [{ id: 'failed-batch', module: 'tts-batch', status: 'failed', seq: 1, error: 'test' }]
  h.taskStore.control = async () => { await pending.promise; h.taskStore.projectTasks[0].status = 'pending' }
  const request = h.retryBatch()
  await vue.nextTick()
  assert.equal(h.taskId.value, null)
  assert.equal(h.busy.value, true)
  pending.resolve()
  await request
  await vue.nextTick()
  assert.equal(h.taskId.value, 'failed-batch')
  assert.equal(h.busy.value, true)
})
