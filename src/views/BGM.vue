<script setup lang="ts">
// 背景音乐（阶段 7）：段落分析（LLM 缓存）→ 匹配（随机 / 段落级时间轴）→ 最终混音。
// 行 / 派生 / SSE / F5 全复刻 Merge.vue 范本：任务按 label 尾部「：{stem}」派生式重挂
// （无本地 job 列表、不依赖后端注册表），终态经 getter 式 watch 驱动行刷新。
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useWorkbenchScope, withinScope } from '@/composables/useWorkbenchScope'
import { useWorkbenchDialog } from '@/composables/useWorkbenchDialog'
import { onDeactivated, onBeforeUnmount } from 'vue'
import { useWorkbenchTaskControl } from '@/composables/useWorkbenchTaskControl'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import {
  analyzeSegmentChapters,
  bgmPreviewUrl,
  getChapters,
  getTimeline,
  matchChapters,
  mixChapters,
  packageMixedAudio,
  updateChapter,
} from '@/api/bgm'
import { downloadUrl, downloadFile } from '@/utils/fileops'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import { listDir } from '@/api/files'
import { getLibrary, musicPreviewUrl } from '@/api/music'
import type {
  BgmChapterRow,
  BgmTimeline,
  MusicLibrary,
  MusicTagCategory,
  TaskSnapshot,
} from '@/types'

import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import ProductionWorkbench from '@/components/ProductionWorkbench.vue'
import Badge from '@/components/ui/Badge.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  useLabelDerivedTasks,
  labelKeyOf,
  LABEL_TASK_TERMINAL_STATUSES,
} from '@/composables/useLabelDerivedTasks'
import { ListMusic, Loader2, Lock, LockOpen, Music4, Package, Wand2 } from 'lucide-vue-next'

const taskStore = useTaskStore()
const taskControl = useWorkbenchTaskControl()
const captureScope = useWorkbenchScope()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

async function resolveDurable<T>(response: T | { task_id: string }): Promise<T> {
  const isCurrent = captureScope()

  if (!('task_id' in (response as object))) return response as T
  await withinScope(taskStore.refresh(), isCurrent)
  const task = await withinScope(
    waitForTask.wait((response as { task_id: string }).task_id),
    isCurrent,
  )
  if (task.status !== 'succeeded')
    throw new Error(task.error_message || (task.status === 'cancelled' ? '任务已取消' : '任务执行失败'))
  return (task.result ?? {}) as T
}

// ---------------------------------------------------------------------------
// 行数据（磁盘口径：02 章节 stem + 两个 08_bgm JSON 缓存 + 06/08 存在性）
// ---------------------------------------------------------------------------
const chapterRows = ref<BgmChapterRow[]>([])
const narrationFiles = ref<Record<string, string>>({})
const mode = ref('random')
// 段落级模式是当前页面的交互状态，不会在每次任务完成后写回 assignments.mode。
// 因此章节列表刷新只能在首次加载时从后端初始化，不能覆盖用户刚选的模式。
let modeInitialized = false
const lib = ref<MusicLibrary | null>(null)
let rowsRequest = 0
const loading = ref(false)
const loadError = ref('')
const error = ref('')
const selected = reactive<Record<string, boolean>>({})
const submitting = ref(false)
const packaging = ref(false)
const packagePreparing = ref(false)
const packageSelection = ref<string[]>([])

// ---------------------------------------------------------------------------
// 行任务派生：bgm-segment / bgm-mix 任务按 label 尾部「：{stem}」归位。
// ---------------------------------------------------------------------------
const BGM_MODULES = ['bgm-segment', 'bgm-mix', 'bgm']
const tasksByStem = useLabelDerivedTasks(BGM_MODULES)

type RowVariant = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'

interface BgmRow {
  stem: string
  data: BgmChapterRow
  task: TaskSnapshot | undefined // 混音在途优先于段落分析在途
  mixTask: TaskSnapshot | undefined
  segmentTask: TaskSnapshot | undefined
  failedTask: TaskSnapshot | undefined
  label: string
  variant: RowVariant
  matched: boolean
  mixable: boolean
  mixLabel: string
}

function segmentTimelineReady(row: BgmChapterRow): boolean {
  return (
    !!row.timeline &&
    !row.segment_music_missing &&
    (!!row.assignment?.segment || (!!row.segment_analysis && !row.segment_analysis.stale))
  )
}

// 行状态优先级：混音中 > 段落分析中 > 未合并（narration 缺失）> 已混音
// （无 BGM 章 copy2 后同样命中；段落级章 music=null 是正常形态，不误显「无 BGM」）
// > ⚠ 已删除（含时间轴曲目出库）> 已匹配（段落级章附时间轴 Badge）> 无 BGM
// > 段落已分析 / 段落分析已失效 > 未匹配。
const rows = computed<BgmRow[]>(() => {
  const { active, failed } = tasksByStem.value
  return chapterRows.value.map((d) => {
    const a = active.get(d.stem)
    const mixTask = a?.module === 'bgm-mix' ? a : undefined
    const segmentTask = a?.module === 'bgm-segment' ? a : undefined
    const task = mixTask ?? segmentTask
    const asg = d.assignment
    const music = asg?.music ?? null
    const missing = d.music_missing
    const segMode = !!asg?.segment
    const segmentView = mode.value === 'segment'
    const timelineReady = segmentTimelineReady(d)
    let label: string
    let variant: RowVariant
    if (task) {
      label = mixTask ? '混音中' : '段落分析中'
      variant = 'secondary'
    } else if (failed.get(d.stem)) {
      label = failed.get(d.stem)?.module === 'bgm-segment' ? '分析失败' : '混音失败'
      variant = 'destructive'
    } else if (missing || d.segment_music_missing) {
      label = '曲目已删除'
      variant = 'destructive'
    } else if (!d.narration_exists) {
      label = '未合并'
      variant = 'destructive'
    } else if (segmentView) {
      if (d.mix_exists) {
        label = asg?.segment ? '已混音（段落级）' : '已有章节混音'
        variant = 'success'
      } else if (d.timeline) {
        label = '已有时间轴'
        variant = 'default'
      } else {
        label = '待处理'
        variant = 'warning'
      }
    } else if (segMode && d.mix_exists) {
      label = '已混音（段落级）'
      variant = 'success'
    } else if (d.mix_exists) {
      label = segMode ? '已混音（段落级）' : music ? '已混音' : '已混音 / 无 BGM'
      variant = 'success'
    } else if (asg && (music || segMode)) {
      label = '已匹配'
      variant = 'default'
    } else if (asg) {
      label = '无 BGM'
      variant = 'secondary'
    } else if (d.segment_analysis) {
      label = d.segment_analysis.stale ? '段落分析已失效' : '段落已分析'
      variant = d.segment_analysis.stale ? 'warning' : 'outline'
    } else {
      label = '未匹配'
      variant = 'secondary'
    }
    // 段落级章的混音可行性 = 旁白在 + 时间轴在 + 曲目未出库（music=null 是正常形态）。
    const matched = segmentView ? timelineReady : !!asg
    const mixable = segmentView
      ? !!d.narration_exists && timelineReady && !task
      : segMode
        ? !!d.narration_exists && timelineReady && !task
        : !!d.narration_exists && !!asg && !(music && missing) && !task
    const mixLabel = segmentView ? '混音（时间轴）' : music ? '混音' : '无 BGM · 复制原声'
    return {
      stem: d.stem,
      data: d,
      task,
      mixTask,
      segmentTask,
      failedTask: task ? undefined : failed.get(d.stem),
      label,
      variant,
      matched,
      mixable,
      mixLabel,
    }
  })
})

