/** Script-parse workbench state (「文本解析」页，章节维度).
 *
 * Row truth = one `GET /script-parse/state` call (server-side aggregate over the
 * FULL task history — the recent-task window is not a chapter-state source) plus
 * the task store's live snapshots for in-flight progress. Task-execution state
 * and result availability are separate axes: a failed re-parse with a still-valid
 * older result reports both (失败 badge + 右栏仍展示旧结果并提示).
 *
 * Selection survives list refreshes within one mount; it is deliberately NOT
 * persisted (F5 clears it) — the copy says so, we don't promise more.
 */
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, reactive, ref, watch } from 'vue'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { ApiError } from '@/api/client'
import {
  cancelParseBatch,
  getScriptParseState, getScriptParseSummary, type ScriptParseSummary,
  getParseSelection,
  runScriptParse,
  scriptParseResultUrl,
  type ScriptParseFile,
  type ScriptParseState,
  type ScriptParseTaskRef,
  type ScriptParseInput,
} from '@/api/script'
import { previewUrl } from '@/api/textFormat'
import { previewUrl as legacyPreviewUrl } from '@/utils/fileops'
import type { ParseChecks, TaskSnapshot } from '@/types'
import { padChapterNum } from '@/utils/bookLabels'

export interface ParseEntry {
  speaker: string
  text: string
  instruct?: string
}

export interface ParseChapter {
  /** 分册文件名 = 选择键 / 任务 source_name。 */
  name: string
  /** 1 基序号（列表内位置）。 */
  seq: number
  numStr: string
  title: string
  chars: number
  input: ScriptParseInput | null
  latest_task: ScriptParseTaskRef | null
  result: ScriptParseFile['result']
  result_status: 'usable' | 'stale' | 'unverified' | null
}

export type RowStatus =
  | 'active'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'done'
  | 'stale'
  | 'unverified'
  | 'pending'

export type RowTone = 'primary' | 'amber' | 'red' | 'emerald' | 'muted'

/** 列表与详情共用的紧凑状态文字色。 */
export const TONE_TEXT: Record<RowTone, string> = {
  primary: 'text-primary',
  amber: 'text-amber-600 dark:text-amber-400',
  red: 'text-destructive',
  emerald: 'text-teal-600 dark:text-teal-400',
  muted: 'text-muted-foreground',
}

export interface ParseRow {
  chapter: ParseChapter
  status: RowStatus
  label: string
  tone: RowTone
  /** 在途任务（store 实时快照；仅 state 兜底时可能为 undefined）。 */
  task: TaskSnapshot | undefined
  taskId: string | null
  progress: number
  error: string
  retryable: boolean
}

export interface ResultPreview {
  key: string | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  entries: ParseEntry[]
  error: string
}

export interface SourcePreview {
  key: string | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  text: string
  error: string
}

/** 解析内 6 项 LLM 检查的 key（随提交固化进每个任务的配置快照；初值记忆项目配置）。
 *  展示名 / 分组 / 规则说明归 ParseChecksDialog（面向用户的文案与展示逻辑不留在组合函数里）。 */
type CheckKey = keyof ParseChecks
const CHECK_KEYS: CheckKey[] = [
  'check_chunk_alignment',
  'check_boundary_speakers',
  'validate_instructs',
  'revalidate_splits',
  'check_long_paragraphs',
  'spot_check_enabled',
]

const ACTIVE = new Set(['pending', 'queued', 'running', 'paused', 'cancelling', 'retrying'])

/** 历史文件名章节号/标题（001_标题.txt → 001 / 标题）；无编号前缀时退回序号。 */
function legacyNumTitle(name: string, seq: number): { numStr: string; title: string } {
  const stem = name.replace(/\.txt$/i, '')
  const m = stem.match(/^(\d+)[_-](.*)$/)
  if (m) return { numStr: m[1], title: m[2] || stem }
  return { numStr: String(seq), title: stem }
}

function initialChecks(settings: ReturnType<typeof useSettingsStore>): Record<CheckKey, boolean> {
  const base = {} as Record<CheckKey, boolean>
  for (const k of CHECK_KEYS) {
    const saved = settings.config?.generation?.[k]
    base[k] = saved === undefined ? true : saved
  }
  return base
}

