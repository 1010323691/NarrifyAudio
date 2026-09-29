import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const { createPinia, setActivePinia } = require('pinia')
const { nextTick, reactive } = require('vue')
// 沙箱里没有组件实例——composable 里的生命周期钩子会被 Vue 忽略并告警，过滤掉这类噪音。
const realWarn = console.warn
console.warn = (...args) => {
  const msg = String(args[0] ?? '')
  if (/called when there is no active component instance/.test(msg)) return
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

/** 解析页 state fixture（version 模式）：files 决定行；source 与 files 同步生成。 */
function stateV(files, { flowId = 'flow-1', versionStatus = 'current', busy = false } = {}) {
  return {
    source: {
      mode: 'version',
      version: {
        flow_id: flowId,
        version_status: versionStatus,
        chapters: files.map((f, i) => ({
          key: null,
          seq: i + 1,
          numStr: String(i + 1).padStart(3, '0'),
          title: `第${i + 1}章`,
          chars: f.chars ?? 100,
        })),
        files: files.map((f) => ({ file_id: f.file_id, name: f.name, chars: f.chars ?? 100 })),
      },
    },
    text_format_busy: busy,
    files,
  }
}

function harness(overrides = {}, extraGlobals = {}) {
  setActivePinia(createPinia())
  const api = {
    '@/api/config': { getConfig: async () => ({}), patchConfig: async () => ({}) },
    '@/api/admin': { getApplicationSettings: async () => ({}), updateApplicationSettings: async () => ({}) },
    '@/api/client': {
      ApiError,
      API_BASE: '',
      http: { get: async () => ({}), post: async () => ({}), del: async () => ({}) },
    },
    '@/components/ui/toast': { useToast: () => ({ push: () => {} }) },
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
      // 组合函数用 window.setTimeout 做在途完成后的 600ms 防抖整表刷新。
      window: { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (id) => clearTimeout(id) },
      setTimeout, clearTimeout, DOMException, AbortController,
      fetch: (...a) => { throw new Error('fetch not stubbed in this test') },
      ...extraGlobals,
    }, { filename: path })
    return module.exports
  }
  return load
}

/** 搭台：project（真实 store）+ task store（可脚本化的桩）+ script api（可脚本化的桩）。
 *  `config` 由 getConfig 桩直接给出——setCurrent 会异步触发 settings.load()，
 *  手改 settings.config 会被那次加载覆盖（与真实「配置随工程」行为对齐）。 */
async function setup({ stateFor, run, config, fetchImpl } = {}) {
  const calls = { state: 0, run: 0, cancel: 0, patch: 0 }
  const toasts = []
  const tasks = reactive({ projectTasks: [], refresh: async () => {}, control: async () => {}, bindProject: () => {} })
  const api = {
    '@/api/script': {
      getScriptParseState: async (pid) => {
        calls.state += 1
        if (stateFor) return stateFor(pid, calls.state)
        throw new Error('stateFor not provided')
      },
      runScriptParse: async (pid, files, checks) => {
        calls.run += 1
        if (run) return run(pid, files, checks)
        return {
          task_ids: files.map((_, i) => `tA${i + 1}`),
          files: files.map((f, i) => ({ name: f.name, task_id: `tA${i + 1}`, input_sha256: null })),
        }
      },
      cancelParseBatch: async (ids) => { calls.cancel += 1; return { cancelled: ids, batches_stopped: 0 } },
      scriptParseResultUrl: (pid, fileId) => `/res/${fileId}`,
    },
    '@/api/config': {
      getConfig: async () => (config ?? {}),
      patchConfig: async () => { calls.patch += 1; return {} },
    },
    '@/api/textFormat': { previewUrl: (pid, fid, name) => `/pf/${fid}/${name}` },
    '@/utils/fileops': { downloadUrl: (mod, name) => `/dl/${mod}/${name}` },
    '@/stores/task': { useTaskStore: () => tasks },
    '@/components/ui/toast': { useToast: () => ({ push: (t) => toasts.push(t) }) },
  }
  const load = harness({ ...api }, { fetch: fetchImpl ?? (() => { throw new Error('fetch not stubbed in this test') }) })
  const project = load('@/stores/project').useProjectStore()
  project.setCurrent({ set: true, project_id: 'P1', project_name: '测试' })
  // 等 setCurrent 触发的 settings.load() 落定再建组合函数——真实时序里
  // 「配置随工程」先于页面进入（initialChecks 读取时配置已就绪）。
  await new Promise((r) => setTimeout(r, 0))
  const wb = load('@/composables/useScriptParseWorkbench').useScriptParseWorkbench()
  return { wb, project, tasks, calls, toasts, load }
}

