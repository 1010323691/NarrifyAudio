import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const { createPinia, setActivePinia } = require('pinia')
const { nextTick } = require('vue')
// 沙箱里没有组件实例——composable 里的生命周期钩子会被 Vue 忽略并告警，过滤掉这类噪音。
const realWarn = console.warn
console.warn = (...args) => {
  if (/called when there is no active component instance and will be ignored/.test(String(args[0] ?? ''))) return
  realWarn(...args)
}
const root = new URL('../', import.meta.url)
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

function harness(overrides = {}) {
  setActivePinia(createPinia())
  const api = {
    '@/api/config': { getConfig: async () => ({}), patchConfig: async () => ({}) },
    '@/api/client': {
      ApiError,
      http: { get: async () => ({}), post: async () => ({}), del: async () => ({}) },
    },
    '@/components/ui/toast': { useToast: () => ({ push: () => {} }) },
    '@/composables/useDurableTaskWait': { useDurableTaskWait: () => ({ track: async () => ({}) }) },
    '@/api/durableTasks': {
      listProjectDurableTasks: async () => [],
      retryDurableTask: async () => ({}),
    },
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

/** 章节 fixtures：前 5 个待核对、第 3 个附带调整。 */
function makeChapters(n) {
  return Array.from({ length: n }, (_, i) => ({
    key: `c${i + 1}`,
    seq: i + 1,
    title: `第${i + 1}章`,
    chars: 100,
    pending: i < 5,
    adjusted: i === 2,
    reasons: [i < 5 ? 'inferred' : 'kept'],
    matters: [],
  }))
}

const readyVersion = (chapters) => ({
  flow_id: 'flow-1',
  task_id: 'split-1',
  mode: 'smart',
  version_status: 'current',
  total_chars: chapters.reduce((sum, c) => sum + c.chars, 0),
  chapters,
  files: chapters.map((c, i) => ({ name: `${String(i + 1).padStart(3, '0')}.txt`, chars: c.chars })),
  matters: [],
  review_marks: [],
})

function workbenchApi({ getState, postFlow, calls }) {
  return {
    getWorkbenchState: (projectId, options) => getState(projectId, options),
    postWorkbenchFlow: async () => {
      calls.post += 1
      return postFlow ? postFlow() : {}
    },
    postReviewMark: async () => { calls.mark += 1; return {} },
    deleteReviewMark: async () => { calls.unmark += 1; return {} },
    previewUrl: () => '',
    zipUrl: () => '',
    workbenchErrorMessage: (status, body) => body?.detail?.message || String(status),
  }
}

function setupWorkbench({ getState, postFlow, extra } = {}) {
  const calls = { post: 0, mark: 0, unmark: 0, wait: 0 }
  const load = harness({
    '@/api/textFormat': workbenchApi({ getState, postFlow, calls }),
    '@/composables/useDurableTaskWait': {
      useDurableTaskWait: () => ({ track: async () => { calls.wait += 1; return {} } }),
    },
    ...extra,
  })
  const project = load('@/stores/project').useProjectStore()
  project.setCurrent({ set: true, project_id: 'P1', project_name: '测试' })
  const wb = load('@/composables/useTextFormatWorkbench').useTextFormatWorkbench()
  return { wb, project, calls, load }
}

test('no flow: the phase stays empty and parsing cannot start', async () => {
  const { wb } = setupWorkbench({
    getState: async () => ({ flow: null, version: null, next_task: null, active_tasks: [] }),
  })
  await wb.resume()
  assert.equal(wb.phase.value, 'empty')
  assert.equal(wb.canEnterParse.value, false)
  assert.equal(wb.enterParseReason.value, '尚未处理')
})

test('the recovery pump tracks the running stage to terminal, then continues exactly once', async () => {
  const chapters = makeChapters(3)
  const running = {
    flow: { id: 'flow-1', status: 'running', config_snapshot: {} },
    version: null,
    next_task: { stage: 'format', task_id: 't1', task_type: 'text.format', status: 'running' },
    active_tasks: [{ id: 't1', status: 'running' }],
  }
  const ready = {
    flow: { id: 'flow-1', status: 'ready', config_snapshot: {} },
    version: readyVersion(chapters),
    next_task: null,
    active_tasks: [],
  }
  let servedReady = false
  const { wb, calls } = setupWorkbench({
    getState: async () => (servedReady ? ready : running),
    postFlow: async () => { servedReady = true; return ready },
  })
  await wb.resume()
  assert.equal(wb.phase.value, 'ready')
  assert.equal(calls.wait, 1)
  assert.equal(calls.post, 1)
  assert.equal(wb.canEnterParse.value, true)
})

test('a failed flow stops the pump without submitting anything', async () => {
  const { wb, calls } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'failed', config_snapshot: {} },
      version: null,
      next_task: { stage: 'split', task_id: 't3', task_type: 'book.split', status: 'failed', failed: true },
      active_tasks: [],
    }),
  })
  await wb.resume()
  assert.equal(wb.phase.value, 'failed')
  assert.equal(calls.post, 0)
  assert.equal(wb.canEnterParse.value, false)
  assert.equal(wb.enterParseReason.value, '流程失败，请先重试')
})

