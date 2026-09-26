<script setup lang="ts">
// 背景音乐（阶段 7）：章节气氛分析（LLM 缓存）→ 匹配（标签评分 / 随机）→ 最终混音。
// 行 / 派生 / SSE / F5 全复刻 Merge.vue 范本：任务按 label 尾部「：{stem}」派生式重挂
// （无本地 job 列表、不依赖后端注册表），终态经 getter 式 watch 驱动行刷新。
import { computed, nextTick, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import {
  analyzeChapters,
  analyzeSegmentChapters,
  bgmPreviewUrl,
  getChapters,
  getTimeline,
  matchChapters,
  mixChapters,
  packageMixedAudio,
  updateChapter,
} from '@/api/bgm'
import { downloadFile } from '@/utils/fileops'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import { getLibrary, musicPreviewUrl } from '@/api/music'
import type {
  BgmChapterRow,
  BgmTimeline,
  MusicLibrary,
  MusicTagCategory,
  TaskSnapshot,
  TrackTags,
} from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import Switch from '@/components/ui/Switch.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import { useLabelDerivedTasks, labelKeyOf, LABEL_TASK_TERMINAL_STATUSES } from '@/composables/useLabelDerivedTasks'
import {
  Eraser,
  ListChecks,
  ListMusic,
  Loader2,
  Lock,
  LockOpen,
  Music,
  Music4,
  Package,
  Pencil,
  RefreshCw,
  Shuffle,
  Wand2,
  XCircle,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

async function resolveDurable<T>(response: T | { task_id: string }): Promise<T> {
  if (!('task_id' in (response as object))) return response as T
  const task = await waitForTask.wait((response as { task_id: string }).task_id)
  if (task.status !== 'succeeded') throw new Error(task.error_message || '任务执行失败')
  return (task.result ?? {}) as T
}

// ---------------------------------------------------------------------------
// 行数据（磁盘口径：02 章节 stem + 两个 08_bgm JSON 缓存 + 06/08 存在性）
// ---------------------------------------------------------------------------
const chapterRows = ref<BgmChapterRow[]>([])
const mode = ref('llm')
// 段落级模式是当前页面的交互状态，不会在每次任务完成后写回 assignments.mode。
// 因此章节列表刷新只能在首次加载时从后端初始化，不能覆盖用户刚选的模式。
let modeInitialized = false
const lib = ref<MusicLibrary | null>(null)
const loading = ref(false)
const loadError = ref('')
const error = ref('')
const selected = reactive<Record<string, boolean>>({})
const submitting = ref(false)
const packaging = ref(false)
const packageSelection = ref<string[]>([])

// ---------------------------------------------------------------------------
// 行任务派生：bgm-analysis / bgm-segment / bgm-mix 任务按 label 尾部「：{stem}」归位。
// ---------------------------------------------------------------------------
const BGM_MODULES = ['bgm-analysis', 'bgm-segment', 'bgm-mix']
const tasksByStem = useLabelDerivedTasks(BGM_MODULES)

type RowVariant = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'

interface BgmRow {
  stem: string
  data: BgmChapterRow
  task: TaskSnapshot | undefined // 混音在途优先于段落分析在途、分析在途
  mixTask: TaskSnapshot | undefined
  segmentTask: TaskSnapshot | undefined
  analysisTask: TaskSnapshot | undefined
  failedTask: TaskSnapshot | undefined
  label: string
  variant: RowVariant
  matched: boolean
  mixable: boolean
  mixLabel: string
}

function segmentTimelineReady(row: BgmChapterRow): boolean {
  return !!row.timeline && !row.segment_music_missing && (
    !!row.assignment?.segment || (!!row.segment_analysis && !row.segment_analysis.stale)
  )
}

// 行状态优先级：混音中 > 段落分析中 > 分析中 > 未合并（narration 缺失）> 已混音
// （无 BGM 章 copy2 后同样命中；段落级章 music=null 是正常形态，不误显「无 BGM」）
// > ⚠ 已删除（含时间轴曲目出库）> 已匹配（段落级章附时间轴 Badge）> 无 BGM
// > 段落已分析 / 段落分析已失效 > 已分析 > 未分析。
const rows = computed<BgmRow[]>(() => {
  const { active, failed } = tasksByStem.value
  return chapterRows.value.map((d) => {
    const a = active.get(d.stem)
    const mixTask = a?.module === 'bgm-mix' ? a : undefined
    const segmentTask = a?.module === 'bgm-segment' ? a : undefined
    const analysisTask = a?.module === 'bgm-analysis' ? a : undefined
    const task = mixTask ?? segmentTask ?? analysisTask
    const asg = d.assignment
    const music = asg?.music ?? null
    const missing = d.music_missing
    const segMode = !!asg?.segment
    const segmentView = mode.value === 'segment'
    const timelineReady = segmentTimelineReady(d)
    let label: string
    let variant: RowVariant
    if (segmentView) {
      if (d.mix_exists) {
        label = '已混音/段落BGM'
        variant = 'success'
      } else if (d.timeline) {
        label = '已有时间轴'
        variant = 'default'
      } else {
        label = '待处理'
        variant = 'warning'
      }
    } else if (mixTask) {
      label = '混音中'
      variant = 'secondary'
    } else if (segmentTask) {
      label = '段落分析中'
      variant = 'secondary'
    } else if (analysisTask) {
      label = '分析中'
      variant = 'secondary'
    } else if (segMode && d.mix_exists) {
      label = '已混音（段落级）'
      variant = 'success'
    } else if (!d.narration_exists) {
      label = '未合并'
      variant = 'destructive'
    } else if (d.mix_exists) {
      label = segMode ? '已混音（段落级）' : music ? '已混音' : '已混音 / 无 BGM'
      variant = 'success'
    } else if (missing || d.segment_music_missing) {
      label = '⚠ 已删除'
      variant = 'destructive'
    } else if (asg && (music || segMode)) {
      label = '已匹配'
      variant = 'default'
    } else if (asg) {
      label = '无 BGM'
      variant = 'secondary'
    } else if (d.segment_analysis) {
      label = d.segment_analysis.stale ? '段落分析已失效' : '段落已分析'
      variant = d.segment_analysis.stale ? 'warning' : 'outline'
    } else if (d.analysis) {
      label = '已分析'
      variant = 'outline'
    } else {
      label = '未分析'
      variant = 'secondary'
    }
    // 段落级章的混音可行性 = 旁白在 + 时间轴在 + 曲目未出库（music=null 是正常形态）。
    const matched = segmentView ? timelineReady : !!asg
    const mixable = segmentView
      ? !!d.narration_exists && timelineReady && !task
      : segMode
        ? !!d.narration_exists && timelineReady && !task
        : !!d.narration_exists && !!asg && !(music && missing) && !task
    const mixLabel = segmentView
      ? '混音（时间轴）'
      : music
        ? '混音'
        : '无 BGM · 复制原声'
    return {
      stem: d.stem,
      data: d,
      task,
      mixTask,
      segmentTask,
      analysisTask,
      failedTask: task ? undefined : failed.get(d.stem),
      label,
      variant,
      matched,
      mixable,
      mixLabel,
    }
  })
})

const selectedNames = computed(() => chapterRows.value.filter((r) => !!selected[r.stem]).map((r) => r.stem))
const selectedMixedNames = computed(() => rows.value
  .filter((r) => !!selected[r.stem] && r.data.mix_exists && !r.mixTask)
  .map((r) => r.stem))
const selectedUnmixedCount = computed(() => Math.max(0, packageSelection.value.length - packageSelection.value.filter((stem) =>
  isMixReady(stem),
).length))
const matchedRows = computed(() => rows.value.filter((r) => r.matched))
const mixedCount = computed(() => chapterRows.value.filter((r) => r.mix_exists).length)
const pendingAnalysisStems = computed(() =>
  rows.value
    .filter((r) => {
      if (r.task || r.data.mix_exists) return false
      return mode.value === 'segment'
        ? !r.data.segment_analysis || r.data.segment_analysis.stale
        : !r.data.analysis
    })
    .map((r) => r.stem),
)
const pendingMixStems = computed(() =>
  rows.value.filter((r) => !r.task && !r.data.mix_exists && r.mixable).map((r) => r.stem),
)
const bgmActive = computed(() =>
  taskStore
    .activeTasks('bgm-analysis')
    .concat(taskStore.activeTasks('bgm-segment'))
    .concat(taskStore.activeTasks('bgm-mix')),
)

// ---------------------------------------------------------------------------
// 刷新（磁盘口径）
// ---------------------------------------------------------------------------
async function refreshRows(options: { reloadLibrary?: boolean } = {}) {
  const reloadLibrary = options.reloadLibrary ?? true
  loading.value = true
  loadError.value = ''
  try {
    const [res, libRes] = await Promise.all([
      getChapters(),
      reloadLibrary ? getLibrary() : Promise.resolve(lib.value),
    ])
    // 内容未变的行沿用旧对象引用：批量完成时每章一次刷新会整体重拉 300+ 行，
    // 若每次整表换引用，v-memo 行会全部失效重渲。stringify 比对（~毫秒级）换掉
    // 整列表 DOM patch，只有真正变化的行更新。
    const prevByStem = new Map(chapterRows.value.map((r) => [r.stem, r]))
    chapterRows.value = res.chapters.map((c) => {
      const prev = prevByStem.get(c.stem)
      return prev && JSON.stringify(prev) === JSON.stringify(c) ? prev : c
    })
    if (!modeInitialized) {
      mode.value = res.mode || 'llm'
      modeInitialized = true
    }
    if (reloadLibrary) lib.value = libRes
    for (const k of Object.keys(selected)) {
      if (!chapterRows.value.some((r) => r.stem === k)) delete selected[k]
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    loadError.value = e?.message || '加载失败'
  } finally {
    loading.value = false
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
async function doAnalyze() {
  if (!selectedNames.value.length || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await analyzeChapters(selectedNames.value)
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
  } finally {
    submitting.value = false
  }
}

async function doSegmentAnalyze() {
  if (!selectedNames.value.length || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await analyzeSegmentChapters(selectedNames.value)
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
  } finally {
    submitting.value = false
  }
}

async function doMix() {
  if (!selectedNames.value.length || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await mixChapters(selectedNames.value)
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    const msg = e?.message || '启动失败'
    error.value = msg
    if (msg.includes('在途')) toast({ title: '提交被拒绝', variant: 'destructive', description: msg })
  } finally {
    submitting.value = false
  }
}

async function doMixRow(stem: string) {
  try {
    await mixChapters([stem])
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '混音启动失败', variant: 'destructive', description: e?.message })
  }
}

function cancelRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'cancel')
}
function retryRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'retry')
}
function cancelAll() {
  // 无专用 cancel-batch 端点：逐任务 cancel（PENDING 壳就地终结 + RUNNING 协作取消）。
  for (const t of bgmActive.value) taskStore.control(t.id, 'cancel')
}