test('row semantics: two axes (task execution + result availability) render independently', async () => {
  const files = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: { id: 't1', status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null }, result: { task_id: 't1', file_id: 'r1', sha256: 'b1', source_sha256: 'a1', verified: true }, result_status: 'usable', chars: 100 },
    { name: '002.txt', input: { name: '002.txt', sha256: 'a2', size: 1 }, latest_task: { id: 't2', status: 'failed', progress: 1, error: 'LLM 超时', created_at: null, finished_at: null }, result: { task_id: 't0', file_id: 'r2', sha256: 'b2', source_sha256: 'a2', verified: true }, result_status: 'usable', chars: 100 },
    { name: '003.txt', input: { name: '003.txt', sha256: 'a3', size: 1 }, latest_task: null, result: null, result_status: null, chars: 100 },
    { name: '004.txt', input: { name: '004.txt', sha256: 'a4', size: 1 }, latest_task: { id: 't4', status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null }, result: { task_id: 't4', file_id: 'r4', sha256: 'b4', source_sha256: 'other', verified: true }, result_status: 'stale', chars: 100 },
  ]
  const { wb } = await setup({ stateFor: async () => stateV(files) })
  await wb.refreshState()
  const rows = wb.rows.value
  assert.deepEqual(rows.map((r) => r.status), ['done', 'failed', 'pending', 'stale'])
  assert.deepEqual(rows.map((r) => r.tone), ['emerald', 'red', 'muted', 'amber'])
  assert.equal(rows[1].label, '失败')
  assert.equal(rows[1].retryable, true)
  assert.ok(rows[1].chapter.result) // 失败的重新解析 + 仍可用的旧结果：结果轴保留（右栏展示旧结果）
  assert.equal(rows[3].label, '输入已变更')
  assert.equal(wb.doneCount.value, 1)
  assert.equal(wb.busy.value, false)
})

test('check defaults come from project config; missing keys default to true', async () => {
  const { wb } = await setup({
    stateFor: async () => stateV([]),
    config: { generation: { validate_instructs: false } },
  })
  assert.equal(wb.checks.validate_instructs, false)
  assert.equal(wb.checks.check_chunk_alignment, true) // 未记忆 = 默认开
  assert.equal(Object.keys(wb.checks).length, 6)
})

test('submitting: the session mapping + task-store snapshot drive the in-flight row', async () => {
  const files = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: null, result: null, result_status: null },
    { name: '002.txt', input: { name: '002.txt', sha256: 'a2', size: 1 }, latest_task: null, result: null, result_status: null },
  ]
  const { wb, tasks, calls, toasts } = await setup({
    stateFor: async () => stateV(files),
    config: { llm: { model_name: 'test-model' } },
  })
  await wb.refreshState()
  wb.selectScope('pending')
  assert.equal(wb.selectedCount.value, 2)
  assert.equal(await wb.startParse(), true)
  assert.equal(calls.run, 1)
  assert.equal(calls.patch, 1) // 勾选记忆进项目配置（fire-and-forget 已完成）
  assert.equal(toasts.length, 1)
  // state 尚未刷新（latest_task 还是 null）——startParse 末尾的 fire-and-forget
  // refreshState 落地后，行才读到本会话提交映射 + store 快照
  tasks.projectTasks = [
    { id: 'tA1', status: 'running', progress: 0.4, phase: 'parse', current: '', error: '' },
    { id: 'tA2', status: 'queued', progress: 0, current: '', error: '' },
  ]
  await new Promise((r) => setTimeout(r, 20))
  const [r1, r2] = wb.rows.value
  assert.equal(r1.taskId, 'tA1') // 提交映射：name → 本会话任务
  assert.equal(r1.status, 'active')
  assert.equal(r1.label, '解析中')
  assert.equal(r1.progress, 0.4)
  assert.equal(r2.status, 'active')
  assert.equal(wb.busy.value, true)
  // 在途期间再点「开始解析」被 busy 门控拒绝（无新提交）
  assert.equal(await wb.startParse(), false)
  assert.equal(calls.run, 1)
})