const selectedNames = computed(() =>
  chapterRows.value.filter((r) => !!selected[r.stem]).map((r) => r.stem),
)
const selectedMixedNames = computed(() =>
  rows.value
    .filter((r) => !!selected[r.stem] && r.data.mix_exists && !r.mixTask)
    .map((r) => r.stem),
)
const selectedUnmixedCount = computed(() =>
  Math.max(
    0,
    packageSelection.value.length -
      packageSelection.value.filter((stem) => isMixReady(stem)).length,
  ),
)
const pendingAnalysisStems = computed(() =>
  rows.value
    .filter((r) => {
      if (r.task || r.data.mix_exists) return false
      return mode.value === 'segment'
        ? !r.data.segment_analysis || r.data.segment_analysis.stale
        : !r.data.assignment
    })
    .map((r) => r.stem),
)
const pendingMixStems = computed(() =>
  rows.value.filter((r) => !r.task && !r.data.mix_exists && r.mixable).map((r) => r.stem),
)
const bgmActive = computed(() =>
  taskStore.projectTasks.filter(
    (task) => task.task_type?.startsWith('bgm.') && !LABEL_TASK_TERMINAL_STATUSES.has(task.status),
  ),
)

// ---------------------------------------------------------------------------
// 刷新（磁盘口径）
// ---------------------------------------------------------------------------
async function refreshRows(options: { reloadLibrary?: boolean } = {}) {
  const isCurrent = captureScope()

  if (!projectSet.value) return
  const request = ++rowsRequest
  const reloadLibrary = options.reloadLibrary ?? true
  loading.value = true
  loadError.value = ''
  try {
    const [res, libRes, narrationDir] = await withinScope(
      Promise.all([
        getChapters(),
        reloadLibrary ? getLibrary() : Promise.resolve(lib.value),
        listDir('06_audio_merge'),
      ]),
      isCurrent,
    )
    // 内容未变的行沿用旧对象引用：批量完成时每章一次刷新会整体重拉 300+ 行，
    // 若每次整表换引用，v-memo 行会全部失效重渲。stringify 比对（~毫秒级）换掉
    // 整列表 DOM patch，只有真正变化的行更新。
    if (request !== rowsRequest) return
    const files: Record<string, string> = {}
    for (const item of narrationDir.items) {
      if (item.is_dir || !/\.(mp3|wav)$/i.test(item.name)) continue
      const stem = item.name.replace(/\.(mp3|wav)$/i, '')
      if (!files[stem] || item.name.toLowerCase().endsWith('.mp3')) files[stem] = item.name
    }
    narrationFiles.value = files
    const prevByStem = new Map(chapterRows.value.map((r) => [r.stem, r]))
    chapterRows.value = res.chapters.map((c) => {
      const prev = prevByStem.get(c.stem)
      return prev && JSON.stringify(prev) === JSON.stringify(c) ? prev : c
    })
    if (!modeInitialized) {
      // 后端对历史「llm」mode 值读作 random，前端只认 random / segment。
      mode.value = res.mode === 'segment' ? 'segment' : 'random'
      modeInitialized = true
    }
    if (reloadLibrary) lib.value = libRes
    for (const k of Object.keys(selected)) {
      if (!chapterRows.value.some((r) => r.stem === k)) delete selected[k]
    }
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    if (request === rowsRequest) loadError.value = e?.message || '加载失败'
  } finally {
    if (isCurrent()) {
      if (request === rowsRequest) loading.value = false
    }
  }
}

// 批量完成时（尤其无 BGM 章走 copy2 毫秒级连发）每章一次的全量重拉会风暴式打满
// 同源连接 + 整表替换行数据：尾沿防抖把 N 次收敛为 1~2 次（toast 仍逐章即时弹）。
let refreshTimer: number | undefined
function scheduleRefresh() {
  if (refreshTimer !== undefined) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void refreshRows()
  }, 800)
}

// ---------------------------------------------------------------------------
// 工具栏
// ---------------------------------------------------------------------------
function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}
function onSelectChange(stem: string, e: Event) {
  if ((e.target as HTMLInputElement).checked) selected[stem] = true
  else delete selected[stem]
}
function selectStems(stems: string[]) {
  clearSelection()
  for (const stem of stems) selected[stem] = true
}
function selectPendingAnalysis() {
  selectStems(pendingAnalysisStems.value)
}
function selectPendingMix() {
  selectStems(pendingMixStems.value)
}

