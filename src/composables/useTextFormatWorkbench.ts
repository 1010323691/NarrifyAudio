/** Chapter-review workbench state.
 *
 * All pipeline state lives server-side (TextFormatFlow + task records); this
 * composable implements the recovery protocol: read state, track the reported
 * next task, and on each terminal state ask the server to evaluate and
 * idempotently submit the next stage — the browser never submits stage tasks
 * itself, so recovery (refresh / return / hand-off gap) cannot duplicate work.
 */
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { ApiError } from '@/api/client'
import { listProjectDurableTasks, retryDurableTask, type DurableTask } from '@/api/durableTasks'
import {
  deleteReviewMark,
  getWorkbenchState,
  postReviewMark,
  postWorkbenchFlow,
  previewUrl,
  workbenchErrorMessage,
  type WorkbenchChapter,
  type WorkbenchFlow,
  type WorkbenchMatter,
  type WorkbenchNextTask,
  type WorkbenchState,
  type WorkbenchVersion,
} from '@/api/textFormat'
import type { TextToggles } from '@/types'

const FLOW_TASK_TYPES = new Set(['text.format', 'book.analyze', 'book.split'])

export interface PreviewState {
  key: string | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  text: string
  error: string
}

export type WorkbenchPhase = 'empty' | 'processing' | 'ready' | 'failed'