test('startParse guards: text-format busy / stale version / empty llm model all block with a destructive toast', async () => {
  const files = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: null, result: null, result_status: null },
  ]
  const mk = async (over = {}) => {
    const t = await setup({ ...over })
    await t.wb.refreshState()
    t.wb.selectScope('pending')
    return t
  }
  {
    const { wb, toasts } = await mk({ stateFor: async () => stateV(files, { busy: true }) })
    assert.equal(await wb.startParse(), false)
    assert.equal(toasts.at(-1).variant, 'destructive')
    assert.match(toasts.at(-1).title, /排版与分册/)
  }
  {
    const { wb, toasts } = await mk({ stateFor: async () => stateV(files, { versionStatus: 'stale' }) })
    assert.equal(await wb.startParse(), false)
    assert.equal(toasts.at(-1).variant, 'destructive')
    assert.match(toasts.at(-1).title, /已被后续处理覆盖/)
  }
  {
    const { wb, toasts } = await mk({ config: { llm: { model_name: '' } }, stateFor: async () => stateV(files) })
    assert.equal(await wb.startParse(), false)
    assert.equal(toasts.at(-1).variant, 'destructive')
    assert.match(toasts.at(-1).title, /LLM 模型/)
  }
})

test('an in-flight task finishing in the task store triggers the debounced whole-table refresh', async () => {
  const running = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: { id: 't1', status: 'running', progress: 0.5, error: '', created_at: null, finished_at: null }, result: null, result_status: null },
  ]
  const done = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: { id: 't1', status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null }, result: { task_id: 't1', file_id: 'r1', sha256: 'b1', source_sha256: 'a1', verified: true }, result_status: 'usable' },
  ]
  let serve = running
  const { wb, tasks, calls } = await setup({ stateFor: async () => stateV(serve) })
  await wb.refreshState()
  assert.equal(wb.rows.value[0].status, 'active')
  assert.equal(calls.state, 1)
  // store 快照先于 state 到达终态 → activeIds 收缩 → 600ms 防抖后整表刷新
  serve = done
  tasks.projectTasks = [{ id: 't1', status: 'succeeded', progress: 1, error: '' }]
  await nextTick()
  assert.equal(wb.rows.value[0].status, 'pending') // 新快照已生效，但整表 state 还没刷新
  await new Promise((r) => setTimeout(r, 700))
  assert.equal(calls.state, 2)
  assert.equal(wb.rows.value[0].status, 'done')
})

test('filters, search and pagination over chapter rows', async () => {
  const files = Array.from({ length: 25 }, (_, i) => {
    const usable = i < 5
    return {
      name: `${String(i + 1).padStart(3, '0')}.txt`,
      input: { name: `${String(i + 1).padStart(3, '0')}.txt`, sha256: `a${i}`, size: 1 },
      latest_task: usable ? { id: `t${i}`, status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null } : null,
      result: usable ? { task_id: `t${i}`, file_id: `r${i}`, sha256: `b${i}`, source_sha256: `a${i}`, verified: true } : null,
      result_status: usable ? 'usable' : null,
    }
  })
  const { wb } = await setup({ stateFor: async () => stateV(files) })
  await wb.refreshState()
  assert.equal(wb.total.value, 25)
  assert.equal(wb.pageCount.value, 2) // 25 章 / 每页 20
  assert.equal(wb.pagedRows.value.length, 20)
  wb.page.value = 2
  assert.equal(wb.pagedRows.value.length, 5)
  assert.equal(wb.pagedRows.value[0].chapter.name, '021.txt')

  wb.filter.value = 'done'
  await nextTick()
  assert.equal(wb.page.value, 1) // 切过滤器回第一页
  assert.equal(wb.filteredRows.value.length, 5)
  wb.filter.value = 'pending'
  await nextTick()
  assert.equal(wb.filteredRows.value.length, 20)
  assert.equal(wb.pageCount.value, 1)

  wb.query.value = '007'
  await nextTick()
  assert.deepEqual(wb.filteredRows.value.map((r) => r.chapter.name), ['007.txt'])
  wb.query.value = '第21章'
  await nextTick()
  assert.deepEqual(wb.filteredRows.value.map((r) => r.chapter.name), ['021.txt'])
  wb.query.value = ''
  await nextTick()
  assert.equal(wb.filteredRows.value.length, 20)
})