test('live task ticks update stage progress and the whole pipeline uses 0–100 task units', async () => {
  const stages = ['format', 'analyze', 'split']
  let stageIndex = 0
  let wb
  const state = () => ({
    flow: { id: 'flow-1', status: stageIndex === 3 ? 'ready' : 'running', config_snapshot: {} },
    version: stageIndex === 3 ? readyVersion(makeChapters(2)) : null,
    next_task: stageIndex === 3 ? null : { stage: stages[stageIndex], task_id: `t${stageIndex}`, status: 'running', progress: 0 },
    active_tasks: stageIndex === 3 ? [] : [{ id: `t${stageIndex}`, status: 'running', progress: 0 }],
  })
  const setup = setupWorkbench({
    getState: async () => state(),
    postFlow: async () => { stageIndex += 1; return state() },
    extra: {
      '@/composables/useDurableTaskWait': { useDurableTaskWait: () => ({ track: async (id, onTick) => {
        for (const percent of [10, 40, 75, 100]) {
          onTick({ id, status: percent === 100 ? 'succeeded' : 'running', progress: percent })
          assert.equal(wb.nextTask.value.progress, percent)
          assert.equal(wb.activeTasks.value[0].progress, percent)
          assert.equal(wb.pipelineProgress.value, (stageIndex + percent / 100) / 3)
        }
      } }) },
    },
  })
  wb = setup.wb
  await wb.resume()
  assert.equal(wb.pipelineProgress.value, 1)
  assert.equal(setup.calls.post, 3)
})

test('late progress ticks cannot update the next project', async () => {
  const pending = deferred()
  let tick
  const running = {
    flow: { id: 'flow-1', status: 'running', config_snapshot: {} }, version: null,
    next_task: { stage: 'format', task_id: 't1', status: 'running', progress: 0 }, active_tasks: [],
  }
  const { wb, project, calls } = setupWorkbench({
    getState: async (projectId) => projectId === 'P1' ? running : {
      flow: null, version: null, next_task: null, active_tasks: [],
    },
    extra: { '@/composables/useDurableTaskWait': { useDurableTaskWait: () => ({ track: async (_id, onTick) => {
      tick = onTick
      await pending.promise
    } }) } },
  })
  const resuming = wb.resume()
  while (!tick) await nextTick()
  project.setCurrent({ set: true, project_id: 'P2', project_name: 'Next project' })
  await nextTick()
  assert.equal(tick({ id: 't1', status: 'running', progress: 75 }), false)
  assert.equal(wb.nextTask.value, null)
  pending.resolve()
  await resuming
  assert.equal(calls.post, 0)
})