export function useScriptParseWorkbench() {
  const project = useProjectStore()
  const settings = useSettingsStore()
  const taskStore = useTaskStore()
  const { push: toast } = useToast()

  // --- server-side state -----------------------------------------------------
  const state = ref<ScriptParseState | null>(null)
  const selectedRowCache = new Map<string, ParseRow>()
  const selectedInputs = reactive(new Map<string, ScriptParseInput | null>())
  const stateError = ref('')
  const summary = ref<ScriptParseSummary | null>(null)
  const summaryError = ref('')
  const loading = ref(false)
  const submitting = ref(false)

  // --- UI state --------------------------------------------------------------
  const selected = ref<Record<string, boolean>>({})
  const query = ref('')
  const filter = ref<'all' | 'pending' | 'done'>('all')
  const page = ref(1)
  const pageSize = ref(10)
  const selectedName = ref<string | null>(null)
  const tab = ref<'result' | 'source'>('result')
  /** 用户手动切过 tab 后，自动默认（空条目回落原文）不再干预。 */
  const tabTouched = ref(false)
  const checks = reactive<Record<CheckKey, boolean>>(initialChecks(settings))

  // 本会话提交映射（name → task_id）：state 的 latest_task 是刷新后口径，
  // 提交响应是本批即时口径——两者取活跃者，不依赖 label 文案。
  const submittedTasks = new Map<string, string>()
  // 历史磁盘文件提交时才登记摘要（响应 input_sha256）：state 刷新落地前
  // 提交请求以这里为准，state 落地后以文件表为准（两者一致）。
  const inputShaOverrides = new Map<string, string>()
  const resultCache = new Map<string, ParseEntry[]>()
  // 原文落地后保留在客户端（key = 预览 URL，内容口径绑定 flow_id——版本重处理换 URL 即失效）：
  // 切章节 / 切 tab 再切回命中缓存，不重新拉取、无 loading 闪烁。
  const sourceCache = new Map<string, string>()

  let loadToken = 0
  let selectionToken = 0
  let liveTimer: number | undefined
  let stateAbort = new AbortController()
  let resultToken = 0
  let resultAbort = new AbortController()
  let sourceToken = 0
  let sourceAbort = new AbortController()
  let firstActivation = true

  // --- chapter rows -----------------------------------------------------------
  const chapters = computed<ParseChapter[]>(() => {
    const st = state.value
    if (!st) return []
    const byName = new Map(st.files.map((f) => [f.name, f]))
    const merge = (name: string, seq: number, numStr: string, title: string, chars: number): ParseChapter => {
      const f: ScriptParseFile | undefined = byName.get(name)
      return {
        name, seq, numStr, title, chars,
        input: f?.input ?? null,
        latest_task: f?.latest_task ?? null,
        result: f?.result ?? null,
        result_status: f?.result_status ?? null,
      }
    }
    if (st.chapter_refs) return st.chapter_refs.map(c => merge(c.name, c.seq, c.numStr, c.title, c.chars))
    if (st.source.mode === 'version' && st.source.version) {
      const v = st.source.version
      // 分册文件按 seq 索引：第 N 章 = files[N-1]（与排版工作台同口径）。
      return v.chapters.map((c) => {
        const file = v.files[(c.seq ?? 1) - 1]
        if (!file) return null
        return merge(file.name, c.seq, c.numStr || String(c.seq), c.title, c.chars ?? 0)
      }).filter((c): c is ParseChapter => c !== null)
    }
    if (st.source.mode === 'legacy') {
      // input 统一取自 state.files（文件表行有 sha；仅磁盘文件为 null = 未登记）。
      const names = [...(st.source.legacy_files ?? []).map((f) => f.name), ...(st.source.disk_only ?? [])]
      return names.map((name, idx) => {
        const { numStr, title } = legacyNumTitle(name, idx + 1)
        return merge(name, idx + 1, numStr, title, 0)
      })
    }
    return []
  })

  // --- row status (two axes: execution + result availability) -----------------
  const rows = computed<ParseRow[]>(() => chapters.value.map((chapter) => {
    const taskRef = chapter.latest_task
    const submittedId = submittedTasks.get(chapter.name) ?? null
    const id = taskRef && ACTIVE.has(taskRef.status) ? taskRef.id : submittedId
    const snap = id ? taskStore.projectTasks.find((t) => t.id === id) : undefined
    const status = snap?.status ?? taskRef?.status ?? null
    const active = !!id && !!status && ACTIVE.has(status)

    let row: ParseRow
    if (active) {
      let label = '排队'
      if (status === 'running') {
        if (snap && (snap.phase === 'parse' || snap.phase === 'check')) label = '解析中'
        else if (snap && /排队/.test(snap.current || '')) label = '排队'
        else label = '解析中'
      } else if (status === 'paused') label = '等待 LLM 恢复'
      else if (status === 'cancelling') label = '取消中'
      row = {
        chapter, status: 'active', label,
        // 暂停（等待 LLM 恢复）= 需要关注的警示态（amber），与 ProjectOverview/Admin 口径一致；
        // 其余在途（排队/解析中/取消中）= 品牌色进行态。
        tone: status === 'paused' ? 'amber' : 'primary',
        task: snap, taskId: id, progress: snap?.progress ?? taskRef?.progress ?? 0,
        error: snap?.error ?? taskRef?.error ?? '', retryable: false,
      }
    } else if (taskRef && taskRef.status === 'failed') {
      row = { chapter, status: 'failed', label: '失败', tone: 'red', task: undefined, taskId: taskRef.id, progress: 1, error: taskRef.error, retryable: true }
    } else if (taskRef && taskRef.status === 'timeout') {
      row = { chapter, status: 'timeout', label: '超时失败', tone: 'red', task: undefined, taskId: taskRef.id, progress: 1, error: taskRef.error, retryable: true }
    } else if (taskRef && taskRef.status === 'cancelled') {
      row = { chapter, status: 'cancelled', label: '已取消', tone: 'muted', task: undefined, taskId: taskRef.id, progress: 0, error: taskRef.error, retryable: true }
    } else if (chapter.result_status === 'usable') {
      row = { chapter, status: 'done', label: '已完成', tone: 'emerald', task: undefined, taskId: null, progress: 1, error: '', retryable: false }
    } else if (chapter.result_status === 'stale') {
      row = { chapter, status: 'stale', label: '输入已变更', tone: 'amber', task: undefined, taskId: null, progress: 0, error: '', retryable: false }
    } else if (chapter.result_status === 'unverified') {
      row = { chapter, status: 'unverified', label: '状态待确认', tone: 'amber', task: undefined, taskId: null, progress: 0, error: '', retryable: false }
    } else {
      row = { chapter, status: 'pending', label: '待解析', tone: 'muted', task: undefined, taskId: null, progress: 0, error: '', retryable: false }
    }
    return row
  }))

  // --- stats / selection --------------------------------------------------------
  const total = computed(() => state.value?.pagination?.counts.all ?? rows.value.length)
  const controlsReady = computed(() => !state.value?.pagination || summary.value != null)
  const doneCount = computed(() => state.value?.pagination ? summary.value?.done_count ?? null : rows.value.filter((r) => r.status === 'done').length)
  const pendingCount = computed(() => doneCount.value == null ? null : Math.max(0, total.value - doneCount.value))
  const activeIds = computed(() => {
    const snapshots = taskStore.projectTasks
    const ids = new Set<string>([...(summary.value?.active_task_ids ?? []), ...rows.value.filter(r => r.status === 'active' && r.taskId).map(r => r.taskId!)])
    for (const task of snapshots) {
      if (task.module === 'script' && ACTIVE.has(task.status)) ids.add(task.id)
      else if (!ACTIVE.has(task.status)) ids.delete(task.id)
    }
    return ids
  })
  const busy = computed(() => activeIds.value.size > 0)

  function matchesSelectedInput(row: ParseRow) {
    return !selectedInputs.has(row.chapter.name) || row.chapter.input?.sha256 === selectedInputs.get(row.chapter.name)?.sha256
  }
  watch(rows, current => {
    for (const row of current) if (matchesSelectedInput(row)) selectedRowCache.set(row.chapter.name, row)
  }, { flush: 'sync' })
  const selectedRows = computed(() => Object.keys(selected.value).filter(name => selected.value[name]).map(name => {
    const current = rows.value.find(row => row.chapter.name === name)
    const row = current && matchesSelectedInput(current) ? current : selectedRowCache.get(name)
    if (!row) return undefined
    // Only the selected input reference is authoritative. Execution/result
    // state for that same input keeps following current rows and SSE.
    return selectedInputs.has(name) ? { ...row, chapter: { ...row.chapter, input: selectedInputs.get(name) ?? null } } : row
  }).filter((row): row is ParseRow => !!row))
  const selectedCount = computed(() => selectedRows.value.length)
  const selectedDoneCount = computed(() => selectedRows.value.filter((r) => r.status === 'done').length)
  const selectedStaleCount = computed(() => selectedRows.value.filter((r) => r.status === 'stale').length)

  function pruneSelection() {
    if (state.value?.pagination) {
      if (selectedName.value && !chapters.value.some(c => c.name === selectedName.value)) selectedName.value = null
      return
    }
    const names = new Set(chapters.value.map((c) => c.name))
    for (const k of Object.keys(selected.value)) if (!names.has(k)) delete selected.value[k]
    if (selectedName.value && !names.has(selectedName.value)) selectedName.value = null
  }

  /** 范围选择作用于全量章节清单（不随搜索/过滤），并排除已有活跃任务的章节
   *  （服务端 409 兜底）。pending = 非已完成（含失败/取消/输入已变更——都可重新解析）。 */
  function selectScope(scope: 'pending' | 'all' | 'done' | 'failed') {
    if (state.value?.pagination) { void selectRemote(scope); return }
    selected.value = {}
    for (const r of rows.value) {
      if (r.status === 'active') continue
      const pick =
        scope === 'all' ||
        (scope === 'pending' && r.status !== 'done') ||
        (scope === 'done' && r.status === 'done') ||
        (scope === 'failed' && (r.status === 'failed' || r.status === 'timeout'))
      if (pick) selected.value[r.chapter.name] = true
    }
  }

  function selectFiltered() {
    if (!project.activeProjectId || busy.value || loading.value || stateError.value || !controlsReady.value) return
    if (state.value?.pagination) { void selectRemote(filter.value, query.value); return }
    selected.value = {}
    for (const row of filteredRows.value) {
      if (row.status !== 'active') selected.value[row.chapter.name] = true
    }
  }

  async function selectRemote(scope: string, q = '') {
    const pid = project.activeProjectId
    const token = loadToken
    const selectionGeneration = ++selectionToken
    try {
      const response = await getParseSelection(pid, scope, q)
      if (token !== loadToken || pid !== project.activeProjectId || selectionGeneration !== selectionToken) return
      selected.value = {}
      selectedInputs.clear()
      for (const item of response.items) {
        selected.value[item.name] = true
        selectedInputs.set(item.name, item.input)
        inputShaOverrides.delete(item.name)
        const previous = selectedRowCache.get(item.name)
        selectedRowCache.set(item.name, {
          chapter: { ...(previous?.chapter ?? { seq: 0, numStr: '', title: item.name, chars: 0 }), name: item.name, input: item.input, latest_task: null, result: null, result_status: item.result_status ?? null },
          status: item.status === 'failed' ? 'failed' : item.status === 'timeout' ? 'timeout' : item.status === 'cancelled' ? 'cancelled' : item.result_status === 'usable' ? 'done' : item.result_status === 'stale' ? 'stale' : 'pending', label: '待解析', tone: 'muted', task: undefined, taskId: null, progress: 0, error: '', retryable: false,
        })
      }
    } catch (cause: any) {
      if (token === loadToken && pid === project.activeProjectId && selectionGeneration === selectionToken) stateError.value = cause?.message || '选择范围读取失败'
    }
  }

  function clearSelection() {
    selectionToken += 1
    selectedInputs.clear()
    selected.value = {}
  }

  function toggleSelect(name: string) {
    selectionToken += 1
    selectedInputs.delete(name)
    inputShaOverrides.delete(name)
    if (selected.value[name]) delete selected.value[name]
    else selected.value[name] = true
  }

  // --- list filtering / pagination (in-memory, hundreds of chapters) ------------
  const filteredRows = computed(() => {
    if (state.value?.pagination) return rows.value
    let list = rows.value
    if (filter.value === 'pending') list = list.filter((r) => r.status !== 'done')
    else if (filter.value === 'done') list = list.filter((r) => r.status === 'done')
    const q = query.value.trim().toLowerCase()
    if (q) {
      list = list.filter(
        (r) =>
          (r.chapter.title ?? '').toLowerCase().includes(q) ||
          String(r.chapter.seq).includes(q) ||
          r.chapter.numStr.toLowerCase().includes(q) ||
          r.chapter.name.toLowerCase().includes(q),
      )
    }
    return list
  })
  const pageCount = computed(() => Math.max(1, Math.ceil((state.value?.pagination?.total ?? 0) / pageSize.value)))
  // 页由服务端切好并随 page/page_size 请求返回；前端不再对全量数据做分页切片。
  const pagedRows = computed(() => filteredRows.value)
  watch([filter, query], () => { page.value = 1 })
  watch([page, pageSize, query, filter], () => { void refreshState() })

  const currentRow = computed<ParseRow | null>(() =>
    rows.value.find((r) => r.chapter.name === selectedName.value) ?? null,
  )
  const chapterNumPad = computed(() => {
    const max = chapters.value.reduce((m, c) => Math.max(m, c.numStr.length, String(c.seq).length), 1)
    return max
  })
  function chapterLabel(row: ParseRow): string {
    return padChapterNum(row.chapter.numStr, chapterNumPad.value)
  }

  function selectChapter(name: string | null) {
    selectedName.value = name
    tabTouched.value = false
    if (!name) return
    // 默认进「解析结果」；无结果、或结果无条目（待解析/失败无旧结果/空解析）→ 看原文。
    // 已加载过（缓存命中）时按条目数同步裁决；未加载时暂留结果页，loadResult 落地后空条目回落。
    const row = currentRow.value
    if (!row || !row.chapter.result) {
      tab.value = 'source'
      return
    }
    const rk = resultKey(row)
    const cached = rk ? resultCache.get(rk) : undefined
    tab.value = cached ? (cached.length > 0 ? 'result' : 'source') : 'result'
  }
  /** 用户手动切 tab：记录后，「空条目自动回落原文」不再覆盖用户选择。 */
  function setTab(t: 'result' | 'source') {
    tabTouched.value = true
    tab.value = t
  }

  // --- previews (cache key = project + file_id + content sha — 同大小不同内容必失效)
  const resultPreview = ref<ResultPreview>({ key: null, status: 'idle', entries: [], error: '' })
  const sourcePreview = ref<SourcePreview>({ key: null, status: 'idle', text: '', error: '' })

  function resultKey(row: ParseRow): string | null {
    const r = row.chapter.result
    if (!r?.file_id) return null
    const pid = project.activeProjectId
    if (!pid) return null
    return `${pid}:${r.file_id}:${r.sha256 ?? r.task_id}`
  }

  /** 结果落地：空条目且用户未手动切过 tab → 回落原文页（空的结果页没有信息量）。 */
  function applyReadyPreview(key: string | null, entries: ParseEntry[]) {
    resultPreview.value = { key, status: 'ready', entries, error: '' }
    if (entries.length === 0 && !tabTouched.value && tab.value === 'result') tab.value = 'source'
  }

  async function loadResult() {
    const row = currentRow.value
    const token = ++resultToken
    resultAbort.abort()
    if (!row || !row.chapter.result?.file_id) {
      resultPreview.value = { key: null, status: 'idle', entries: [], error: '' }
      return
    }
    const key = resultKey(row)
    const pid = project.activeProjectId
    if (!key || !pid) {
      resultPreview.value = { key: null, status: 'error', entries: [], error: '解析结果不可用' }
      return
    }
    const cached = resultCache.get(key)
    if (cached) {
      applyReadyPreview(key, cached)
      return
    }
    resultPreview.value = { key, status: 'loading', entries: [], error: '' }
    resultAbort = new AbortController()
    try {
      const res = await fetch(scriptParseResultUrl(pid, row.chapter.result.file_id), {
        signal: resultAbort.signal,
        credentials: 'include',
      })
      if (token !== resultToken || selectedName.value !== row.chapter.name) return
      if (res.status === 409) {
        resultPreview.value = { key, status: 'error', entries: [], error: '解析结果内容已丢失，请刷新后重试' }
        return
      }
      if (res.status === 404) {
        resultPreview.value = { key, status: 'error', entries: [], error: '解析结果不存在，请刷新后重试' }
        return
      }
      if (!res.ok) throw new Error('解析结果加载失败')
      const data = await res.json()
      if (token !== resultToken || selectedName.value !== row.chapter.name) return
      const entries: ParseEntry[] = Array.isArray(data)
        ? data.filter((e): e is ParseEntry => !!e && typeof e === 'object' && typeof e.text === 'string')
        : []
      resultCache.set(key, entries)
      applyReadyPreview(key, entries)
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      if (token === resultToken && selectedName.value === row.chapter.name) {
        resultPreview.value = { key, status: 'error', entries: [], error: e?.message || '解析结果加载失败' }
      }
    }
  }

  function sourceUrl(row: ParseRow): string | null {
    const st = state.value
    if (!st) return null
    if (st.source.mode === 'version' && st.source.version) {
      if (st.source.version.version_status !== 'current') return null
      return previewUrl(project.activeProjectId, st.source.version.flow_id, row.chapter.name)
    }
    if (st.source.mode === 'legacy') return legacyPreviewUrl('02_split_text', row.chapter.name)
    return null
  }

  async function loadSource() {
    const row = currentRow.value
    const token = ++sourceToken
    sourceAbort.abort()
    if (!row) {
      sourcePreview.value = { key: null, status: 'idle', text: '', error: '' }
      return
    }
    const st = state.value
    if (st?.source.mode === 'version' && st.source.version?.version_status !== 'current') {
      sourcePreview.value = { key: null, status: 'error', text: '', error: '该版本内容已被后续处理覆盖，原文预览不可用' }
      return
    }
    const url = sourceUrl(row)
    const key = url ?? null
    if (!url) {
      sourcePreview.value = { key: null, status: 'error', text: '', error: '没有可预览的原文' }
      return
    }
    const cached = sourceCache.get(url)
    if (cached !== undefined) {
      sourcePreview.value = { key, status: 'ready', text: cached, error: '' }
      return
    }
    sourcePreview.value = { key, status: 'loading', text: '', error: '' }
    sourceAbort = new AbortController()
    try {
      const res = await fetch(url, { signal: sourceAbort.signal, credentials: 'include' })
      if (token !== sourceToken || selectedName.value !== row.chapter.name) return
      if (res.status === 413) {
        sourcePreview.value = { key, status: 'error', text: '', error: '文件较大，无法在线预览' }
        return
      }
      if (res.status === 409) {
        // 内容已被新版本替换：缓存的旧文本作废，不再回填。
        sourceCache.delete(url)
        sourcePreview.value = { key, status: 'error', text: '', error: '内容已被新版本替换，请刷新后重试' }
        return
      }
      if (!res.ok) throw new Error('原文加载失败')
      const text = await res.text()
      if (token !== sourceToken || selectedName.value !== row.chapter.name) return
      sourceCache.set(url, text)
      sourcePreview.value = { key, status: 'ready', text, error: '' }
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      if (token === sourceToken && selectedName.value === row.chapter.name) {
        sourcePreview.value = { key, status: 'error', text: '', error: e?.message || '原文加载失败' }
      }
    }
  }

  watch(selectedName, () => {
    void loadResult()
    void loadSource()
  })
  watch(tab, () => {
    if (tab.value === 'result') void loadResult()
    else void loadSource()
  })
  // 状态刷新后选中章节的结果摘要变化（重新解析完成）→ 缓存键变化即重新拉取。
  watch(
    () => (currentRow.value ? resultKey(currentRow.value) : null),
    () => {
      if (tab.value === 'result') void loadResult()
    },
  )

  // --- state loading -------------------------------------------------------------
  function applyState(next: ScriptParseState) {
    state.value = next
    stateError.value = ''
    pruneSelection()
  }

  async function refreshState(): Promise<boolean> {
    const pid = project.activeProjectId
    if (!pid) {
      state.value = null
      summary.value = null
      summaryError.value = ''
      selectedRowCache.clear()
      selectedInputs.clear()
      return false
    }
    const token = ++loadToken
    loading.value = true
    stateAbort.abort()
    stateAbort = new AbortController()
    try {
      const next = await getScriptParseState(pid, stateAbort.signal, { page: page.value, page_size: pageSize.value, q: query.value, filter: filter.value })
      if (token !== loadToken || project.activeProjectId !== pid) return false
      applyState(next)
      if (next.pagination) {
        const signal = stateAbort.signal
        void getScriptParseSummary(pid, signal).then(value => {
          if (!signal.aborted && token === loadToken && project.activeProjectId === pid) { summary.value = value; summaryError.value = '' }
        }).catch((error: any) => {
          if (!signal.aborted && token === loadToken && project.activeProjectId === pid) summaryError.value = error?.message || '全书状态暂未更新'
        })
      }
      if (page.value > pageCount.value) page.value = pageCount.value
      return true
    } catch (e: any) {
      // 「状态待确认」：保留上次已知行，不当成正常完成。
      if (token === loadToken && project.activeProjectId === pid && e?.name !== 'AbortError') {
        stateError.value = e?.message || '状态请求失败，当前显示的是上次已知状态'
      }
      return false
    } finally {
      if (token === loadToken) loading.value = false
    }
  }

  /** 在途任务离开活跃态（成功/失败/取消/超时）→ 合并在 600ms 内整表刷新：
   *  单章先完成立即刷新该章状态/预览，不等整批（review #4）。 */
  watch(activeIds, (now, prev) => {
    if (!prev) return
    const dropped = [...prev].filter((id) => id != null && !now.has(id))
    if (!dropped.length) return
    window.clearTimeout(liveTimer)
    liveTimer = window.setTimeout(() => void refreshState(), 600)
  })

  // --- submission ------------------------------------------------------------------
  const canSubmit = computed(() => {
    const st = state.value
    const staleBlocked = st?.source.mode === 'version' && st?.source.version?.version_status === 'stale'
    return controlsReady.value && !!project.activeProjectId && !!st && !st.text_format_busy && !staleBlocked
  })

  async function startParse(): Promise<boolean> {
    if (!controlsReady.value || busy.value || submitting.value || !selectedCount.value) return false
    if (!project.activeProjectId) return false
    if (state.value?.text_format_busy) {
      toast({ title: '排版与分册任务正在进行，暂不能提交解析', variant: 'destructive' })
      return false
    }
    if (state.value?.source.mode === 'version' && state.value.source.version?.version_status === 'stale') {
      toast({ title: '该版本分册文本已被后续处理覆盖，请先到「排版与分册」刷新', variant: 'destructive' })
      return false
    }
    if (!(settings.config?.llm?.model_name || '').trim()) {
      toast({ title: '请先填写 LLM 模型名称（模型不能为空）', variant: 'destructive' })
      return false
    }
    const files = selectedRows.value.map((r) => ({
      name: r.chapter.name,
      sha256: inputShaOverrides.get(r.chapter.name) ?? r.chapter.input?.sha256 ?? null,
    }))
    submitting.value = true
    try {
      const r = await runScriptParse(project.activeProjectId, files, { ...checks })
      for (const f of r.files) {
        submittedTasks.set(f.name, f.task_id)
        if (f.input_sha256) inputShaOverrides.set(f.name, f.input_sha256)
      }
      await taskStore.refresh()
      // 记忆本次勾选进项目配置（下次页面初值）；fire-and-forget，失败不阻断。
      void settings.save({ generation: { ...checks } })
      toast({ title: `已开始解析 ${r.files.length} 章`, variant: 'success' })
      void refreshState()
      return true
    } catch (e: any) {
      toast({
        title: '无法提交解析',
        variant: 'destructive',
        description: e?.message || '请稍后重试',
      })
      // 版本失配（409：文件已变更 / 分册发布中 / 任务在途）→ 立即重核，
      // 让用户基于最新状态重新选择，而不是对着过期勾选硬点。
      if (e instanceof ApiError && e.status === 409) void refreshState()
      return false
    } finally {
      submitting.value = false
    }
  }

  /** 【取消全部】：一次取消所有在途解析任务（端点失败回退逐任务取消）。 */
  async function cancelAll() {
    const ids = [...activeIds.value]
    if (!ids.length) return
    try {
      await cancelParseBatch(ids)
    } catch {
      for (const id of ids) void taskStore.control(id, 'cancel')
    }
    await taskStore.refresh()
    void refreshState()
  }

  /** 重试 = 后端对同一 task 记录 requeue（status 回 pending，返回同一 id）。
   *  登记进本会话提交映射后，行实时状态改由 task store 快照驱动（SSE 进度/阶段），
   *  不必等 state 整表刷新——state 的 latest_task 在刷新前还是终态口径，
   *  不登记的话该行会一直显示「失败」。失败给 toast（此前是静默 fire-and-forget）。 */
  async function retryRow(row: ParseRow) {
    if (!row.taskId || !row.retryable) return
    try {
      await taskStore.control(row.taskId, 'retry')
    } catch (e: any) {
      toast({
        title: '无法重试该章节',
        variant: 'destructive',
        description: e?.message || '请稍后重试',
      })
      return
    }
    submittedTasks.set(row.chapter.name, row.taskId)
    void refreshState()
  }

  function cancelRow(row: ParseRow) {
    if (row.taskId) void taskStore.control(row.taskId, 'cancel')
  }

  // --- lifecycle (keep-alive aware) -------------------------------------------------
  function resetWorkbench() {
    loadToken += 1
    stateAbort.abort()
    resultToken += 1
    resultAbort.abort()
    sourceToken += 1
    sourceAbort.abort()
    state.value = null
    summary.value = null
    summaryError.value = ''
    selectedRowCache.clear()
    selectedInputs.clear()
    stateError.value = ''
    selected.value = {}
    selectedName.value = null
    submittedTasks.clear()
    inputShaOverrides.clear()
    resultCache.clear()
    sourceCache.clear()
    resultPreview.value = { key: null, status: 'idle', entries: [], error: '' }
    sourcePreview.value = { key: null, status: 'idle', text: '', error: '' }
    page.value = 1
    query.value = ''
    filter.value = 'all'
  }

  watch(
    () => project.activeProjectId,
    (id, prev) => {
      if (id === prev) return
      if (!id) {
        resetWorkbench()
        return
      }
      resetWorkbench()
      void refreshState()
      void taskStore.refresh()
    },
  )

  onMounted(() => {
    void (async () => {
      if (!settings.loaded) await settings.load()
      if (project.activeProjectId) {
        await taskStore.refresh()
        await refreshState()
      }
    })()
  })
  onActivated(() => {
    if (firstActivation) {
      firstActivation = false
      return
    }
    if (project.activeProjectId) void refreshState()
  })
  onDeactivated(() => {
    resultAbort.abort()
    sourceAbort.abort()
    stateAbort.abort()
  })
  onBeforeUnmount(() => {
    resultAbort.abort()
    sourceAbort.abort()
    stateAbort.abort()
    window.clearTimeout(liveTimer)
  })

  return {
    // state
    state, stateError, summaryError, controlsReady, loading, submitting,
    rows, chapters, filteredRows, pagedRows, pageCount, page, pageSize,
    filteredTotal: computed(() => state.value?.pagination?.total ?? filteredRows.value.length),
    filter, query, chapterNumPad, chapterLabel,
    // stats / selection
    total, doneCount, pendingCount, busy,
    selectedCount, selectedDoneCount, selectedStaleCount,
    selected,
    selectScope, selectFiltered, clearSelection, toggleSelect,
    // preview
    selectedName, selectChapter, currentRow, tab, setTab,
    resultPreview, sourcePreview, loadResult, loadSource,
    // submission
    canSubmit, startParse, cancelAll, retryRow, cancelRow,
    // checks
    checks,
    refreshState,
  } as const
}