// ---------------------------------------------------------------------------
// 行操作
// ---------------------------------------------------------------------------
async function doAnalyzeRow(stem: string) {
  try {
    await analyzeChapters([stem])
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '分析启动失败', variant: 'destructive', description: e?.message })
  }
}

async function doSegmentAnalyzeRow(stem: string) {
  try {
    await analyzeSegmentChapters([stem])
    await taskStore.refresh()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '段落分析启动失败', variant: 'destructive', description: e?.message })
  }
}

const matching = ref(false)
const matchNote = ref('')
const pendingMode = ref<string | null>(null)
const displayedMode = computed(() => pendingMode.value ?? mode.value)

function waitForPaint() {
  return new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
}

async function handlePackageClick() {
  if (!selectedNames.value.length || packaging.value || !projectSet.value) return
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
  const confirmed = await showConfirm(message, {
    title: '确认打包下载',
    confirmText: ready.length ? '继续下载' : '知道了',
    hideCancel: !ready.length,
  })
  // The dialog can stay open while mixes finish — re-check readiness before
  // acting, so a "继续下载" can only ever download currently-ready chapters.
  if (confirmed) {
    const freshReady = packageSelection.value.filter(isMixReady)
    if (freshReady.length) void doPackageDownload(freshReady)
  }
}