test('stale versions and active tasks block entering the parser with a reason', async () => {
  const chapters = makeChapters(2)
  const { wb } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'ready', config_snapshot: {} },
      version: { ...readyVersion(chapters), version_status: 'stale' },
      next_task: null,
      active_tasks: [],
    }),
  })
  await wb.resume()
  assert.equal(wb.phase.value, 'ready')
  assert.equal(wb.canEnterParse.value, false)
  assert.equal(wb.enterParseReason.value, '该版本内容已被后续处理覆盖')
})

test('pending/adjusted filters, search and page slicing; marks toggle and advance the selection', async () => {
  const { wb, calls } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'ready', config_snapshot: {} },
      version: readyVersion(makeChapters(25)),
      next_task: null,
      active_tasks: [],
    }),
  })
  await wb.resume()
  assert.equal(wb.phase.value, 'ready')
  assert.equal(wb.pendingCount.value, 5)

  assert.equal(wb.pageCount.value, 3) // 25 章 / 每页 10
  assert.equal(wb.pagedChapters.value.length, 10)
  wb.page.value = 2
  assert.equal(wb.pagedChapters.value.length, 10)
  assert.equal(wb.pagedChapters.value[0].key, 'c11')
  wb.page.value = 3
  assert.equal(wb.pagedChapters.value.length, 5)
  assert.equal(wb.pagedChapters.value[0].key, 'c21')

  wb.filter.value = 'pending'
  await nextTick()
  assert.equal(wb.page.value, 1) // 切过滤器回到第一页
  assert.equal(wb.filteredChapters.value.length, 5)
  assert.equal(wb.filteredChapters.value.map((c) => c.key).join(','), 'c1,c2,c3,c4,c5')

  wb.filter.value = 'adjusted'
  await nextTick()
  assert.equal(wb.filteredChapters.value.map((c) => c.key).join(','), 'c3')

  wb.filter.value = 'all'
  wb.query.value = '第7章'
  await nextTick()
  assert.deepEqual(wb.filteredChapters.value.map((c) => c.key), ['c7'])

  // 待核对过滤器下标记 → 计数即时更新、选中跳到下一个待核对章。
  wb.query.value = ''
  wb.filter.value = 'pending'
  await nextTick()
  wb.selectChapter('c1')
  assert.equal(calls.mark, 0)
  await wb.toggleMark('c1')
  assert.equal(calls.mark, 1)
  assert.equal(wb.isMarked('c1'), true)
  assert.equal(wb.pendingCount.value, 4)
  assert.equal(wb.selectedKey.value, 'c2') // advanceAfterMark

  // 撤销走 DELETE，本地数组同步收缩。
  await wb.toggleMark('c1')
  assert.equal(calls.unmark, 1)
  assert.equal(wb.isMarked('c1'), false)
  assert.equal(wb.pendingCount.value, 5)
})

test('settingsDirty compares the flow config snapshot against the current config', async () => {
  const { wb, load } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'ready', config_snapshot: { x: 1, merge_adjacent_same_speaker: true } },
      version: readyVersion(makeChapters(1)),
      next_task: null,
      active_tasks: [],
    }),
  })
  await wb.resume()
  const settings = load('@/stores/settings').useSettingsStore()
  settings.config = { text: { merge_adjacent_same_speaker: true, x: 1 } }
  assert.equal(wb.settingsDirty.value, false) // 键序不同但值相同 → 规范化后相等
  settings.config = { text: { merge_adjacent_same_speaker: false, x: 1 } }
  assert.equal(wb.settingsDirty.value, true)
  settings.config = { text: { x: 1 } }
  assert.equal(wb.settingsDirty.value, true) // 缺键也判脏
})

test('an empty config snapshot (adopted legacy flow) is never dirty', async () => {
  const { wb, load } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-adopt', status: 'ready', config_snapshot: {} },
      version: readyVersion(makeChapters(1)),
      next_task: null,
      active_tasks: [],
    }),
  })
  await wb.resume()
  const settings = load('@/stores/settings').useSettingsStore()
  settings.config = { text: { sentence_break: true, x: 1 } }
  assert.equal(wb.settingsDirty.value, false) // 无快照基准 → 不显示「设置已修改」
})