// ---------------------------------------------------------------------------
// 提交 / 取消 / 重试
// ---------------------------------------------------------------------------
async function doRematchSelected() {
  const isCurrent = captureScope()

  if (
    !selectedNames.value.length ||
    submitting.value ||
    matching.value ||
    !projectSet.value ||
    loading.value ||
    loadError.value
  )
    return
  submitting.value = true
  error.value = ''
  try {
    if (!selectedStageNames.value.length) return
    const r = (await withinScope(
      resolveDurable(await withinScope(matchChapters(selectedStageNames.value, mode.value), isCurrent)),
      isCurrent,
    )) as any
    matchNote.value = `匹配完成：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({ title: '重匹配完成', variant: 'success', description: matchNote.value })
    await withinScope(refreshRows({ reloadLibrary: false }), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
    if (error.value.includes('在途'))
      toast({ title: '提交被拒绝', variant: 'destructive', description: error.value })
  } finally {
    if (isCurrent()) {
      submitting.value = false
    }
  }
}

async function doSegmentAnalyze() {
  const isCurrent = captureScope()

  if (
    !selectedNames.value.length ||
    submitting.value ||
    matching.value ||
    !projectSet.value ||
    loading.value ||
    loadError.value
  )
    return
  submitting.value = true
  error.value = ''
  try {
    if (!selectedStageNames.value.length) return
    await withinScope(analyzeSegmentChapters(selectedStageNames.value), isCurrent)
    await withinScope(taskStore.refresh(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
  } finally {
    if (isCurrent()) {
      submitting.value = false
    }
  }
}

async function doMix() {
  const isCurrent = captureScope()

  if (
    !selectedNames.value.length ||
    submitting.value ||
    matching.value ||
    !projectSet.value ||
    loading.value ||
    loadError.value
  )
    return
  submitting.value = true
  error.value = ''
  try {
    if (!selectedMixable.value.length) return
    if (
      selectedMixable.value.some(
        (stem) => rows.value.find((row) => row.stem === stem)?.data.mix_exists,
      ) &&
      !(await withinScope(
        showConfirm('所选可混音章节包含已有混音结果。继续会覆盖这些章节的旧混音音频。', {
          title: '确认重新混音',
          destructive: true,
        }),
        isCurrent,
      ))
    )
      return
    await withinScope(mixChapters(selectedMixable.value), isCurrent)
    await withinScope(taskStore.refresh(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    const msg = e?.message || '启动失败'
    error.value = msg
    if (msg.includes('在途'))
      toast({ title: '提交被拒绝', variant: 'destructive', description: msg })
  } finally {
    if (isCurrent()) {
      submitting.value = false
    }
  }
}

async function doMixRow(stem: string) {
  const isCurrent = captureScope()

  if (
    submitting.value ||
    matching.value ||
    !projectSet.value ||
    !rows.value.find((row) => row.stem === stem)?.mixable
  )
    return
  submitting.value = true
  try {
    if (
      rows.value.find((row) => row.stem === stem)?.data.mix_exists &&
      !(await withinScope(
        showConfirm('重新混音将覆盖本章已有混音音频。', {
          title: '确认重新混音',
          destructive: true,
        }),
        isCurrent,
      ))
    )
      return
    await withinScope(mixChapters([stem]), isCurrent)
    await withinScope(taskStore.refresh(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '混音启动失败', variant: 'destructive', description: e?.message })
  } finally {
    if (isCurrent()) submitting.value = false
  }
}

function cancelRow(task: TaskSnapshot) {
  void taskControl.control(task.id, 'cancel')
}
function retryRow(task: TaskSnapshot) {
  void taskControl.control(task.id, 'retry')
}
function cancelAll() {
  // 无专用 cancel-batch 端点：逐任务 cancel（PENDING 壳就地终结 + RUNNING 协作取消）。
  for (const t of bgmActive.value) void taskControl.control(t.id, 'cancel')
}

// ---------------------------------------------------------------------------
// 行操作
// ---------------------------------------------------------------------------
async function doSegmentAnalyzeRow(stem: string) {
  const isCurrent = captureScope()

  if (
    submitting.value ||
    matching.value ||
    !projectSet.value ||
    rows.value.find((row) => row.stem === stem)?.task
  )
    return
  submitting.value = true
  try {
    await withinScope(analyzeSegmentChapters([stem]), isCurrent)
    await withinScope(taskStore.refresh(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '段落分析启动失败', variant: 'destructive', description: e?.message })
  } finally {
    if (isCurrent()) submitting.value = false
  }
}

const matchSubmitting = ref(false)
const matching = computed(
  () =>
    matchSubmitting.value ||
    taskStore.projectTasks.some(
      (task) => task.task_type === 'bgm.match' && !LABEL_TASK_TERMINAL_STATUSES.has(task.status),
    ),
)
const matchNote = ref('')
const activeMatchTask = computed(() => taskStore.projectTasks.find(
  task => task.task_type === 'bgm.match' && !LABEL_TASK_TERMINAL_STATUSES.has(task.status),
))
const matchFeedback = computed(() => {
  const task = activeMatchTask.value
  if (task?.status === 'pending' || task?.status === 'queued')
    return '匹配任务已排队，等待 Worker 执行。可前往任务中心查看或取消。'
  if (task?.status === 'paused') return '匹配任务已暂停，可前往任务中心继续或取消。'
  if (task) return '正在执行匹配任务，可前往任务中心查看进度或取消。'
  if (matching.value) return '正在提交匹配任务…'
  return matchNote.value || (mode.value === 'segment'
    ? '选择章节后点击「分析所选」，生成段落时间轴。已有分配在执行后更新。'
    : '选择章节后点击「匹配所选」。已有音乐分配在重新匹配后更新。')
})

async function handlePackageClick() {
  const isCurrent = captureScope()

  if (!selectedNames.value.length || packaging.value || packagePreparing.value || !projectSet.value)
    return
  packagePreparing.value = true
  try {
    packageSelection.value = [...selectedNames.value]
    if (selectedMixedNames.value.length === selectedNames.value.length) {
      void doPackageDownload([...selectedNames.value])
      return
    }
    // Partial / zero mixing coverage — confirm through the shared dialog
    // (Q21): the zero-ready case is a single-button notice.
    const ready = packageSelection.value.filter(isMixReady)
    const message = ready.length
      ? `所选章节中有 ${selectedUnmixedCount.value} 个尚未完成混音。继续后只会打包 ${ready.length} 个已完成混音的章节，是否下载？`
      : '所选章节均尚未完成混音，当前没有可下载的音频。请完成混音后再打包。'
    const confirmed = await withinScope(
      showConfirm(message, {
        title: '确认打包下载',
        confirmText: ready.length ? '继续下载' : '知道了',
        hideCancel: !ready.length,
      }),
      isCurrent,
    )
    // The dialog can stay open while mixes finish — re-check readiness before
    // acting, so a "继续下载" can only ever download currently-ready chapters.
    if (confirmed) {
      const freshReady = packageSelection.value.filter(isMixReady)
      if (freshReady.length) void doPackageDownload(freshReady)
    }
  } catch (e: any) {
    if (isCurrent() && e?.name !== 'AbortError') error.value = e?.message || '打包准备失败'
  } finally {
    if (isCurrent()) packagePreparing.value = false
  }
}

function isMixReady(stem: string) {
  return rows.value.some((row) => row.stem === stem && row.data.mix_exists && !row.mixTask)
}

async function doPackageDownload(chapters: string[]) {
  const isCurrent = captureScope()

  if (!chapters.length || packaging.value || !projectSet.value) return
  packaging.value = true
  error.value = ''
  try {
    const result = (await withinScope(
      resolveDurable(await withinScope(packageMixedAudio(chapters), isCurrent)),
      isCurrent,
    )) as any
    downloadFile('08_bgm', result.path || result.zip_path || result.name)
    toast({
      title: '打包完成',
      variant: 'success',
      description: `${result.base}.zip（${result.file_count} 个音频文件）`,
    })
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    error.value = e?.message || '打包下载失败'
    toast({ title: '打包下载失败', variant: 'destructive', description: error.value })
  } finally {
    if (isCurrent()) {
      packaging.value = false
    }
  }
}

async function doRematch(stem: string) {
  const isCurrent = captureScope()

  if (matching.value || submitting.value || !projectSet.value) return
  matchSubmitting.value = true
  try {
    const r = (await withinScope(
      resolveDurable(await withinScope(matchChapters([stem], mode.value), isCurrent)),
      isCurrent,
    )) as any
    matchNote.value = `匹配完成：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({
      title: '重匹配完成',
      variant: 'success',
      description: `${stem}（${r.matched} 命中 / ${r.no_bgm} 无 BGM）`,
    })
    await withinScope(refreshRows(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '匹配失败', variant: 'destructive', description: e?.message })
  } finally {
    if (isCurrent()) {
      matchSubmitting.value = false
    }
  }
}

