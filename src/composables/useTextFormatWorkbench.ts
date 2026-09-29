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
import { retryDurableTask } from '@/api/durableTasks'
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
import { chapterNumWidth, padChapterNum } from '@/utils/bookLabels'

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
  const loading = ref(false)

  // --- workbench UI state -------------------------------------------------
  const selectedKey = ref<string | null>(null)
  const filter = ref<'all' | 'pending' | 'adjusted'>('all')
  /** 按核对原因筛选（原因代码；'' = 全部）。 */
  const reasonFilter = ref('')
  /** 查看同号章节（原编号）：置位后表格只展示该组，便于集中比较。 */
  const sameOrigNum = ref<number | null>(null)
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
  const isMarked = (key: string | null) => !!key && (version.value?.review_marks ?? []).includes(key)
  const pendingCount = computed(() => chapters.value.filter((c) => c.pending && !isMarked(c.key)).length)
  const markedCount = computed(() => (version.value?.review_marks ?? []).length)

  /** The flow's config snapshot (TextToggles) differs from the persisted config. */
  const settingsDirty = computed(() => {
    const snapshot = (flow.value?.config_snapshot ?? null) as TextToggles | null
    const current = settings.config?.text
    if (!flow.value || !snapshot || !current) return false
    // adopt 老数据无快照（空对象）：没有比较基准，不判「已修改」。
    if (Object.keys(snapshot).length === 0) return false
    // live（自动重跑语义已移除）与 detect_chapters（并入分册方式、服务端恒开）
    // 不受用户开关控制，不参与「设置已修改」比较。
    const norm = (t: Record<string, unknown>) => JSON.stringify(Object.fromEntries(
      Object.keys(t).sort().filter((k) => k !== 'live' && k !== 'detect_chapters').map((k) => [k, t[k]]),
    ))
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
    if (sameOrigNum.value != null) {
      // 同号比较模式：整组展示，忽略状态/原因筛选（组内混有已核对/正常章也要可比）。
      list = list.filter((c) => c.orig_num === sameOrigNum.value)
    } else {
      if (filter.value === 'pending') list = list.filter((c) => c.pending && !isMarked(c.key))
      else if (filter.value === 'adjusted') list = list.filter((c) => c.adjusted)
      if (reasonFilter.value) list = list.filter((c) => (c.reasons ?? []).includes(reasonFilter.value))
    }
    const q = query.value.trim().toLowerCase()
    if (q) {
      // 同时匹配界面显示的补零章节号（如「001」）——只比原始 seq 时用户照界面输入搜不到。
      const pad = chapterNumWidth(chapters.value)
      list = list.filter(
        (c) =>
          (c.title ?? '').toLowerCase().includes(q) ||
          String(c.seq).includes(q) ||
          padChapterNum(c.numStr || c.seq, pad).includes(q),
      )
    }
    return list
  })
  /** 可筛选原因：本版本出现过的非「保留」原因，按首次出现顺序。 */
  const reasonOptions = computed(() => {
    const seen: string[] = []
    for (const c of chapters.value) {
      for (const r of c.reasons ?? []) if (r !== 'kept' && !seen.includes(r)) seen.push(r)
    }
    return seen
  })
  const pageCount = computed(() => Math.max(1, Math.ceil(filteredChapters.value.length / pageSize.value)))
  const pagedChapters = computed(() => {
    const p = Math.min(page.value, pageCount.value)
    return filteredChapters.value.slice((p - 1) * pageSize.value, p * pageSize.value)
  })
  watch([filter, query, reasonFilter, sameOrigNum], () => { page.value = 1 })

  const currentChapter = computed(() => chapters.value.find((c) => c.key === selectedKey.value) ?? null)
  /** 选中章节在同原编号组里的规模与位置（按正文顺序）；「重复」类原因的结论卡与
   * 「查看同号章节」共用。组小于 2 或无原编号时返回 null。 */
  const dupInfo = computed(() => {
    const ch = currentChapter.value
    if (!ch || ch.orig_num == null) return null
    const group = chapters.value.filter((c) => c.orig_num === ch.orig_num)
    if (group.length < 2) return null
    return { count: group.length, index: group.findIndex((c) => c.key === ch.key) + 1 }
  })
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
    /** 强制按字数分册（处理设置弹窗两选一中的「按字数分册」）。 */
    forceByLength?: boolean
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
        force_by_length: opts.forceByLength ?? false,
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
        const advanced = advanceAfterMark(chapterKey)
        const ch = chapters.value.find((c) => c.key === chapterKey)
        const numLabel = ch ? (ch.numStr || String(ch.seq)) : ''
        // 标记成功：给出可撤销的反馈（撤销 = 跳回该章并取消标记），减少来回点击。
        toast({
          title: `第${numLabel}章 已标记已核对`,
          variant: 'success',
          // 只有待核对过滤下才有「自动跳过」语义，其余过滤不提示。
          description: filter.value === 'pending'
            ? (advanced ? '已跳到下一个待核对章节' : '已是最后一个待核对章节')
            : undefined,
          duration: 6000,
          action: { label: '撤销', onClick: () => {
            selectedKey.value = chapterKey
            void toggleMark(chapterKey)
          } },
        })
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

  /** With the 待核对 filter active, jump to the next pending chapter after a
   *  mark. The just-marked chapter is already gone from the filtered list, so
   *  "next" is resolved in original (seq) order, not list position — marking
   *  the middle item must move forward, not jump to the first item. Returns
   *  whether the selection actually moved (drives the toast). */
  function advanceAfterMark(justMarkedKey: string): boolean {
    if (filter.value !== 'pending' || selectedKey.value !== justMarkedKey) return false
    const all = chapters.value
    const markedIdx = all.findIndex((c) => c.key === justMarkedKey)
    if (markedIdx < 0) return false
    const next = filteredChapters.value.find(
      (c) => all.findIndex((x) => x.key === c.key) > markedIdx,
    )
    if (!next) return false
    const position = filteredChapters.value.findIndex((c) => c.key === next.key)
    page.value = Math.floor(Math.max(0, position) / pageSize.value) + 1
    selectedKey.value = next.key
    return true
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
        preview.value = { key, status: 'error', text: '', error: '文件较大，无法在线预览' }
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

  function resetWorkbench() {
    loadToken += 1
    previewToken += 1
    previewAbort.abort()
    flow.value = null
    version.value = null
    nextTask.value = null
    activeTasks.value = []
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
    // 不短路旧 flow：keep-alive 切走时泵已中止，回来必须重新恢复（服务端读取
    // 幂等推进，不会重复处理）；首次激活与 onMounted 的恢复由 loadToken 去重。
    if (!project.activeProjectId) return
    void resume()
  })
  onDeactivated(() => previewAbort.abort())
  onUnmounted(() => previewAbort.abort())

  return {
    // state
    phase, flow, version, nextTask, activeTasks, loading,
    chapters, filteredChapters, pagedChapters, pageCount, page, pageSize,
    filter, query, reasonFilter, sameOrigNum, reasonOptions,
    selectedKey, marksBusy, preview,
    // derived
    pendingCount, markedCount, settingsDirty, canEnterParse, enterParseReason,
    isMarked, currentChapter, chapterMatters, chapterFile, dupInfo,
    // actions
    resume, startFlow, retryFailedStage, toggleMark, loadPreview,
    selectChapter: (key: string | null) => { selectedKey.value = key },
  }
}