test('selectScope: pending excludes done and active; failed = failed + timeout', async () => {
  const mk = (name, t, rs, taskStatus) => ({
    name,
    input: { name, sha256: name, size: 1 },
    latest_task: taskStatus ? { id: `t-${name}`, status: taskStatus, progress: 0, error: '', created_at: null, finished_at: null } : null,
    result: rs ? { task_id: 'x', file_id: `r-${name}`, sha256: 'b', source_sha256: 'a', verified: true } : null,
    result_status: rs,
  })
  const files = [
    mk('001.txt', 'usable', 'usable', 'succeeded'),
    mk('002.txt', 'usable', 'usable', 'succeeded'),
    mk('003.txt', null, null, 'running'),
    mk('004.txt', null, null, 'failed'),
    mk('005.txt', null, null, 'timeout'),
    mk('006.txt', 'stale', 'stale', 'succeeded'),
    mk('007.txt', null, null, null),
  ]
  const { wb } = await setup({ stateFor: async () => stateV(files) })
  await wb.refreshState()
  const sel = () => wb.selected.value
  wb.selectScope('pending')
  assert.equal(wb.selectedCount.value, 4) // failed / timeout / stale / pending
  assert.equal(sel()['003.txt'], undefined) // 在途章不勾
  assert.equal(sel()['001.txt'], undefined) // done 不勾
  wb.selectScope('failed')
  assert.deepEqual(Object.keys(sel()).sort(), ['004.txt', '005.txt'])
  wb.selectScope('done')
  assert.deepEqual(Object.keys(sel()).sort(), ['001.txt', '002.txt'])
  wb.selectScope('all')
  assert.equal(wb.selectedCount.value, 6) // 全部但在途除外
  wb.clearSelection()
  assert.equal(wb.selectedCount.value, 0)
})