test('reason filter, same-number group and dupInfo drive the reason card', async () => {
  const chapters = [
    { key: 'c1', seq: 1, title: 'A', chars: 10, pending: true, adjusted: true, reasons: ['duplicate_number'], matters: [], orig_num: 5 },
    { key: 'c2', seq: 2, title: 'B', chars: 10, pending: true, adjusted: true, reasons: ['duplicate_kept'], matters: [], orig_num: 5 },
    { key: 'c3', seq: 3, title: 'C', chars: 10, pending: false, adjusted: false, reasons: ['kept'], matters: [], orig_num: 3 },
    { key: 'c4', seq: 4, title: 'D', chars: 10, pending: true, adjusted: true, reasons: ['inferred'], matters: [], orig_num: 4 },
  ]
  const { wb } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'ready', config_snapshot: {} },
      version: readyVersion(chapters),
      next_task: null,
      active_tasks: [],
    }),
  })
  await wb.resume()
  // 可筛原因 = 出现过的非「保留」原因，按首次出现顺序
  assert.deepEqual([...wb.reasonOptions.value], ['duplicate_number', 'duplicate_kept', 'inferred'])

  // 原因筛选
  wb.reasonFilter.value = 'duplicate_kept'
  await nextTick()
  assert.equal(wb.page.value, 1) // 切筛选回第一页
  assert.deepEqual(wb.filteredChapters.value.map((c) => c.key), ['c2'])
  wb.reasonFilter.value = ''

  // 同号比较：整组展示，忽略状态筛选（组内 c2 已核对也会被排除在 pending 外，但比较模式全留）
  wb.filter.value = 'pending'
  wb.sameOrigNum.value = 5
  await nextTick()
  assert.equal(wb.page.value, 1)
  assert.deepEqual(wb.filteredChapters.value.map((c) => c.key), ['c1', 'c2'])

  // dupInfo：同原编号组的规模与位置（按正文顺序）
  wb.selectChapter('c1')
  assert.deepEqual({ ...wb.dupInfo.value }, { count: 2, index: 1 })
  wb.selectChapter('c2')
  assert.deepEqual({ ...wb.dupInfo.value }, { count: 2, index: 2 })
  wb.selectChapter('c3')
  assert.equal(wb.dupInfo.value, null) // 组小于 2 → 无重复语境
})

test('balanced siblings do not inflate original-number duplicate counts', async () => {
  let chapters = [
    { ...makeChapters(1)[0], key: 'c1', seq: 1, orig_num: 2, source_chapter_id: 'first', reasons: ['long_chapter_split'] },
    { ...makeChapters(1)[0], key: 'c2', seq: 2, orig_num: 2, source_chapter_id: 'first', reasons: ['long_chapter_split'] },
  ]
  const { wb } = setupWorkbench({
    getState: async () => ({ flow: { id: 'flow-1', status: 'ready', config_snapshot: {} }, version: readyVersion(chapters), next_task: null, active_tasks: [] }),
  })
  await wb.resume()
  wb.selectChapter('c2')
  assert.equal(wb.dupInfo.value, null)
  chapters = [...chapters, { ...chapters[0], key: 'c3', seq: 3, source_chapter_id: 'second', reasons: ['duplicate_number'] }]
  await wb.resume()
  wb.selectChapter('c2')
  assert.deepEqual({ ...wb.dupInfo.value }, { count: 2, index: 1 })
  wb.selectChapter('c3')
  assert.deepEqual({ ...wb.dupInfo.value }, { count: 2, index: 2 })
})