// 模式选择只决定下一次明确提交的操作，不改写已有分配或隐式提交全书任务。
function switchMode(m: 'random' | 'segment') {
  if (m === mode.value || matching.value || submitting.value || !projectSet.value) return
  mode.value = m
  modeInitialized = true
  matchNote.value = m === 'segment'
    ? '已选择 LLM 段落级：选择章节后点击「分析所选」，生成段落时间轴。'
    : '已选择全章节随机：选择章节后点击「匹配所选」。已有音乐分配保持不变。'
}

async function doLock(stem: string, locked: boolean) {
  const isCurrent = captureScope()

  if (submitting.value || matching.value || !projectSet.value) return
  submitting.value = true
  try {
    await withinScope(updateChapter(stem, { locked }), isCurrent)
    toast({ title: locked ? '已锁定' : '已解锁', variant: 'default', description: stem })
    await withinScope(refreshRows(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '操作失败', variant: 'destructive', description: e?.message })
  } finally {
    if (isCurrent()) submitting.value = false
  }
}

// ---------------------------------------------------------------------------
// 手动选曲弹层（enabled 曲目 + 试听 + 锁定 checkbox）
// ---------------------------------------------------------------------------
const manualStem = ref<string | null>(null)
const manualPick = ref<string | null>(null)
const manualLock = ref(false)
const manualBusy = ref(false)

let manualOpener: HTMLElement | null = null
let timelineOpener: HTMLElement | null = null
function openManual(row: BgmRow, event?: Event) {
  manualOpener = (event?.currentTarget as HTMLElement | null) ?? document.activeElement as HTMLElement
  manualStem.value = row.stem
  manualPick.value = row.data.assignment?.music ?? null
  manualLock.value = row.data.assignment?.locked ?? false
}

const manualTracks = computed(() =>
  Object.entries(lib.value?.tracks ?? {})
    .map(([name, tr]) => ({ name, ...tr }))
    .filter((tr) => tr.enabled)
    .sort((a, b) => a.name.localeCompare(b.name)),
)

async function saveManual() {
  const isCurrent = captureScope()

  if (
    !manualStem.value ||
    manualBusy.value ||
    !projectSet.value ||
    matching.value ||
    submitting.value
  )
    return
  manualBusy.value = true
  try {
    await withinScope(
      updateChapter(manualStem.value, {
        music: manualPick.value,
        locked: manualLock.value,
        __setMusic: true,
      }),
      isCurrent,
    )
    toast({
      title: '已手动选曲',
      variant: 'success',
      description: manualPick.value || '清除（无 BGM）',
    })
    manualStem.value = null
    await withinScope(refreshRows(), isCurrent)
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    if (isCurrent()) {
      manualBusy.value = false
    }
  }
}

// ---------------------------------------------------------------------------
// 时间轴查看弹层（段落级章：GET /api/bgm/timeline/{stem} 的完整时间轴文件）
// ---------------------------------------------------------------------------
const timelineStem = ref<string | null>(null)
const timelineData = ref<BgmTimeline | null>(null)
const timelineBusy = ref(false)
async function openTimeline(row: BgmRow, event?: Event) {
  timelineOpener = (event?.currentTarget as HTMLElement | null) ?? document.activeElement as HTMLElement
  const isCurrent = captureScope()

  if (timelineBusy.value) return
  timelineStem.value = row.stem
  timelineData.value = null
  timelineBusy.value = true
  try {
    const r = await withinScope(getTimeline(row.stem), isCurrent)
    if (timelineStem.value !== row.stem) return
    timelineData.value = r.timeline
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    toast({ title: '时间轴加载失败', variant: 'destructive', description: e?.message })
    timelineStem.value = null
  } finally {
    if (isCurrent()) {
      timelineBusy.value = false
    }
  }
}