function isMixReady(stem: string) {
  return rows.value.some((row) => row.stem === stem && row.data.mix_exists && !row.mixTask)
}

async function doPackageDownload(chapters: string[]) {
  if (!chapters.length || packaging.value || !projectSet.value) return
  packaging.value = true
  error.value = ''
  try {
    const result = await resolveDurable(await packageMixedAudio(chapters)) as any
    downloadFile('08_bgm', result.path || result.zip_path || result.name)
    toast({
      title: '打包完成',
      variant: 'success',
      description: `${result.base}.zip（${result.file_count} 个音频文件）`,
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '打包下载失败'
    toast({ title: '打包下载失败', variant: 'destructive', description: error.value })
  } finally {
    packaging.value = false
  }
}

function rowTags(row: BgmRow): TrackTags {
  const tags = mode.value === 'segment'
    ? row.data.segment_analysis?.tags
    : null
  return (tags ?? row.data.analysis ?? row.data.assignment?.tags ?? {}) as TrackTags
}

async function doRematch(stem: string) {
  if (matching.value) return
  matching.value = true
  try {
    const r = await resolveDurable(await matchChapters([stem], mode.value)) as any
    matchNote.value = `匹配完成：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({ title: '重匹配完成', variant: 'success', description: `${stem}（${r.matched} 命中 / ${r.no_bgm} 无 BGM）` })
    await refreshRows()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '匹配失败', variant: 'destructive', description: e?.message })
  } finally {
    matching.value = false
  }
}

// 模式切换：章节模式（llm / random）= 立即全量重匹配（assignments.mode 持久化在后端，
// 章节模式匹配会写 segment:false 把任何遗留段落级时间轴作废）；段落级模式不做
// 章节重匹配——只置模式并提示段落分析会自动生成时间轴。
async function switchMode(m: string) {
  if (m === mode.value || matching.value) return
  if (m === 'segment') {
    mode.value = 'segment'
    matchNote.value =
      '已切换为「LLM 段落级匹配」：完成段落分析后会自动生成时间轴。'
    return
  }
  pendingMode.value = m
  matching.value = true
  matchNote.value = `正在切换为「${m === 'random' ? '全章节随机' : 'LLM 标签匹配'}」并重匹配全部章节，请稍候…`
  // 先让选中态和处理中反馈完成一次绘制，再发起全量匹配请求。
  await nextTick()
  await waitForPaint()
  try {
    const r = await resolveDurable(await matchChapters(null, m)) as any
    mode.value = m
    pendingMode.value = null
    matchNote.value = `已切换为「${m === 'random' ? '全章节随机' : 'LLM 标签匹配'}」并重匹配：${r.matched} 章命中 · ${r.no_bgm} 章无 BGM${r.skipped_locked ? ` · ${r.skipped_locked} 章锁定跳过` : ''}`
    toast({ title: '重匹配完成', variant: 'success', description: matchNote.value })
    // 匹配接口已经完成持久化，音乐库没有变化，无需再次拉取整份库数据。
    await nextTick()
    await refreshRows({ reloadLibrary: false })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    pendingMode.value = null
    matchNote.value = '匹配失败，已保持原有匹配模式。'
    toast({ title: '匹配失败', variant: 'destructive', description: e?.message })
  } finally {
    pendingMode.value = null
    matching.value = false
  }
}

async function doLock(stem: string, locked: boolean) {
  try {
    await updateChapter(stem, { locked })
    toast({ title: locked ? '已锁定' : '已解锁', variant: 'default', description: stem })
    await refreshRows()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '操作失败', variant: 'destructive', description: e?.message })
  }
}

// ---------------------------------------------------------------------------
// 手动选曲弹层（enabled 曲目 + 试听 + 锁定 checkbox）
// ---------------------------------------------------------------------------
const manualStem = ref<string | null>(null)
const manualPick = ref<string | null>(null)
const manualLock = ref(false)
const manualBusy = ref(false)

function openManual(row: BgmRow) {
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
  if (!manualStem.value || manualBusy.value) return
  manualBusy.value = true
  try {
    await updateChapter(manualStem.value, {
      music: manualPick.value,
      locked: manualLock.value,
      __setMusic: true,
    })
    toast({ title: '已手动选曲', variant: 'success', description: manualPick.value || '清除（无 BGM）' })
    manualStem.value = null
    await refreshRows()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    manualBusy.value = false
  }
}

// ---------------------------------------------------------------------------
// 编辑标签弹层（四类分桶 + 自定义；写入 analysis，edited:true）
// ---------------------------------------------------------------------------
const editStem = ref<string | null>(null)
const editTags = reactive<TrackTags>({ scene: [], mood: [], emotion: [], custom: [] })
const editCustom = ref('')
const editBusy = ref(false)

function openEdit(row: BgmRow) {
  editStem.value = row.stem
  const src = row.data.analysis ?? row.data.assignment?.tags ?? { scene: [], mood: [], emotion: [], custom: [] }
  for (const c of ['scene', 'mood', 'emotion', 'custom'] as MusicTagCategory[]) {
    editTags[c] = [...(src[c] ?? [])]
  }
  editCustom.value = ''
}

function toggleEditTag(cat: MusicTagCategory, name: string) {
  const i = editTags[cat].indexOf(name)
  if (i >= 0) editTags[cat].splice(i, 1)
  else editTags[cat].push(name)
}

function addEditCustom() {
  const v = editCustom.value.trim()
  if (v && !editTags.custom.includes(v)) editTags.custom.push(v)
  editCustom.value = ''
}

async function saveEdit() {
  if (!editStem.value || editBusy.value) return
  editBusy.value = true
  try {
    await updateChapter(editStem.value, { tags: { ...editTags } })
    toast({ title: '标签已更新', variant: 'success', description: editStem.value })
    editStem.value = null
    await refreshRows()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    editBusy.value = false
  }
}

// ---------------------------------------------------------------------------
// 时间轴查看弹层（段落级章：GET /api/bgm/timeline/{stem} 的完整时间轴文件）
// ---------------------------------------------------------------------------
const timelineStem = ref<string | null>(null)
const timelineData = ref<BgmTimeline | null>(null)
const timelineBusy = ref(false)
async function openTimeline(row: BgmRow) {
  if (timelineBusy.value) return
  timelineStem.value = row.stem
  timelineData.value = null
  timelineBusy.value = true
  try {
    const r = await getTimeline(row.stem)
    timelineData.value = r.timeline
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '时间轴加载失败', variant: 'destructive', description: e?.message })
    timelineStem.value = null
  } finally {
    timelineBusy.value = false
  }
}

function fmtMs(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  return `${m}:${String(s % 60).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// 音频参数（config.bgm fire-and-forget 保存，分集页先例）
// ---------------------------------------------------------------------------
const draft = reactive({
  volume: 0.18,
  fade_in: 1.5,
  fade_out: 3.0,
  loop: true,
  min_match_score: 1,
  analysis_chars: 6000,
})
const paramsSaved = ref(false)
const paramsSaving = ref(false)

function seedDraft() {
  const b = settings.config?.bgm
  if (!b) return
  draft.volume = b.volume
  draft.fade_in = b.fade_in
  draft.fade_out = b.fade_out
  draft.loop = b.loop
  draft.min_match_score = b.min_match_score
  draft.analysis_chars = b.analysis_chars
}

async function saveParams() {
  if (paramsSaving.value) return
  paramsSaving.value = true
  try {
    await settings.save({ bgm: { ...draft } })
    paramsSaved.value = true
    setTimeout(() => (paramsSaved.value = false), 2000)
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    toast({ title: '保存失败', variant: 'destructive', description: e?.message })
  } finally {
    paramsSaving.value = false
  }
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
    if (!watcherArmed) {
      watcherArmed = true
      for (const t of taskStore.projectTasks) {
        if (BGM_MODULES.includes(t.module) && LABEL_TASK_TERMINAL_STATUSES.has(t.status)) processed.add(t.id)
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
      if (t.status === 'succeeded') {
        const stem = labelKeyOf(t.label)
        if (t.module === 'bgm-mix') {
          toast({ title: '混音完成', variant: 'success', description: stem })
        } else if (t.module === 'bgm-segment') {
          const r = t.result as { timeline?: boolean } | null
          toast({
            title: '段落分析完成',
            variant: 'success',
            description: r?.timeline ? `${stem}（已自动生成时间轴）` : `${stem}（时间轴未生成，请检查音频输入后重试段落分析）`,
          })
        } else {
          toast({ title: '章节分析完成', variant: 'success', description: `${stem}（已自动匹配）` })
        }
        scheduleRefresh()
      }
    }
  },
)

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  seedDraft()
  await taskStore.refresh()
  await refreshRows()
})

// keep-alive 缓存页：重新进入时刷新磁盘口径。
onActivated(() => {
  if (settings.loaded) void refreshRows()
})

const TAG_CATS: { key: MusicTagCategory; label: string; cls: string }[] = [
  { key: 'scene', label: '场景', cls: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30' },
  { key: 'mood', label: '气氛', cls: 'bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30' },
  { key: 'emotion', label: '情绪', cls: 'bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30' },
  { key: 'custom', label: '自定义', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30' },
]
</script>

<template>
  <div class="bgm-page space-y-5">
    <header class="page-header">
      <p class="eyebrow">Pipeline · BGM</p>
      <h1 class="page-title flex items-center gap-3">
        <Music4 class="h-7 w-7 text-primary" aria-hidden="true" />背景音乐
      </h1>
      <p class="page-description">
        为章节匹配背景音乐并完成混音。
      </p>
    </header>

    <ProjectGateAlert />

    <!-- 模式 -->
    <div class="bgm-control-card-shell" :class="{ 'is-processing': matching }" :aria-busy="matching">
      <Card class="bgm-control-card">
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <Shuffle class="h-5 w-5" />
          匹配模式
          <span v-if="matching" class="bgm-mode-status" role="status">
            <Loader2 class="h-3.5 w-3.5 animate-spin" />
            {{ loading ? '正在更新列表' : '正在重匹配' }}
          </span>
        </CardTitle>
        <CardDescription>
          选择匹配方式。段落级匹配需先完成段落分析。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <!-- 三个 radio 必须同 name 组：无 name 的原生 radio 互不排斥（全亮/全灭的根因）；
             选中态恒由 :checked 驱动（switchMode 是唯一写 mode 的地方）。 -->
        <div class="bgm-mode-options">
          <label class="bgm-mode-option" :class="{ 'is-selected': displayedMode === 'llm' }">
            <input type="radio" name="bgm-match-mode" :checked="displayedMode === 'llm'" :disabled="matching" class="h-4 w-4 accent-primary" @change="switchMode('llm')" />
            LLM 标签匹配
          </label>
          <label class="bgm-mode-option" :class="{ 'is-selected': displayedMode === 'random' }">
            <input type="radio" name="bgm-match-mode" :checked="displayedMode === 'random'" :disabled="matching" class="h-4 w-4 accent-primary" @change="switchMode('random')" />
            全章节随机
          </label>
          <label class="bgm-mode-option" :class="{ 'is-selected': displayedMode === 'segment' }">
            <input type="radio" name="bgm-match-mode" :checked="displayedMode === 'segment'" :disabled="matching" class="h-4 w-4 accent-primary" @change="switchMode('segment')" />
            LLM 段落级匹配
          </label>
        </div>
        <p v-if="matchNote" class="text-xs text-muted-foreground">{{ matchNote }}</p>
      </CardContent>
      </Card>
    </div>

    <!-- 音频参数 -->
    <Card class="bgm-control-card">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Music class="h-5 w-5" />音频参数</CardTitle>
        <CardDescription>
          设置音量、淡入淡出、循环和最低匹配分。
        </CardDescription>
      </CardHeader>
      <CardContent class="bgm-parameter-grid">
        <label class="bgm-field">
          BGM 音量（0~2）
          <input v-model.number="draft.volume" type="number" min="0" max="2" step="0.01"
            class="bgm-field__input" />
        </label>
        <label class="bgm-field">
          淡入（秒）
          <input v-model.number="draft.fade_in" type="number" min="0" step="0.1"
            class="bgm-field__input" />
        </label>
        <label class="bgm-field">
          淡出（秒）
          <input v-model.number="draft.fade_out" type="number" min="0" step="0.1"
            class="bgm-field__input" />
        </label>
        <label class="bgm-switch-field">
          循环铺满
          <Switch :model-value="draft.loop" @update:model-value="draft.loop = $event" />
        </label>
        <label class="bgm-field">
          匹配最低分
          <input v-model.number="draft.min_match_score" type="number" min="1" step="1"
            class="bgm-field__input" />
        </label>
        <label class="bgm-field">
          LLM 采样字数
          <input v-model.number="draft.analysis_chars" type="number" min="500" step="500"
            class="bgm-field__input" />
        </label>
        <Button variant="outline" size="sm" class="bgm-save-button" :disabled="paramsSaving" @click="saveParams">
          <Loader2 v-if="paramsSaving" class="h-3.5 w-3.5 animate-spin" />
          {{ paramsSaved ? '已保存' : '保存参数' }}
        </Button>
      </CardContent>
    </Card>

    <!-- 章节匹配结果 -->
    <Card class="bgm-results-card">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Music4 class="h-5 w-5" />章节匹配结果</CardTitle>
        <CardDescription>
          查看匹配和混音状态。可分别全选待解析和待混音章节。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="loadError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ loadError }}
        </Alert>

        <div v-else-if="rows.length" class="bgm-results-list">
          <!-- v-memo：任务流每次事件都会重算全局 rows computed，没有 memo 时整列表全量
               patch（待处理条目多时每次任务进度 tick 都重渲几百行）。key 覆盖行模板
               读到的全部响应式状态：行数据对象引用 + 本行任务 + 选中态 + 四个全局开关。
               数据刷新（chapterRows 整体替换）时 data 引用变化 → 该行必然重渲，不会漏更新。 -->
          <div
            v-for="row in rows"
            :key="row.stem"
            v-memo="[row.data, row.label, row.variant, row.task?.id, row.task?.status, row.task ? Math.round(row.task.progress) : -1, row.failedTask?.id, selected[row.stem] ? 1 : 0, mode, submitting, matching, projectSet]"
            class="bgm-row"
            :class="{ 'bgm-row--segment': mode === 'segment' }"
          >
            <div class="bgm-row__header">
              <label class="bgm-row__title">
                <input type="checkbox" class="h-4 w-4 shrink-0 accent-primary"
                  :checked="!!selected[row.stem]"
                  :disabled="submitting || !!row.task"
                  @change="onSelectChange(row.stem, $event)" />
                <span class="min-w-0 truncate text-sm font-semibold" :title="row.stem">{{ row.stem }}</span>
              </label>
              <div class="bgm-row__status">
                <Badge v-if="row.data.timeline && (row.data.assignment?.segment || mode === 'segment')" variant="secondary" class="shrink-0">
                  时间轴 · {{ row.data.timeline.sections }} 段
                </Badge>
                <Badge :variant="row.variant" class="shrink-0">{{ row.label }}</Badge>
              </div>
            </div>

              <!-- 段落模式的标签已用于生成时间轴，不在条目操作区重复展示。 -->
              <div v-if="mode !== 'segment'" class="bgm-row__tags">
                <template v-for="cat in TAG_CATS" :key="cat.key">
                  <Badge
                    v-for="t in rowTags(row)[cat.key] ?? []"
                    :key="cat.key + t"
                    variant="outline"
                    :class="cat.cls"
                  >{{ t }}</Badge>
                </template>
              </div>

              <div v-if="mode !== 'segment'" class="bgm-row__meta">
              <!-- BGM -->
              <div class="bgm-row__track">
                <template v-if="row.data.assignment?.music">
                  <span class="min-w-0 truncate text-xs" :class="{ 'text-destructive line-through': row.data.music_missing }"
                    :title="row.data.music_missing ? '曲目已从音乐库删除——重匹配可恢复' : row.data.assignment.music">
                    {{ row.data.assignment.music }}
                  </span>
                  <MiniAudioPlayer v-if="!row.data.music_missing" :src="musicPreviewUrl(row.data.assignment.music)" />
                  <Badge v-else variant="destructive">已删除</Badge>
                </template>
                <span v-else-if="row.data.assignment" class="text-xs text-muted-foreground">—（无 BGM）</span>
                <span v-else class="text-xs text-muted-foreground">—</span>
              </div>
              <!-- 匹配分（段落级章的音乐由时间轴逐段决定，无单分数） -->
              <span class="w-14 shrink-0 text-right text-xs tabular-nums text-muted-foreground"
                :title="row.data.assignment?.reason || ''">
                {{ row.data.assignment?.score ?? '—' }}
              </span>
              </div>

              <div v-if="row.data.mix_exists" class="bgm-row__player">
                <MiniAudioPlayer :src="bgmPreviewUrl(row.stem)" />
              </div>

              <!-- 操作 -->
              <div class="bgm-row__actions" :class="{ 'bgm-row__actions--segment': mode === 'segment' }">
                <template v-if="row.task">
                  <Progress :value="row.task.progress" class="h-1.5 w-20 sm:w-24" />
                  <span class="w-9 text-right text-xs tabular-nums text-muted-foreground">
                    {{ Math.round(row.task.progress * 100) }}%
                  </span>
                  <Button variant="outline" size="sm" @click="cancelRow(row.task)">
                    <XCircle class="h-3.5 w-3.5" />取消
                  </Button>
                </template>
                <template v-else>
                  <Button v-if="row.failedTask" variant="outline" size="sm" @click="retryRow(row.failedTask)">
                    <RefreshCw class="h-3.5 w-3.5" />重试
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :disabled="!projectSet || !row.mixable || submitting"
                    :class="mode === 'segment' ? 'order-3' : ''"
                    @click="doMixRow(row.stem)"
                    :title="!row.mixable
                      ? (row.data.narration_exists
                          ? (row.data.assignment?.segment || (row.data.segment_analysis && !row.data.segment_analysis.stale)
                              ? (row.data.segment_music_missing
                                  ? '时间轴曲目已删除——请重新进行段落分析'
                                  : (row.data.timeline ? '时间轴暂不可用——请刷新后重试' : '该章尚无时间轴——请先完成段落分析'))
                              : (row.data.music_missing ? '曲目已删除——请重匹配' : '该章从未匹配——请先匹配'))
                          : '06 旁白缺失——请先完成音频合并')
                      : '混音本章'"
                  >
                    {{ row.mixLabel }}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :class="mode === 'segment' ? 'order-1' : ''"
                    :disabled="!projectSet || submitting"
                    @click="mode === 'segment' ? doSegmentAnalyzeRow(row.stem) : doAnalyzeRow(row.stem)"
                  >
                    <Wand2 class="h-3.5 w-3.5" />{{ mode === 'segment' ? '段落分析' : '分析' }}
                  </Button>
                  <Button
                    v-if="mode !== 'segment'"
                    variant="outline"
                    size="sm"
                    :disabled="!projectSet || matching || mode === 'segment'"
                    :title="mode === 'segment' ? '段落级章的时间轴由段落分析自动生成' : ''"
                    @click="doRematch(row.stem)"
                  >
                    <Shuffle class="h-3.5 w-3.5" />重匹配
                  </Button>
                  <Button
                    v-if="mode !== 'segment'"
                    variant="outline"
                    size="sm"
                    :disabled="mode === 'segment'"
                    :title="mode === 'segment' ? '段落级章的曲目由时间轴逐段决定' : ''"
                    @click="openManual(row)"
                  >
                    <Music class="h-3.5 w-3.5" />选曲
                  </Button>
                  <Button
                   v-if="row.data.timeline && (row.data.assignment?.segment || mode === 'segment')"
                    variant="outline"
                    size="sm"
                    :class="mode === 'segment' ? 'order-2' : ''"
                    @click="openTimeline(row)"
                  >
                    <ListMusic class="h-3.5 w-3.5" />时间轴
                  </Button>
                  <Button v-if="mode !== 'segment'" variant="outline" size="sm" @click="openEdit(row)">
                    <Pencil class="h-3.5 w-3.5" />标签
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    :class="mode === 'segment' ? 'order-4' : ''"
                    :disabled="!projectSet"
                    @click="doLock(row.stem, !(row.data.assignment?.locked ?? false))"
                  >
                    <Lock v-if="row.data.assignment?.locked" class="h-3.5 w-3.5" />
                    <LockOpen v-else class="h-3.5 w-3.5" />
                    {{ row.data.assignment?.locked ? '解锁' : '锁定' }}
                  </Button>
                </template>
              </div>
            <p v-if="mode !== 'segment' && row.data.assignment?.reason" class="bgm-row__note text-xs text-muted-foreground">
              {{ row.data.assignment.reason }}
            </p>
            <p v-if="row.failedTask" class="bgm-row__note text-xs text-destructive">{{ row.failedTask.error || '任务失败' }}</p>
            <div v-if="row.task && row.task.status === 'running'" class="bgm-row__log">
              <LiveLogPanel :task="row.task" :max-height-class="'h-40'" />
            </div>
          </div>
        </div>
        <p v-else class="text-sm text-muted-foreground">
          暂无章节文本，请先到「排版与分册」完成分册。
        </p>

        <div class="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" :disabled="submitting || !pendingAnalysisStems.length" @click="selectPendingAnalysis">
            <ListChecks class="h-3.5 w-3.5" />全选待解析
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || !pendingMixStems.length" @click="selectPendingMix">
            <ListChecks class="h-3.5 w-3.5" />全选待混音
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || !rows.length" @click="clearSelection">
            <Eraser class="h-3.5 w-3.5" />清空
          </Button>
          <Button variant="outline" size="sm" :disabled="submitting || loading" @click="refreshRows">
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
          </Button>
          <span class="ml-auto text-xs text-muted-foreground">
            已选 {{ selectedNames.length }} / {{ rows.length }} 章
            <span v-if="matchedRows.length"> · 已匹配 {{ matchedRows.length }}</span>
            <span v-if="mixedCount"> · 已混音 {{ mixedCount }}</span>
          </span>
        </div>

        <div class="flex flex-wrap gap-2">
          <Button
            variant="outline"
            class="min-w-[11rem] flex-1"
            :disabled="!projectSet || submitting || !selectedNames.length"
            @click="mode === 'segment' ? doSegmentAnalyze() : doAnalyze()"
          >
            <Wand2 class="h-4 w-4" />
            {{ mode === 'segment' ? '段落分析所选' : '分析所选' }}（{{ selectedNames.length }} 章）
          </Button>
          <Button
            class="min-w-[11rem] flex-1"
            :disabled="!projectSet || submitting || !selectedNames.length"
            @click="doMix"
          >
            <Loader2 v-if="submitting" class="h-4 w-4 animate-spin" />
            <Music4 v-else class="h-4 w-4" />
            混音所选（{{ selectedNames.length }} 章）
          </Button>
          <Button
            variant="outline"
            class="min-w-[11rem] flex-1"
            :disabled="!projectSet || packaging || !selectedNames.length"
            :title="`打包下载已选的 ${selectedNames.length} 个章节`"
            :aria-busy="packaging"
            @click="handlePackageClick"
          >
            <Loader2 v-if="packaging" class="h-4 w-4 animate-spin" />
            <Package v-else class="h-4 w-4" />
            {{ packaging ? '打包中…' : `打包下载（${selectedNames.length} 章）` }}
          </Button>
          <Button v-if="bgmActive.length" variant="destructive" @click="cancelAll">
            <XCircle class="h-4 w-4" />取消全部
          </Button>
        </div>
      </CardContent>
      <CardFooter class="justify-start">
        <span class="text-xs text-muted-foreground">
          锁定章节不会参与重新匹配。
        </span>
      </CardFooter>
    </Card>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>

    <div
      v-if="manualStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="manualStem = null"
    >
      <div class="w-full max-w-lg rounded-xl border bg-background p-4 shadow-lg">
        <h2 class="text-lg font-semibold">手动选曲：{{ manualStem }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">
          选择曲目后，该章节将使用手动指定的音乐；锁定后不会被重新匹配。
        </p>
        <ScrollArea class="mt-3 h-72 rounded-md border">
          <div class="space-y-1 p-2">
            <label class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50">
              <input type="radio" :value="null" v-model="manualPick" class="h-4 w-4 accent-primary" />
              <span class="text-sm text-muted-foreground">无 BGM（混音时直接复制原声）</span>
            </label>
            <label
              v-for="tr in manualTracks"
              :key="tr.name"
              class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-accent/50"
            >
              <input type="radio" :value="tr.name" v-model="manualPick" class="h-4 w-4 accent-primary" />
              <span class="min-w-0 flex-1 truncate text-sm" :title="tr.name">{{ tr.name }}</span>
              <MiniAudioPlayer :src="musicPreviewUrl(tr.name)" />
            </label>
            <p v-if="!manualTracks.length" class="px-2 py-3 text-xs text-muted-foreground">
              音乐库中没有启用的曲目——请先到「音乐库」上传并启用。
            </p>
          </div>
        </ScrollArea>
        <label class="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="manualLock" class="h-4 w-4 accent-primary" />
          锁定（重匹配时整章跳过）
        </label>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="manualStem = null">关闭</Button>
          <Button size="sm" :disabled="manualBusy" @click="saveManual">
            <Loader2 v-if="manualBusy" class="h-3.5 w-3.5 animate-spin" />
            确认
          </Button>
        </div>
      </div>
    </div>

    <!-- 编辑标签弹层 -->
    <div
      v-if="editStem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="editStem = null"
    >
      <div class="w-full max-w-lg rounded-xl border bg-background p-4 shadow-lg">
        <h2 class="text-lg font-semibold">编辑章节标签：{{ editStem }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">
          修改标签后请重新匹配，让新标签生效。
        </p>
        <div class="mt-3 space-y-3">
          <div v-for="cat in TAG_CATS" :key="cat.key" class="space-y-1">
            <p class="text-xs font-medium text-muted-foreground">{{ cat.label }}</p>
            <div class="flex flex-wrap gap-1">
              <button
                v-for="t in (lib?.tags ?? {})[cat.key] ?? []"
                :key="t"
                type="button"
                class="rounded-md border px-2 py-0.5 text-xs transition-colors"
                :class="editTags[cat.key].includes(t)
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border text-muted-foreground hover:bg-accent/50'"
                @click="toggleEditTag(cat.key, t)"
              >{{ t }}</button>
              <button
                v-if="cat.key === 'custom'"
                type="button"
                class="rounded-md border border-dashed border-border px-2 py-0.5 text-xs text-muted-foreground hover:bg-accent/50"
                @click="addEditCustom"
              >+ {{ editCustom || '自定义标签' }}</button>
              <span v-if="!((lib?.tags ?? {})[cat.key] ?? []).length && !editTags[cat.key].length" class="text-xs text-muted-foreground">（空）</span>
            </div>
            <div v-if="cat.key === 'custom'" class="flex gap-2">
              <input
                v-model="editCustom"
                type="text"
                placeholder="输入自定义标签后点上方 + 添加"
                class="h-7 flex-1 rounded-md border border-input bg-background px-2 text-xs"
                @keyup.enter="addEditCustom"
              />
            </div>
          </div>
          <p v-if="editTags.custom.length" class="text-xs text-muted-foreground">
            已选自定义：{{ editTags.custom.join('、') }}
          </p>
        </div>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="editStem = null">关闭</Button>
          <Button size="sm" :disabled="editBusy" @click="saveEdit">
            <Loader2 v-if="editBusy" class="h-3.5 w-3.5 animate-spin" />
            保存
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
        class="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border bg-background shadow-2xl sm:max-h-[calc(100dvh-3rem)]"
        role="dialog"
        aria-modal="true"
        aria-labelledby="timeline-dialog-title"
      >
        <header class="border-b px-4 py-4 sm:px-6">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <p class="text-xs font-medium uppercase tracking-wide text-muted-foreground">段落级背景音乐</p>
              <h2 id="timeline-dialog-title" class="mt-1 break-words text-lg font-semibold sm:text-xl">BGM 时间轴：{{ timelineStem }}</h2>
            </div>
            <Button variant="outline" size="sm" class="shrink-0" @click="timelineStem = null">关闭</Button>
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
              <p class="mt-0.5 truncate font-medium" :title="timelineData.model">{{ timelineData.model }}</p>
            </div>
          </div>
          <p v-if="timelineData" class="mt-3 text-xs leading-5 text-muted-foreground">
            生成于 {{ timelineData.generated_at }} · 混音会按此时间轴逐段叠加 BGM，音量 = 基准 × 强度档位。
          </p>
        </header>

        <p v-if="!timelineData" class="px-6 py-8 text-sm text-muted-foreground">加载中…</p>
        <template v-else>
          <p v-if="timelineData.duration <= 0" class="border-b px-6 py-4 text-sm text-muted-foreground">全章无 BGM 段（混音 = 直接复制原声）。</p>

          <ScrollArea class="min-h-0 flex-1 bg-muted/20">
            <ol class="space-y-3 p-4 sm:p-6">
              <li v-for="(sp, i) in timelineData.timeline" :key="i" class="grid gap-3 sm:grid-cols-[7.5rem_minmax(0,1fr)]">
                <div class="flex items-start gap-2 text-xs sm:block">
                  <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground">{{ i + 1 }}</span>
                  <div class="pt-1 tabular-nums text-muted-foreground sm:mt-1 sm:pl-1">
                    <p>{{ fmtMs(sp.start) }} – {{ fmtMs(sp.end) }}</p>
                    <p class="mt-0.5">持续 {{ fmtMs(sp.end - sp.start) }}</p>
                  </div>
                </div>
                <article class="min-w-0 rounded-xl border bg-background p-3 shadow-sm sm:p-4">
                  <div class="flex flex-wrap items-center gap-2">
                    <p class="min-w-0 break-all text-sm font-semibold">{{ sp.music_id }}</p>
                    <MiniAudioPlayer :src="musicPreviewUrl(sp.music_id)" />
                    <span class="ml-auto rounded-md bg-accent px-2 py-1 text-xs tabular-nums text-muted-foreground">强度 {{ sp.intensity }}/3</span>
                    <span class="rounded-md bg-accent px-2 py-1 text-xs tabular-nums text-muted-foreground">音量 {{ sp.volume.toFixed(2) }}</span>
                  </div>
                  <p v-if="sp.scene_desc || sp.mood_desc" class="mt-3 text-sm leading-5">
                    <span v-if="sp.scene_desc">{{ sp.scene_desc }}</span>
                    <span v-if="sp.scene_desc && sp.mood_desc" class="text-muted-foreground"> · </span>
                    <span v-if="sp.mood_desc" class="text-muted-foreground">{{ sp.mood_desc }}</span>
                  </p>
                  <p v-if="sp.switch_reason || sp.reason" class="mt-2 break-words text-xs leading-5 text-muted-foreground">{{ sp.switch_reason || sp.reason }}</p>
                  <div v-if="TAG_CATS.some(cat => (sp.tags[cat.key] ?? []).length)" class="mt-3 flex flex-wrap gap-1.5">
                    <template v-for="cat in TAG_CATS" :key="cat.key">
                      <Badge
                        v-for="t in sp.tags[cat.key] ?? []"
                        :key="cat.key + t"
                        variant="outline"
                        :class="cat.cls"
                      >{{ t }}</Badge>
                    </template>
                  </div>
                </article>
              </li>
              <li v-if="!timelineData.timeline.length" class="rounded-xl border border-dashed bg-background px-4 py-8 text-center text-sm text-muted-foreground">
                此章尚未生成可播放的 BGM 段。
              </li>
            </ol>
          </ScrollArea>
        </template>
        <footer class="flex items-center justify-between gap-3 border-t bg-background px-4 py-3 text-xs text-muted-foreground sm:px-6">
          <span>可在上方试听每段使用的曲目</span>
          <Button variant="outline" size="sm" @click="timelineStem = null">关闭</Button>
        </footer>
      </section>
    </div>
  </div>
</template>