test('mark success pushes an undoable toast; unmark does not', async () => {
  const toasts = []
  const { wb, calls } = setupWorkbench({
    getState: async () => ({
      flow: { id: 'flow-1', status: 'ready', config_snapshot: {} },
      version: readyVersion(makeChapters(5)),
      next_task: null,
      active_tasks: [],
    }),
    extra: {
      '@/components/ui/toast': { useToast: () => ({ push: (t) => toasts.push(t) }) },
    },
  })
  await wb.resume()
  wb.filter.value = 'pending'
  await nextTick()
  wb.selectChapter('c1')
  assert.equal(toasts.length, 0)
  await wb.toggleMark('c1')
  assert.equal(calls.mark, 1)
  assert.equal(wb.selectedKey.value, 'c2') // 自动跳到下一待核对
  assert.equal(toasts.length, 1)
  assert.equal(toasts[0].title, '第1章 已标记已核对')
  assert.equal(toasts[0].description, '已跳到下一个待核对章节')
  assert.equal(typeof toasts[0].action.onClick, 'function')

  // 撤销：跳回该章并取消标记，不产生新 toast
  toasts[0].action.onClick()
  assert.equal(wb.selectedKey.value, 'c1')
  await new Promise((r) => setTimeout(r, 50)) // toggleMark 是异步的
  assert.equal(calls.unmark, 1)
  assert.equal(wb.isMarked('c1'), false)
  assert.equal(toasts.length, 1)

  // 标记中间项：前进到下一待核对（c4），而不是跳回第一项
  toasts.length = 0
  wb.selectChapter('c3')
  await wb.toggleMark('c3')
  assert.equal(calls.mark, 2)
  assert.equal(wb.selectedKey.value, 'c4')
  assert.equal(toasts.length, 1)
  assert.equal(toasts[0].title, '第3章 已标记已核对')
})

test('a late state response from the previous project cannot overwrite the new project state', async () => {
  const old = deferred()
  const calls = { post: 0, mark: 0, unmark: 0, wait: 0 }
  const oldState = {
    flow: { id: 'flow-A', status: 'ready', config_snapshot: {} },
    version: readyVersion(makeChapters(1)),
    next_task: null,
    active_tasks: [],
  }
  const load = harness({
    '@/api/textFormat': workbenchApi({
      calls,
      getState: (pid) => {
        if (pid === 'P1') return old.promise
        throw new Error('project B has no state (simulated failure)')
      },
    }),
    '@/composables/useDurableTaskWait': {
      useDurableTaskWait: () => ({ track: async () => (calls.wait += 1, {}) }),
    },
  })
  const project = load('@/stores/project').useProjectStore()
  project.setCurrent({ set: true, project_id: 'P1', project_name: '测试' })
  const wb = load('@/composables/useTextFormatWorkbench').useTextFormatWorkbench()
  const resume = wb.resume()
  project.setCurrent({ set: true, project_id: 'P2', project_name: '测试' })
  await nextTick() // 让 P2 的 resume 先跑（并递增 loadToken）
  old.resolve(oldState) // P1 的迟到响应
  await resume
  assert.equal(wb.flow.value, null)
  assert.equal(wb.version.value, null)
})


test('manual state refresh explicitly requests a read-only server snapshot', async () => {
  let readOptions
  const { wb, calls } = setupWorkbench({
    getState: async (_projectId, options) => {
      readOptions = options
      return {
        flow: { id: 'flow-1', status: 'running', config_snapshot: {} },
        version: null,
        next_task: { stage: 'format', task_id: 'task-1', status: 'running', progress: 30 },
        active_tasks: [],
      }
    },
  })
  assert.equal(await wb.refreshState({ recover: false }), true)
  assert.deepEqual(readOptions, { recover: false })
  assert.equal(wb.nextTask.value.progress, 30)
  assert.equal(calls.post, 0)
  assert.equal(calls.wait, 0)
})


test('state API keeps lifecycle recovery by default and opts out for manual refresh', async () => {
  const urls = []
  const load = harness({ '@/api/client': { http: { get: async url => { urls.push(url); return {} } } } })
  const { getWorkbenchState } = load('@/api/textFormat')
  await getWorkbenchState('P1')
  await getWorkbenchState('P1', { recover: false })
  assert.deepEqual(urls, ['/api/v1/projects/P1/text-format/state', '/api/v1/projects/P1/text-format/state?recover=false'])
})