function fmtMs(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// SSE 驱动（getter 式 watch）：终态任务 → toast + 重拉磁盘口径。
// processed 集合防重复（快照重放 / 断线重连）；转回非终态（重试）时释放。
// ---------------------------------------------------------------------------
const processed = new Set<string>()
let watcherArmed = false

watch(
  () =>
    taskStore.projectTasks
      .filter((t) => BGM_MODULES.includes(t.module))
      .map((t) => `${t.id}:${t.status}`)
      .join('|'),
  () => {
    if (!captureScope()()) return
    if (!watcherArmed) {
      watcherArmed = true
      for (const t of taskStore.projectTasks) {
        if (BGM_MODULES.includes(t.module) && LABEL_TASK_TERMINAL_STATUSES.has(t.status))
          processed.add(t.id)
      }
      return
    }
    for (const t of taskStore.projectTasks) {
      if (!BGM_MODULES.includes(t.module)) continue
      if (!LABEL_TASK_TERMINAL_STATUSES.has(t.status)) {
        processed.delete(t.id)
        continue
      }
      if (processed.has(t.id)) continue
      processed.add(t.id)
      scheduleRefresh()
      if (t.status === 'succeeded') {
        const stem = labelKeyOf(t.label)
        if (t.module === 'bgm-mix') {
          toast({ title: '混音完成', variant: 'success', description: stem })
        } else if (t.module === 'bgm-segment') {
          const r = t.result as { timeline?: boolean } | null
          toast({
            title: '段落分析完成',
            variant: 'success',
            description: r?.timeline
              ? `${stem}（已自动生成时间轴）`
              : `${stem}（时间轴未生成，请检查音频输入后重试段落分析）`,
          })
        }
        scheduleRefresh()
      }
    }
  },
)

watch(
  () => taskStore.projectTasks,
  () => {
    if (captureScope()()) scheduleRefresh()
  },
)

onMounted(async () => {
  const isCurrent = captureScope()

  try {
    await withinScope(taskStore.refresh(), isCurrent)
    await withinScope(refreshRows(), isCurrent)
  } catch (e: any) {
    if (!isCurrent() || e?.name === 'AbortError') return
    error.value = e?.message || '工作台初始化失败，请刷新重试。'
  }
})

// keep-alive 缓存页：重新进入时刷新磁盘口径。
onActivated(() => {
  void refreshRows()
})

const TAG_CATS: { key: MusicTagCategory; label: string; cls: string }[] = [
  {
    key: 'scene',
    label: '场景',
    cls: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30',
  },
  {
    key: 'mood',
    label: '气氛',
    cls: 'bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30',
  },
  {
    key: 'emotion',
    label: '情绪',
    cls: 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30',
  },
  {
    key: 'custom',
    label: '自定义',
    cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30',
  },
]

const selectedStageNames = computed(() => rows.value.filter(row => selected[row.stem] && !row.task).map(row => row.stem))
const selectedMixable = computed(() =>
  rows.value.filter((row) => selected[row.stem] && row.mixable).map((row) => row.stem),
)
const workRows = computed(() =>
  rows.value.map((row) => ({
    ...row,
    workKey: row.stem,
    workName: row.stem,
    workState: row.task
      ? 'running'
      : row.failedTask || row.data.music_missing || row.data.segment_music_missing
        ? 'error'
        : !row.data.narration_exists
          ? 'blocked'
          : row.data.mix_exists
            ? 'done'
            : row.mixable
              ? 'ready'
              : 'pending',
  })),
)
function selectFiltered(names: string[]) {
  selectStems(names)
}
function mixReason(row: BgmRow): string {
  if (row.task) return '本章任务正在执行，请等待或取消任务。'
  if (!row.data.narration_exists) return '缺少合并人声，请先完成音频合并。'
  if (row.data.music_missing || row.data.segment_music_missing)
    return '使用的曲目已删除，请更换曲目或重新分析。'
  if ((mode.value === 'segment' || row.data.assignment?.segment) && !segmentTimelineReady(row.data))
    return '缺少有效时间轴，请完成段落分析。'
  if (!row.data.assignment && mode.value !== 'segment') return '尚未匹配，请先匹配或手动选曲。'
  return ''
}

const manualPanel = ref<HTMLElement | null>(null)
const timelinePanel = ref<HTMLElement | null>(null)
const manualOpen = computed(() => !!manualStem.value)
const timelineOpen = computed(() => !!timelineStem.value)
function closeManual() {
  if (!manualBusy.value) manualStem.value = null
}
const manualKeydown = useWorkbenchDialog(manualOpen, manualPanel, closeManual, () => manualOpener)
const timelineKeydown = useWorkbenchDialog(timelineOpen, timelinePanel, () => {
  timelineStem.value = null
}, () => timelineOpener)
onDeactivated(() => {
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
  refreshTimer = undefined
  manualStem.value = null
  timelineStem.value = null
  submitting.value = false
  matchSubmitting.value = false
  packaging.value = false
  packagePreparing.value = false
  manualBusy.value = false
  timelineBusy.value = false
})
onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="viewport-workbench">
    <header class="page-header">
      <p class="eyebrow">Pipeline · BGM</p>
      <h1 class="page-title">背景音乐</h1>
      <p class="page-description">匹配曲目、核对段落与时间轴，将人声与音乐混合为最终音频。</p>
    </header>
    <div class="workbench-controls" tabindex="0" role="region" aria-label="制作条件与流程">
      <ProjectGateAlert />
      <WorkbenchContextBar>
        <template #icon><Music4 /></template>
        <template #title>01 匹配 → 02 核对与分析 → 03 混音与试听</template>
        <template #description><span class="flex min-w-0 items-center gap-1.5" role="status" aria-live="polite" :title="matchFeedback">
          <span v-if="matching" class="inline-flex h-4 w-4 shrink-0 items-center justify-center overflow-hidden" aria-hidden="true">
            <Loader2 v-if="matching" class="h-3.5 w-3.5 animate-spin" />
          </span>
          <span class="min-w-0 truncate">{{ matchFeedback }}</span>
        </span></template>
        <template #actions><label class="flex items-center gap-2"
          ><input
            type="radio"
            name="bgm-match-mode"
            :checked="mode === 'random'"
            :disabled="matching || submitting || !projectSet"
            class="accent-primary"
            @change="switchMode('random')"
          />全章节随机</label
        ><label class="flex items-center gap-2"
          ><input
            type="radio"
            name="bgm-match-mode"
            :checked="mode === 'segment'"
            :disabled="matching || submitting || !projectSet"
            class="accent-primary"
            @change="switchMode('segment')"
          />LLM 段落级</label
        ></template>
      </WorkbenchContextBar>
    </div>
    <ProductionWorkbench
      :rows="workRows"
      :selected="selected"
      :loading="loading"
      :load-error="loadError"
      :disabled="submitting || matching || !projectSet"
      :row-disabled="(row) => !!row.task"
      :overlay-open="!!manualStem || !!timelineStem"
      :filters="[
        { key: 'pending', label: '待匹配 / 分析' },
        { key: 'ready', label: '待混音' },
        { key: 'blocked', label: '缺少人声' },
        { key: 'running', label: '执行中' },
        { key: 'error', label: '异常' },
        { key: 'done', label: '已混音' },
      ]"
      :columns="[
        { label: '匹配曲目', width: '120px' },
        { label: '段落分析', width: '82px' },
        { label: '混音 / 任务', width: '140px' },
      ]"
      label="背景音乐"
      empty-text="暂无章节文本，请先完成排版与分册。"
      @refresh="refreshRows"
      @select="onSelectChange"
      @select-filtered="selectFiltered"
    >
      <template #selection
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || matching || !pendingAnalysisStems.length"
          @click="selectPendingAnalysis"
          >{{ mode === 'segment' ? '选择待分析' : '选择未匹配' }}</Button
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || matching || !pendingMixStems.length"
          @click="selectPendingMix"
          >选择待混音</Button
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || matching || !selectedNames.length"
          @click="clearSelection"
          >清空</Button
        ></template
      >
      <template #empty
        ><RouterLink to="/text" class="mt-3 inline-block text-primary underline"
          >前往排版与分册</RouterLink
        ></template
      >
      <template #cells="{ row }"
        ><td>
          <span
            v-if="row.data.assignment?.music && !row.data.assignment.segment"
            class="block truncate"
            :title="row.data.assignment?.music ?? ''"
            :class="{
              'text-destructive': row.data.music_missing || row.data.segment_music_missing,
            }"
            >{{ row.data.assignment.music }}</span>
          <WorkbenchStatus v-else :variant="row.data.assignment ? 'default' : 'secondary'"
            >{{ row.data.assignment?.segment ? '段落时间轴' : row.data.assignment ? '无 BGM · 原声' : '未匹配' }}</WorkbenchStatus>
          <span v-if="row.data.assignment?.locked" class="block text-[10px] text-muted-foreground"
            >已锁定</span
          >
        </td>
        <td>
          <WorkbenchStatus :variant="row.data.segment_analysis?.stale ? 'warning' : row.data.segment_analysis ? 'success' : 'secondary'">{{
            row.data.segment_analysis
              ? row.data.segment_analysis.stale
                ? '已失效'
                : '已分析'
              : '未分析'
          }}</WorkbenchStatus
          ><span v-if="row.data.timeline" class="block text-[10px] text-muted-foreground"
            >{{ row.data.timeline.sections }} 个音乐段</span
          >
        </td>
        <td>
          <WorkbenchStatus :variant="row.variant" :mixed="row.data.mix_exists">{{
            row.task
              ? `${Math.round(row.task.progress * 100)}% · ${row.task.module === 'bgm-mix' ? '混音' : '分析'}`
              : row.label
          }}</WorkbenchStatus>
        </td></template
      >
      <template #detail="{ row }">
        <WorkbenchStatus :variant="row.variant" :mixed="row.data.mix_exists">{{ row.label }}</WorkbenchStatus>
        <div class="production-section">
          <h3>匹配曲目</h3>
          <p class="break-all">
            {{
              row.data.assignment?.music ||
              (row.data.assignment?.segment
                ? '按段落时间轴分配音乐'
                : row.data.assignment
                  ? '无 BGM，混音时复制原声'
                  : '尚未匹配')
            }}
          </p>
          <MiniAudioPlayer
            v-if="row.data.assignment?.music && !row.data.music_missing"
            :key="row.data.assignment.music"
            class="mt-3"
            :src="musicPreviewUrl(row.data.assignment.music)"
            preload-metadata
          />
          <p v-if="row.data.assignment?.reason" class="mt-2 break-words text-muted-foreground">
            匹配依据：{{ row.data.assignment.reason }}
          </p>
          <p v-if="!manualTracks.length" class="mt-2 text-muted-foreground">
            音乐库暂无启用曲目。请联系管理员维护音乐库，也可手动指定无 BGM。
          </p>
          <div class="mt-3 flex flex-wrap gap-2">
            <Button
              v-if="mode !== 'segment'"
              variant="outline"
              size="sm"
              :disabled="!projectSet || submitting || matching || !!row.task"
              @click="openManual(row, $event)"
              >更换曲目</Button
            ><Button
              variant="outline"
              size="sm"
              :disabled="!projectSet || submitting || matching || !!row.task"
              @click="doLock(row.stem, !(row.data.assignment?.locked ?? false))"
              ><Lock v-if="row.data.assignment?.locked" class="h-3 w-3" /><LockOpen
                v-else
                class="h-3 w-3"
              />{{ row.data.assignment?.locked ? '解锁' : '锁定' }}</Button
            ><Button
              variant="outline"
              size="sm"
              :disabled="!projectSet || submitting || matching || !!row.task"
              @click="mode === 'segment' ? doSegmentAnalyzeRow(row.stem) : doRematch(row.stem)"
              >{{ mode === 'segment' ? '段落分析本章' : '重新匹配本章' }}</Button
            >
          </div>
          <p class="mt-2 text-[11px] text-muted-foreground">锁定章节会跳过重新匹配。</p>
        </div>
        <div class="production-section">
          <h3>段落与时间轴</h3>
          <dl class="production-facts">
            <dt>分析状态</dt>
            <dd>
              <WorkbenchStatus :variant="row.data.segment_analysis?.stale ? 'warning' : row.data.segment_analysis ? 'success' : 'secondary'">
              {{
                row.data.segment_analysis
                  ? row.data.segment_analysis.stale
                    ? '已失效'
                    : '已分析'
                  : '尚未分析'
              }}
              </WorkbenchStatus>
            </dd>
            <dt>音乐段</dt>
            <dd>{{ row.data.timeline?.sections ?? '尚未生成' }}</dd>
            <dt>时间轴时长</dt>
            <dd>
              {{ row.data.timeline?.duration != null ? fmtMs(row.data.timeline.duration) : '未知' }}
            </dd>
          </dl>
          <Button
            v-if="row.data.timeline"
            variant="outline"
            size="sm"
            class="mt-3"
            :disabled="timelineBusy"
            @click="openTimeline(row, $event)"
            ><ListMusic class="h-3.5 w-3.5" />查看时间范围与强度</Button
          >
        </div>
        <div class="production-section">
          <h3>人声与最终混音</h3>
          <p class="mb-2 text-muted-foreground">原始人声 · 合并结果</p>
          <MiniAudioPlayer
            v-if="row.data.narration_exists && narrationFiles[row.stem]"
            :key="narrationFiles[row.stem]"
            :src="downloadUrl('06_audio_merge', narrationFiles[row.stem])"
            preload-metadata
          />
          <p v-else class="text-destructive">人声缺失，请先合并章节。</p>
          <p class="mb-2 mt-4 text-muted-foreground">最终混音 · 现有产物</p>
          <p class="mb-2 text-[11px] text-muted-foreground">
            更换曲目、更新人声或重新分析后，请再次混音以更新现有产物。
          </p>
          <MiniAudioPlayer
            v-if="row.data.mix_exists && !row.mixTask"
            :key="row.stem + '-mix'"
            :src="bgmPreviewUrl(row.stem)"
            preload-metadata
          />
          <p v-else class="text-muted-foreground">
            {{ row.mixTask ? '混音任务执行中，旧结果暂不可试听。' : '尚无混音结果' }}
          </p>
          <Button
            v-if="row.data.mix_exists && !row.mixTask"
            variant="outline"
            size="sm"
            class="mt-2"
            @click="downloadFile('08_bgm', row.stem + '.mp3')"
            >下载最终混音</Button
          >
          <p class="mt-3 text-[11px] text-muted-foreground">
            混音音量、淡入淡出与强度由管理员统一配置。
          </p>
        </div>
        <div class="production-section">
          <p v-if="mixReason(row)" class="mb-3 text-destructive">{{ mixReason(row) }}</p>
          <RouterLink
            v-if="!row.data.narration_exists"
            to="/merge"
            class="mb-3 inline-block text-primary underline"
            >前往合并补齐人声</RouterLink
          >
          <div class="flex flex-wrap gap-2">
            <Button
              :disabled="!projectSet || !row.mixable || submitting || matching || loading || !!loadError"
              :title="mixReason(row) || '混音本章'"
              size="sm"
              @click="doMixRow(row.stem)"
              >{{ row.data.mix_exists ? '重新混音本章' : row.mixLabel }}</Button
            ><Button v-if="row.task" variant="destructive" size="sm" @click="cancelRow(row.task)"
              >取消任务</Button
            ><Button
              v-if="row.failedTask"
              variant="outline"
              size="sm"
              :disabled="submitting || matching || !projectSet"
              @click="retryRow(row.failedTask)"
              >重试失败任务</Button
            >
          </div>
          <p v-if="row.failedTask" class="mt-3 break-words text-destructive">
            {{ row.failedTask.error || '任务失败' }}
          </p>
          <LiveLogPanel
            v-if="row.task || row.failedTask"
            :task="row.task || row.failedTask || null"
            :max-height-class="'h-40'"
            class="mt-3"
          />
        </div>
      </template>
    </ProductionWorkbench>
    <div class="production-actionbar">
      <div class="mr-auto text-xs">
        <strong
          >已选 {{ selectedNames.length }} 章 · 可混音 {{ selectedMixable.length }} · 可下载
          {{ selectedMixedNames.length }}</strong
        >
        <p class="mt-1 text-muted-foreground">
          匹配跳过锁定章；匹配 / 分析跳过运行中章节；混音仅提交可用输入（跳过
          {{ selectedNames.length - selectedMixable.length }} 章）；下载只打包已完成结果。
        </p>
      </div>
      <Button
        variant="outline"
        :disabled="!projectSet || submitting || matching || loading || !!loadError || !selectedStageNames.length"
        @click="mode === 'segment' ? doSegmentAnalyze() : doRematchSelected()"
        ><Wand2 class="h-4 w-4" />{{ mode === 'segment' ? '分析所选' : '匹配所选' }}（{{ selectedStageNames.length }} 章）</Button
      ><Button
        :disabled="!projectSet || submitting || matching || loading || !!loadError || !selectedMixable.length"
        @click="doMix"
        ><Loader2 v-if="submitting" class="h-4 w-4 animate-spin" /><Music4
          v-else
          class="h-4 w-4"
        />混音（{{ selectedMixable.length }} 章）</Button
      ><Button
        variant="outline"
        :disabled="!projectSet || packaging || packagePreparing || !selectedNames.length"
        @click="handlePackageClick"
        ><Loader2 v-if="packaging" class="h-4 w-4 animate-spin" /><Package
          v-else
          class="h-4 w-4"
        />{{ packaging ? '打包中…' : '打包下载' }}</Button
      ><Button v-if="bgmActive.length" variant="destructive" @click="cancelAll"
        >取消全部任务</Button
      >
    </div>
    <div v-if="error" class="workbench-feedback" tabindex="0" role="region" aria-label="制作反馈与报告">
      <Alert v-if="error" variant="destructive">{{ error }}</Alert>
    </div>
    <div
      v-if="manualStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeManual"
    >
      <div
        ref="manualPanel"
        tabindex="-1"
        role="dialog"
        aria-modal="true"
        aria-label="手动选曲"
        class="w-full max-w-lg rounded-xl border bg-background p-4 shadow-lg"
        @keydown="manualKeydown"
      >
        <h2 class="text-lg font-semibold">手动选曲：{{ manualStem }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">
          选择曲目后，该章节将使用手动指定的音乐；锁定后不会被重新匹配。
        </p>
        <ScrollArea class="mt-3 h-72 rounded-md border">
          <div class="space-y-1 p-2">
            <label
              class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50"
            >
              <input
                type="radio"
                :value="null"
                v-model="manualPick"
                :disabled="manualBusy"
                class="h-4 w-4 accent-primary"
              />
              <span class="text-sm text-muted-foreground">无 BGM（混音时直接复制原声）</span>
            </label>
            <label
              v-for="tr in manualTracks"
              :key="tr.name"
              class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50"
            >
              <input
                type="radio"
                :value="tr.name"
                v-model="manualPick"
                :disabled="manualBusy"
                class="h-4 w-4 accent-primary"
              />
              <span class="min-w-0 flex-1 truncate text-sm" :title="tr.name">{{ tr.name }}</span>
              <MiniAudioPlayer :src="musicPreviewUrl(tr.name)" />
            </label>
            <p v-if="!manualTracks.length" class="px-2 py-3 text-xs text-muted-foreground">
              音乐库中没有启用的曲目——请先到「音乐库」上传并启用。
            </p>
          </div>
        </ScrollArea>
        <label class="mt-3 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            v-model="manualLock"
            :disabled="manualBusy"
            class="h-4 w-4 accent-primary"
          />
          锁定（重匹配时整章跳过）
        </label>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="closeManual" :disabled="manualBusy"
            >关闭</Button
          >
          <Button size="sm" :disabled="manualBusy" @click="saveManual">
            <Loader2 v-if="manualBusy" class="h-3.5 w-3.5 animate-spin" />
            确认
          </Button>
        </div>
      </div>
    </div>

    <!-- 时间轴查看弹层（段落级章：08_bgm/timelines/<stem>.json 的完整内容） -->
    <div
      v-if="timelineStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3 sm:p-6"
      @click.self="timelineStem = null"
    >
      <section
        ref="timelinePanel"
        tabindex="-1"
        @keydown="timelineKeydown"
        class="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border bg-background shadow-2xl sm:max-h-[calc(100dvh-3rem)]"
        role="dialog"
        aria-modal="true"
        aria-labelledby="timeline-dialog-title"
      >
        <header class="border-b px-4 py-4 sm:px-6">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                段落级背景音乐
              </p>
              <h2
                id="timeline-dialog-title"
                class="mt-1 break-words text-lg font-semibold sm:text-xl"
              >
                BGM 时间轴：{{ timelineStem }}
              </h2>
            </div>
            <Button variant="outline" size="sm" class="shrink-0" @click="timelineStem = null"
              >关闭</Button
            >
          </div>
          <div v-if="timelineData" class="mt-4 grid gap-2 text-xs sm:grid-cols-3">
            <div class="rounded-lg border bg-accent/30 px-3 py-2">
              <p class="text-muted-foreground">总时长</p>
              <p class="mt-0.5 font-medium tabular-nums">{{ fmtMs(timelineData.duration) }}</p>
            </div>
            <div class="rounded-lg border bg-accent/30 px-3 py-2">
              <p class="text-muted-foreground">音乐段</p>
              <p class="mt-0.5 font-medium tabular-nums">{{ timelineData.timeline.length }} 段</p>
            </div>
            <div class="min-w-0 rounded-lg border bg-accent/30 px-3 py-2">
              <p class="text-muted-foreground">分析模型</p>
              <p class="mt-0.5 truncate font-medium" :title="timelineData.model">
                {{ timelineData.model }}
              </p>
            </div>
          </div>
          <p v-if="timelineData" class="mt-3 text-xs leading-5 text-muted-foreground">
            生成于 {{ timelineData.generated_at }} · 混音会按此时间轴逐段叠加 BGM，音量 = 基准 ×
            强度档位。
          </p>
        </header>

        <p v-if="!timelineData" class="px-6 py-8 text-sm text-muted-foreground">加载中…</p>
        <template v-else>
          <p
            v-if="timelineData.duration <= 0"
            class="border-b px-6 py-4 text-sm text-muted-foreground"
          >
            全章无 BGM 段（混音 = 直接复制原声）。
          </p>

          <ScrollArea class="min-h-0 flex-1 bg-muted/20">
            <ol class="space-y-3 p-4 sm:p-6">
              <li
                v-for="(sp, i) in timelineData.timeline"
                :key="i"
                class="grid gap-3 sm:grid-cols-[7.5rem_minmax(0,1fr)]"
              >
                <div class="flex items-start gap-2 text-xs sm:block">
                  <span
                    class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground"
                    >{{ i + 1 }}</span
                  >
                  <div class="pt-1 tabular-nums text-muted-foreground sm:mt-1 sm:pl-1">
                    <p>{{ fmtMs(sp.start) }} – {{ fmtMs(sp.end) }}</p>
                    <p class="mt-0.5">持续 {{ fmtMs(sp.end - sp.start) }}</p>
                  </div>
                </div>
                <article class="min-w-0 rounded-xl border bg-background p-3 shadow-sm sm:p-4">
                  <div class="flex flex-wrap items-center gap-2">
                    <p class="min-w-0 break-all text-sm font-semibold">{{ sp.music_id }}</p>
                    <MiniAudioPlayer :src="musicPreviewUrl(sp.music_id)" />
                    <span
                      class="ml-auto rounded-md bg-accent px-2 py-1 text-xs tabular-nums text-muted-foreground"
                      >强度 {{ sp.intensity }}/3</span
                    >
                    <span
                      class="rounded-md bg-accent px-2 py-1 text-xs tabular-nums text-muted-foreground"
                      >音量 {{ sp.volume.toFixed(2) }}</span
                    >
                  </div>
                  <p v-if="sp.scene_desc || sp.mood_desc" class="mt-3 text-sm leading-5">
                    <span v-if="sp.scene_desc">{{ sp.scene_desc }}</span>
                    <span v-if="sp.scene_desc && sp.mood_desc" class="text-muted-foreground">
                      ·
                    </span>
                    <span v-if="sp.mood_desc" class="text-muted-foreground">{{
                      sp.mood_desc
                    }}</span>
                  </p>
                  <p
                    v-if="sp.switch_reason || sp.reason"
                    class="mt-2 break-words text-xs leading-5 text-muted-foreground"
                  >
                    {{ sp.switch_reason || sp.reason }}
                  </p>
                  <div
                    v-if="TAG_CATS.some((cat) => (sp.tags[cat.key] ?? []).length)"
                    class="mt-3 flex flex-wrap gap-1.5"
                  >
                    <template v-for="cat in TAG_CATS" :key="cat.key">
                      <Badge
                        v-for="t in sp.tags[cat.key] ?? []"
                        :key="cat.key + t"
                        variant="outline"
                        :class="cat.cls"
                        >{{ t }}</Badge
                      >
                    </template>
                  </div>
                </article>
              </li>
              <li
                v-if="!timelineData.timeline.length"
                class="rounded-xl border border-dashed bg-background px-4 py-8 text-center text-sm text-muted-foreground"
              >
                此章尚未生成可播放的 BGM 段。
              </li>
            </ol>
          </ScrollArea>
        </template>
        <footer
          class="flex items-center justify-between gap-3 border-t bg-background px-4 py-3 text-xs text-muted-foreground sm:px-6"
        >
          <span>可在上方试听每段使用的曲目</span>
          <Button variant="outline" size="sm" @click="timelineStem = null">关闭</Button>
        </footer>
      </section>
    </div>
  </div>
</template>