test('selectChapter tab default; empty results fall back to source until the user chooses', async () => {
  const completed = []
  // 与浏览器同语义：已 abort 的 fetch 不再完成（组合函数的 token/abort 竞态保护依赖这点）。
  const fetchImpl = (url, opts) => {
    let res
    const body =
      url === '/res/r1' ? [] :
      url === '/res/r3' ? [{ speaker: 'A', text: 'hi' }] : null
    if (body === null) {
      res = { status: 200, ok: true, json: async () => [], text: async () => '原文' }
    } else {
      res = { status: 200, ok: true, json: async () => body, text: async () => '' }
    }
    if (opts?.signal?.aborted) {
      const e = new Error('aborted'); e.name = 'AbortError'
      return Promise.reject(e)
    }
    return new Promise((resolve, reject) => {
      opts?.signal?.addEventListener?.('abort', () => {
        const e = new Error('aborted'); e.name = 'AbortError'
        reject(e)
      }, { once: true })
      setTimeout(() => { completed.push(url); resolve(res) }, 5)
    })
  }
  const files = [
    { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: { id: 't1', status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null }, result: { task_id: 't1', file_id: 'r1', sha256: 'b1', source_sha256: 'a1', verified: true }, result_status: 'usable' },
    { name: '002.txt', input: { name: '002.txt', sha256: 'a2', size: 1 }, latest_task: null, result: null, result_status: null },
    { name: '003.txt', input: { name: '003.txt', sha256: 'a3', size: 1 }, latest_task: { id: 't3', status: 'succeeded', progress: 1, error: '', created_at: null, finished_at: null }, result: { task_id: 't3', file_id: 'r3', sha256: 'b3', source_sha256: 'a3', verified: true }, result_status: 'usable' },
  ]
  const { wb } = await setup({ stateFor: async () => stateV(files), fetchImpl })
  await wb.refreshState()
  // 选中章节由 watch 自动触发 loadResult/loadSource（页面同款时序），等它落地。
  const settle = () => new Promise((r) => setTimeout(r, 20))

  // 无结果章 → 直接进原文页
  wb.selectChapter('002.txt')
  assert.equal(wb.tab.value, 'source')

  // 有结果章默认进结果页；空条目落地后回落原文（用户未手动切过）
  wb.selectChapter('001.txt')
  assert.equal(wb.tab.value, 'result')
  await settle()
  assert.equal(wb.resultPreview.value.status, 'ready')
  assert.equal(wb.resultPreview.value.entries.length, 0)
  assert.equal(wb.tab.value, 'source')

  // 手动切回结果页：本次停留期间回落不再干预
  wb.setTab('result')
  assert.equal(wb.tab.value, 'result')
  // 重新选中该章（切章重置手动记录）→ 空条目缓存 → 回落原文
  wb.selectChapter('003.txt')
  wb.selectChapter('001.txt')
  await settle()
  assert.equal(wb.tab.value, 'source')

  // 非空结果保持结果页
  wb.selectChapter('003.txt')
  assert.equal(wb.tab.value, 'result')
  await settle()
  assert.equal(wb.resultPreview.value.entries.length, 1)
  assert.equal(wb.tab.value, 'result')

  // 缓存：重新选中已加载章零新增 fetch（重选前 /res/ 已完成数 = 重选后）
  const resDone = completed.filter((u) => u.startsWith('/res/')).length
  wb.selectChapter('001.txt')
  await settle()
  assert.equal(wb.tab.value, 'source') // 空条目缓存 → 回落原文
  assert.equal(completed.filter((u) => u.startsWith('/res/')).length, resDone)
})

test('a late state response from the previous project cannot overwrite the new project state', async () => {
  const old = deferred()
  const stateFor = (pid, n) => {
    if (pid === 'P1') return old.promise
    return Promise.resolve(stateV([{ name: 'p2-001.txt', input: { name: 'p2-001.txt', sha256: 'z', size: 1 }, latest_task: null, result: null, result_status: null }]))
  }
  const { wb, project } = await setup({ stateFor })
  const first = wb.refreshState() // P1 的请求在途
  project.setCurrent({ set: true, project_id: 'P2', project_name: '测试' })
  await nextTick() // P2 的 resume（reset + refresh）先跑，递增 loadToken
  old.resolve(stateV([{ name: 'p1-001.txt', input: null, latest_task: null, result: null, result_status: null }]))
  await first
  assert.equal(wb.state.value.source.mode, 'version')
  assert.deepEqual(wb.rows.value.map((r) => r.chapter.name), ['p2-001.txt']) // P1 迟到响应被丢弃
})

test('selection survives a state refresh; names that vanished from the list are pruned', async () => {
  const keep = { name: '002.txt', input: { name: '002.txt', sha256: 'a2', size: 1 }, latest_task: null, result: null, result_status: null }
  const gone = { name: '001.txt', input: { name: '001.txt', sha256: 'a1', size: 1 }, latest_task: null, result: null, result_status: null }
  let serve = [gone, keep]
  const { wb } = await setup({ stateFor: async () => stateV(serve) })
  await wb.refreshState()
  wb.toggleSelect('001.txt')
  wb.toggleSelect('002.txt')
  wb.selectChapter('001.txt')
  assert.equal(wb.selectedCount.value, 2)
  serve = [keep] // 重新分册后 001.txt 消失
  await wb.refreshState()
  assert.equal(wb.selectedCount.value, 1)
  assert.equal(wb.selected.value['002.txt'], true) // 仍存在的选中保留
  assert.equal(wb.selected.value['001.txt'], undefined)
  assert.equal(wb.selectedName.value, null) // 选中的章节已不在列表 → 清除
})