export function useTextFormatWorkbench() {
  const project = useProjectStore()
  const settings = useSettingsStore()
  const { push: toast } = useToast()
  const waitForTask = useDurableTaskWait()

  // --- server-side state -------------------------------------------------
  const flow = ref<WorkbenchFlow | null>(null)
  const version = ref<WorkbenchVersion | null>(null)
  const nextTask = ref<WorkbenchNextTask | null>(null)
  const activeTasks = ref<WorkbenchState['active_tasks']>([])
  const records = ref<DurableTask[]>([])
  const loading = ref(false)

  // --- workbench UI state -------------------------------------------------
  const selectedKey = ref<string | null>(null)
  const tab = ref<'chapters' | 'preview' | 'records'>('chapters')
  const filter = ref<'all' | 'pending' | 'adjusted'>('all')
  const query = ref('')
  const page = ref(1)
  const pageSize = ref(20)
  const marksBusy = ref<string | null>(null)
  const preview = ref<PreviewState>({ key: null, status: 'idle', text: '', error: '' })

  let previewToken = 0
  let previewAbort = new AbortController()
  let loadToken = 0
  let pumpRunning = false

  // --- derived ------------------------------------------------------------
  const phase: Ref<WorkbenchPhase> = computed(() => {
    if (!flow.value) return 'empty'
    if (flow.value.status === 'failed') return 'failed'
    if (flow.value.status === 'ready' && version.value) return 'ready'
    return 'processing'
  })

  const chapters = computed<WorkbenchChapter[]>(() => version.value?.chapters ?? [])
  const matterById = computed<Map<string, WorkbenchMatter>>(
    () => new Map((version.value?.matters ?? []).map((m) => [m.id, m])),
  )
  const versionMatters = computed<WorkbenchMatter[]>(
    () => (version.value?.matters ?? []).filter((m) => m.scope === 'version'),
  )
  const isMarked = (key: string | null) => !!key && (version.value?.review_marks ?? []).includes(key)
  const pendingCount = computed(() => chapters.value.filter((c) => c.pending && !isMarked(c.key)).length)
  const markedCount = computed(() => (version.value?.review_marks ?? []).length)

  /** The flow's config snapshot (TextToggles) differs from the persisted config. */
  const settingsDirty = computed(() => {
    const snapshot = (flow.value?.config_snapshot ?? null) as TextToggles | null
    const current = settings.config?.text
    if (!flow.value || !snapshot || !current) return false
    const norm = (t: Record<string, unknown>) => JSON.stringify(Object.fromEntries(Object.keys(t).sort().map((k) => [k, t[k]])))
    return norm(snapshot as unknown as Record<string, unknown>) !== norm(current as unknown as Record<string, unknown>)
  })

  const canEnterParse = computed(() =>
    phase.value === 'ready' && version.value?.version_status === 'current' && activeTasks.value.length === 0,
  )
  const enterParseReason = computed(() => {
    if (phase.value === 'processing') return '排版分册处理中'
    if (phase.value === 'failed') return '流程失败，请先重试'
    if (phase.value === 'empty') return '尚未处理'
    if (version.value?.version_status === 'stale') return '该版本内容已被后续处理覆盖'
    return '请先完成排版与分册'
  })

  // --- table filtering / pagination (in-memory, hundreds of chapters) -----
  const filteredChapters = computed(() => {
    let list = chapters.value
    if (filter.value === 'pending') list = list.filter((c) => c.pending && !isMarked(c.key))
    else if (filter.value === 'adjusted') list = list.filter((c) => c.adjusted)
    const q = query.value.trim().toLowerCase()
    if (q) list = list.filter((c) => (c.title ?? '').toLowerCase().includes(q) || String(c.seq).includes(q))
    return list
  })
  const pageCount = computed(() => Math.max(1, Math.ceil(filteredChapters.value.length / pageSize.value)))
  const pagedChapters = computed(() => {
    const p = Math.min(page.value, pageCount.value)
    return filteredChapters.value.slice((p - 1) * pageSize.value, p * pageSize.value)
  })
  watch([filter, query], () => { page.value = 1 })

  const currentChapter = computed(() => chapters.value.find((c) => c.key === selectedKey.value) ?? null)
  function chapterMatters(chapter: WorkbenchChapter): WorkbenchMatter[] {
    return (chapter.matters ?? []).map((id) => matterById.value.get(id)).filter((m): m is WorkbenchMatter => !!m)
  }

  /** The output file of a chapter (files is seq-indexed: chapter N → file N). */
  function chapterFile(chapter: WorkbenchChapter): { name: string; chars: number } | null {
    return version.value?.files[chapter.seq - 1] ?? null
  }

  // --- state loading / recovery pump ---------------------------------------
  function applyState(state: WorkbenchState) {
    const previousFlowId = version.value?.flow_id
    flow.value = state.flow
    version.value = state.version
    nextTask.value = state.next_task
    activeTasks.value = state.active_tasks
    const keys = new Set(chapters.value.map((c) => c.key).filter(Boolean))
    if (selectedKey.value && !keys.has(selectedKey.value)) selectedKey.value = null
    // New generation: chapter keys can repeat across versions — reload the
    // preview for a still-valid selection instead of showing old text.
    if (previousFlowId && state.version && previousFlowId !== state.version.flow_id) {
      const still = state.version.chapters.find((c) => c.key === selectedKey.value)
      if (still) void loadPreview(still)
    }
  }

  async function refreshState(): Promise<boolean> {
    const projectId = project.activeProjectId
    if (!projectId) return false
    const token = ++loadToken
    try {
      const state = await getWorkbenchState(projectId)
      if (token !== loadToken || project.activeProjectId !== projectId) return false
      applyState(state)
      return true
    } catch {
      return false
    }
  }

  /** Ask the server to evaluate the flow and idempotently submit the next
   *  missing stage. Never re-submits an existing stage (stable tflow keys). */
  async function continueFlow(): Promise<WorkbenchState | null> {
    const projectId = project.activeProjectId
    if (!projectId) return null
    try {
      const state = await postWorkbenchFlow(projectId, {})
      if (project.activeProjectId !== projectId) return null
      applyState(state)
      return state
    } catch {
      return null
    }
  }

  /** Track the reported next task to terminal, then re-evaluate — repeat
   *  until the flow is ready / failed / blocked. Guarded: only one pump. */
  async function pump() {
    if (pumpRunning || !project.activeProjectId) return
    if (!nextTask.value || nextTask.value.failed) return
    pumpRunning = true
    const projectId = project.activeProjectId
    try {
      while (nextTask.value && !nextTask.value.failed) {
        const taskId = nextTask.value.task_id
        try {
          await waitForTask.wait(taskId)
        } catch (e: any) {
          if (e?.name === 'AbortError') return // deactivated / project switch — resume later
          throw e
        }
        if (project.activeProjectId !== projectId) return
        const state = await continueFlow()
        if (!state) return
      }
    } finally {
      pumpRunning = false
    }
  }

  /** Recovery entry point (mount / reactivate / manual refresh). */
  async function resume() {
    loading.value = true
    try {
      if (!settings.loaded) await settings.load()
      if (!(await refreshState())) return
      await pump()
    } finally {
      loading.value = false
    }
  }

  // --- user actions ---------------------------------------------------------
  /** Start or restart the pipeline for a source file (explicit user action). */
  async function startFlow(opts: {
    sourceFile: { file_id: string; name: string }
    config: TextToggles
    wholeBook?: boolean
    restart?: boolean
  }): Promise<boolean> {
    const projectId = project.activeProjectId
    if (!projectId || !opts.sourceFile?.file_id) {
      toast({ title: '请先选择要处理的 TXT 文件', variant: 'destructive' })
      return false
    }
    loading.value = true
    try {
      const state = await postWorkbenchFlow(projectId, {
        source_file_id: opts.sourceFile.file_id,
        config: { ...opts.config },
        whole_book: opts.wholeBook ?? false,
        restart: opts.restart ?? false,
      })
      applyState(state)
      await pump()
      return true
    } catch (e: any) {
      toast({
        title: e instanceof ApiError ? '无法开始处理' : '请求失败',
        variant: 'destructive',
        description: e instanceof ApiError ? workbenchErrorMessage(e.status, { detail: e.message }) : e?.message || '网络异常，请重试',
      })
      return false
    } finally {
      loading.value = false
    }
  }

  /** Retry the failed stage (existing task retry endpoint), then re-evaluate. */
  async function retryFailedStage(): Promise<void> {
    const failed = nextTask.value
    if (!failed || !failed.failed) return
    loading.value = true
    try {
      await retryDurableTask(failed.task_id)
      const state = await continueFlow()
      if (state) await pump()
    } catch (e: any) {
      toast({ title: '重试失败', variant: 'destructive', description: e?.message || '网络异常，请重试' })
    } finally {
      loading.value = false
    }
  }

  // --- review marks ----------------------------------------------------------
  async function toggleMark(chapterKey: string | null): Promise<boolean> {
    const v = version.value
    if (!v || !chapterKey || marksBusy.value) return false
    const projectId = project.activeProjectId
    marksBusy.value = chapterKey
    try {
      const marked = isMarked(chapterKey)
      if (marked) {
        await deleteReviewMark(projectId, v.task_id, chapterKey)
        v.review_marks = v.review_marks.filter((k) => k !== chapterKey)
      } else {
        await postReviewMark(projectId, v.task_id, chapterKey)
        v.review_marks = [...v.review_marks, chapterKey]
        advanceAfterMark()
      }
      return true
    } catch (e: any) {
      toast({
        title: '核对标记失败',
        variant: 'destructive',
        description: e instanceof ApiError ? workbenchErrorMessage(e.status, { detail: e.message }) : e?.message || '网络异常，请重试',
      })
      return false
    } finally {
      marksBusy.value = null
    }
  }

  /** With the 待核对 filter active, jump to the next pending chapter after a mark. */
  function advanceAfterMark() {
    if (filter.value !== 'pending' || !selectedKey.value) return
    const list = filteredChapters.value
    const idx = list.findIndex((c) => c.key === selectedKey.value)
    const next = list[idx + 1]
    if (next) {
      const position = filteredChapters.value.findIndex((c) => c.key === next.key)
      page.value = Math.floor(Math.max(0, position) / pageSize.value) + 1
      selectedKey.value = next.key
    }
  }

  // --- versioned preview (stale-response guard + lifecycle abort) -----------
  async function loadPreview(chapter: WorkbenchChapter | null) {
    previewAbort.abort()
    const token = ++previewToken
    const key = chapter?.key ?? null
    if (!chapter) {
      preview.value = { key: null, status: 'idle', text: '', error: '' }
      return
    }
    const v = version.value
    const file = chapterFile(chapter)
    const projectId = project.activeProjectId
    if (!v || !file || !projectId || v.version_status !== 'current') {
      preview.value = { key, status: 'error', text: '', error: v ? '该版本内容已被后续处理覆盖，预览不可用' : '没有可预览的版本' }
      return
    }
    preview.value = { key, status: 'loading', text: '', error: '' }
    previewAbort = new AbortController()
    try {
      const res = await fetch(previewUrl(projectId, v.flow_id, file.name), {
        signal: previewAbort.signal,
        credentials: 'include',
      })
      if (token !== previewToken || selectedKey.value !== key) return
      if (res.status === 413) {
        preview.value = { key, status: 'error', text: '', error: '文件较大，请直接下载查看' }
        return
      }
      if (res.status === 409) {
        preview.value = { key, status: 'error', text: '', error: '内容已被新版本替换，请刷新后重试' }
        return
      }
      if (!res.ok) throw new Error('预览加载失败')
      const text = await res.text()
      if (token !== previewToken || selectedKey.value !== key) return
      preview.value = { key, status: 'ready', text, error: '' }
    } catch (e: any) {
      if (e?.name === 'AbortError') return
      if (token === previewToken && selectedKey.value === key) {
        preview.value = { key, status: 'error', text: '', error: e?.message || '预览加载失败' }
      }
    }
  }

  watch(selectedKey, (key) => {
    void loadPreview(chapters.value.find((c) => c.key === key) ?? null)
  })

  // --- 处理记录 tab ----------------------------------------------------------
  async function loadRecords(): Promise<void> {
    const projectId = project.activeProjectId
    if (!projectId) return
    try {
      const all = await listProjectDurableTasks(projectId)
      records.value = all.filter((t) => FLOW_TASK_TYPES.has(t.task_type))
    } catch {
      // Records are supplementary; state is the source of truth.
    }
  }

  function resetWorkbench() {
    loadToken += 1
    previewToken += 1
    previewAbort.abort()
    flow.value = null
    version.value = null
    nextTask.value = null
    activeTasks.value = []
    records.value = []
    selectedKey.value = null
    preview.value = { key: null, status: 'idle', text: '', error: '' }
    page.value = 1
  }

  // --- lifecycle (keep-alive aware) ------------------------------------------
  watch(
    () => project.activeProjectId,
    (id, prev) => {
      if (id === prev) return
      if (!id) {
        resetWorkbench()
        return
      }
      void resume()
    },
  )
  onMounted(() => {
    if (project.activeProjectId) void resume()
  })
  onActivated(() => {
    if (flow.value || !project.activeProjectId) return
    if (project.activeProjectId) void resume()
  })
  onDeactivated(() => previewAbort.abort())
  onUnmounted(() => previewAbort.abort())

  return {
    // state
    phase, flow, version, nextTask, activeTasks, records, loading,
    chapters, filteredChapters, pagedChapters, pageCount, page, pageSize,
    tab, filter, query, selectedKey, marksBusy, preview,
    // derived
    pendingCount, markedCount, settingsDirty, canEnterParse, enterParseReason,
    isMarked, currentChapter, chapterMatters, chapterFile, versionMatters,
    // actions
    resume, startFlow, retryFailedStage, toggleMark, loadPreview, loadRecords,
    selectChapter: (key: string | null) => { selectedKey.value = key },
  }
}
