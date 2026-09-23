<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listDir } from '@/api/files'
import { batchStatusFiles, listVoices, resetBatch, runBatch, runStressTest, ttsStatus } from '@/api/tts'
import { waitForDurableTask } from '@/api/persistentTasks'
import type { BatchFileStatus, BatchResult, FileItem, StressTestResult, TTSStatus, VoiceItem } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Badge from '@/components/ui/Badge.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import Input from '@/components/ui/Input.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { formatNumber } from '@/utils/format'
import {
  Layers,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  RotateCcw,
  ArrowRight,
  FlaskConical,
  X,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)

// ---------------------------------------------------------------------------
// 待合成 rows: the 03_parsed_json/ directory listing + each file's live stats
// (completed/total, speaker count, ready voices, the 已合成 flag) — one row per
// parsed JSON, so the numbers sit on the row instead of below a single pick.
// ---------------------------------------------------------------------------
const fileNames = ref<string[]>([])
const statuses = ref<BatchFileStatus[]>([])
const filesLoaded = ref(false)
const filesLoading = ref(false)

// The same filter DirPicker applied: plain .json files, excluding the two-checks
// shared product (<stem>_checked.json).
function matches(i: FileItem): boolean {
  if (i.is_dir) return false
  const low = i.name.toLowerCase()
  return low.endsWith('.json') && !low.endsWith('_checked.json')
}

// Grid tracks shared by the list header and every row:
// [checkbox | filename (flexible) | 已合成/总段落 | 角色 | 已就绪声音].
// The numeric columns get fixed, generous tracks (header text fits on one line, no
// wrapping); the filename absorbs the leftover width; the 已合成 badge sits INSIDE the
// filename cell so badge rows and plain rows keep their numeric columns in the exact
// same place (the old flex layout let the badge push them right on wide screens).
const ROW_GRID =
  'grid grid-cols-[1.25rem_minmax(0,1fr)_6.5rem_3.5rem_5rem] items-center gap-x-6 px-2'

/** A 待合成 row: the directory's file name + its live stats (zeros when stats are missing). */
interface FileRow {
  name: string
  total: number
  completed: number
  remaining: number
  complete: boolean
  speakers: number
  ready: number
  missing: string[]
  stale_speakers?: string[]
}

const rows = computed<FileRow[]>(() => {
  const byName = new Map(statuses.value.map((s) => [s.name, s]))
  return fileNames.value.map(
    (n) =>
      byName.get(n) ?? {
        name: n, total: 0, completed: 0, remaining: 0,
        complete: false, speakers: 0, ready: 0, missing: [],
      },
  )
})

/** Re-fetch ONLY the per-row stats (the poller's single request; the row list is untouched). */
async function refreshStatusesOnly() {
  if (!fileNames.value.length) return
  try {
    // `?? []` guards against a pre-upgrade backend (its /batch-status answer has no
    // `files` key) — the rows then show zeros instead of the computed crashing on render.
    statuses.value = (await batchStatusFiles(fileNames.value)).files ?? []
  } catch {
    /* backend down — keep the last known stats */
  }
}

async function refreshRows() {
  if (!workspaceSet.value) return
  filesLoading.value = true
  try {
    const r = await listDir('03_parsed_json')
    fileNames.value = r.items.filter(matches).map((i) => i.name)
    await refreshStatusesOnly()
  } catch {
    /* backend down — keep the last known rows */
  } finally {
    filesLoading.value = false
    filesLoaded.value = true
  }
}

// ---------------------------------------------------------------------------
// Selection: a local multi-select map (keyed by file name). The shared single
// value project.activeScript (角色配音 reads it) mirrors the FIRST selected file —
// written only on user interaction, so a multi-select is never collapsed by its
// own sync; an external change (角色配音 picking a file) collapses the selection
// to that file (the previous cross-page replace semantics).
// ---------------------------------------------------------------------------
const selected = reactive<Record<string, boolean>>({})
let lastSynced = project.activeScript
if (project.activeScript) selected[project.activeScript] = true

const selectedNames = computed(() => fileNames.value.filter((n) => selected[n]))

function syncScript() {
  const first = selectedNames.value[0] ?? ''
  project.activeScript = first
  lastSynced = first
}

function onRowChange(name: string, ev: Event) {
  if ((ev.target as HTMLInputElement).checked) selected[name] = true
  else delete selected[name]
  syncScript()
}

watch(
  () => project.activeScript,
  (v) => {
    if (v === lastSynced) return // our own sync — ignore
    for (const k of Object.keys(selected)) delete selected[k]
    if (v) selected[v] = true
    lastSynced = v
  },
)

// Select-all = every NOT-yet-complete file (a 已合成 row is never auto-checked);
// unchecking clears only the pending rows (a manually-checked complete row stays —
// it is still useful for 重新全部合成).
const pendingRows = computed(() => rows.value.filter((r) => !r.complete))
const allPendingSelected = computed(
  () => pendingRows.value.length > 0 && pendingRows.value.every((r) => selected[r.name]),
)
const somePendingSelected = computed(
  () => !allPendingSelected.value && pendingRows.value.some((r) => selected[r.name]),
)

function onSelectAllChange(ev: Event) {
  const on = (ev.target as HTMLInputElement).checked
  if (on) {
    for (const r of pendingRows.value) selected[r.name] = true
  } else {
    for (const r of pendingRows.value) delete selected[r.name]
  }
  syncScript()
}

// ---------------------------------------------------------------------------
// Warnings
// ---------------------------------------------------------------------------
const noSynthesizable = computed(
  () => filesLoaded.value && rows.value.length > 0 && rows.value.every((r) => r.total === 0),
)

/** Union of the not-ready speakers across the selected rows (all rows when nothing is selected). */
const warnNames = computed(() => {
  const scope = selectedNames.value.length ? rows.value.filter((r) => selected[r.name]) : rows.value
  const names = new Set<string>()
  for (const r of scope) for (const m of r.missing) names.add(m)
  return [...names]
})

// ---------------------------------------------------------------------------
// Run state
// ---------------------------------------------------------------------------
const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)
const result = ref<BatchResult | null>(null)

const task = computed(() => taskStore.tasks.find((t) => t.id === taskId.value) ?? null)

// 长度排序后按批内上限组批，模型在任务内仅加载一次。
const MIN_CONCURRENCY = 1
const MAX_CONCURRENCY = 128
const concurrency = ref(80)
const autoConcurrency = ref(false)

function clampConcurrency(n: number): number {
  const v = Math.trunc(n)
  if (!Number.isFinite(v)) return MIN_CONCURRENCY
  return Math.max(MIN_CONCURRENCY, Math.min(MAX_CONCURRENCY, v))
}

// Coerce the 批内段数 input (the Input component emits a string) to an integer in [1, 128].
function onConcurrency(v: string | number) {
  concurrency.value = clampConcurrency(Number(v))
}

function persistAutoConcurrency() {
  void settings.save({ tts: { batch_auto: autoConcurrency.value } })
}

// Reproducible seed (a debug aid; NOT persisted to config): empty → use the configured
// default (config.tts.batch_seed, default -1 = random); a number → this run is reproducible.
const seedInput = ref('')
function runSeed(): number | undefined {
  const t = seedInput.value.trim()
  if (t === '') return undefined
  const v = Math.trunc(Number(t))
  return Number.isFinite(v) ? v : undefined
}

// ---------------------------------------------------------------------------
// Live progress: while a run is in flight, re-fetch the per-row stats every 3 s
// (the manifests are written incrementally, so the numbers are real, never
// animated) and the 已合成 badge appears the moment a file finishes.
// ---------------------------------------------------------------------------
let statusTimer: ReturnType<typeof setInterval> | null = null

function startStatusPolling() {
  if (statusTimer) return
  void refreshStatusesOnly()
  statusTimer = setInterval(() => void refreshStatusesOnly(), 3000)
}

function stopStatusPolling(final = true) {
  if (statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
  if (final) void refreshStatusesOnly()
}

// ---------------------------------------------------------------------------
// Lifecycle (the page is keep-alive cached: onUnmounted does NOT fire on
// navigation, so the poller follows onActivated/onDeactivated)
// ---------------------------------------------------------------------------
// 刷新恢复：页面重载后本地 taskId 丢失，但后端合成任务仍在跑（store 的 refresh 已拉回全量
// 任务）。按 module 重新挂接在途任务（后端守卫保证至多一个在途）——每文件行由 refreshRows()
//（03_parsed_json 目录列表）自行恢复，这里只恢复任务级状态（日志面板 / 取消钮 / 完成 watcher）。
function reattachTask() {
  if (taskId.value) return
  const t = taskStore.activeTasks('tts-batch')[0]
  if (t) {
    taskId.value = t.id
    busy.value = true
    startStatusPolling()
  }
}

// 压测任务的挂接（F5 恢复）：后端至多一个在途压测任务；子窗口保持关闭，只恢复任务引用
// （压测按钮转为「压测中…」，重新打开子窗口即可看到日志 / 取消 / 结果）。
function reattachStressTask() {
  if (stressTaskId.value) return
  const t = taskStore.activeTasks('tts-stress')[0]
  if (t) stressTaskId.value = t.id
}

onMounted(async () => {
  // On a cold start settings may still be loading when onActivated runs (it fires right
  // after mounted, before this await resolves) — remember whether WE had to load it.
  const needLoad = !settings.loaded
  if (needLoad) await settings.load()
  // Initialize 批内段数 from the persisted config (falls back to the default of 80 if unavailable).
  concurrency.value = clampConcurrency(Number(settings.config?.tts?.batch_concurrency ?? 80))
  autoConcurrency.value = Boolean(settings.config?.tts?.batch_auto)
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await taskStore.refresh()
  reattachTask()
  reattachStressTask()
  if (needLoad) void refreshRows() // the initial onActivated run was skipped (no workspace yet)
})

// Rows refresh whenever the page (re)appears, and the live poll resumes only if the
// tracked run is still going. onActivated also fires on first mount (initial load);
// on a cold start (settings still loading) it defers to onMounted's post-load refresh.
onActivated(() => {
  if (settings.loaded) void refreshRows()
  reattachTask()
  reattachStressTask()
  const st = task.value?.status
  if (task.value && (st === 'pending' || st === 'running' || st === 'paused')) startStatusPolling()
})
onDeactivated(() => stopStatusPolling(false)) // hidden: no timer, no requests
onUnmounted(() => stopStatusPolling(false)) // last resort (keep-alive usually prevents this)

async function doRun() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  busy.value = true
  error.value = ''
  result.value = null
  const concurrencyNow = concurrency.value
  const autoNow = autoConcurrency.value
  try {
    // Default (resume): synthesize only the not-yet-done segments, skipping existing audio
    // (fully-done files contribute no segments to the pool).
    const { task_id } = await runBatch({
      scripts: names,
      concurrency: autoNow ? undefined : concurrencyNow,
      auto_concurrency: autoNow,
      seed: runSeed(),
    })
    taskId.value = task_id
    await taskStore.refresh()
    // Remember the chosen 批内段数 in the persisted config (fire-and-forget: the run above
    // already carries it; a save failure here must not fail the run that just started).
    void settings.save({ tts: { batch_concurrency: concurrencyNow, batch_auto: autoNow } })
    startStatusPolling()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

// 「重新全部合成」: step 1 deletes the selected files' synthesis packages
// (05_audio_chunk/<包>/ — the finished mp3s + manifest); step 2 then sends the EXACT same
// request as 一键音频合成 — with nothing left on disk the ordinary resume run re-does every
// segment (loads the model again). There is no separate force-re-synthesis route on the
// backend. Gated behind a confirm since it is expensive and discards the finished audio.
async function doRunAll() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  if (!window.confirm('重新全部合成会删除选中文件已合成的音频（05_audio_chunk/ 下对应文件夹，含进度清单），并从头重做全部段落（模型加载一次、耗时较长）。确定继续吗？')) return
  busy.value = true
  error.value = ''
  result.value = null
  const concurrencyNow = concurrency.value
  const autoNow = autoConcurrency.value
  try {
    // Clear the completion state (the package folders) first …
    const reset = await resetBatch(names)
    if ('task_id' in reset) {
      const resetTask = await waitForDurableTask(reset.task_id)
      if (resetTask.status !== 'succeeded') {
        throw new Error(resetTask.error_message || '重置合成包失败')
      }
    }
    // … then the identical one-click run: default resume, nothing done → everything re-done.
    const { task_id } = await runBatch({
      scripts: names,
      concurrency: autoNow ? undefined : concurrencyNow,
      auto_concurrency: autoNow,
      seed: runSeed(),
    })
    taskId.value = task_id
    await taskStore.refresh()
    void settings.save({ tts: { batch_concurrency: concurrencyNow, batch_auto: autoNow } })
    startStatusPolling()
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

function cancel() {
  if (task.value) taskStore.control(task.value.id, 'cancel')
}

watch(
  () => task.value?.status,
  (st) => {
    const t = task.value
    if (!st || !t) return
    if (st === 'succeeded') {
      result.value = t.result as BatchResult
      taskId.value = null
      busy.value = false
      stopStatusPolling()
      project.recordBatch(result.value)
      toast({
        title: '音频合成完成',
        variant: result.value?.failed.length ? 'default' : 'success',
        description: `成功 ${result.value?.completed ?? 0} / ${result.value?.total ?? 0} 段`,
      })
    } else if (st === 'failed') {
      error.value = t.error || '音频合成失败'
      taskId.value = null
      busy.value = false
      stopStatusPolling()
      toast({ title: '音频合成失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      taskId.value = null
      busy.value = false
      stopStatusPolling()
    }
  },
)

// ---------------------------------------------------------------------------
// 压测（临时测试入口）：子窗口 + 独立任务跟踪
// ---------------------------------------------------------------------------
// 机器自动生成的自然语句（不借助 LLM、不需要解析脚本）+ 任意一个已有克隆音色：固定批内
// 行数（--concurrency 上限），每行字数从起点每轮递增（步长可设），一轮一次引擎子进程，
// 无限跑到失败为止。失败 = 未达吞吐标准（1 秒必须出 10 个字：限时 = 处理量 / 10 秒，
// 计时自 worker「模型就绪」起，不含模型加载）/ 看门狗 124 / 引擎失败。逐轮报告（处理量 /
// 耗时 / 真实吞吐）写入工作空间 stress_test/ 目录；临时产物在 00_temp/ 每轮用完即删。
const stressOpen = ref(false)
const stressRows = ref(64)
const stressStartChars = ref(10)
const stressStepChars = ref(10)
const stressMaxRounds = ref('') // '' = 不限（跑到失败为止）
const stressSpeaker = ref('') // '' = 自动取第一个可用克隆音色
const cloneSpeakers = ref<VoiceItem[]>([])
const cloneSpeakersLoaded = ref(false)
const stressTaskId = ref<string | null>(null)
const stressResult = ref<StressTestResult | null>(null)
const stressError = ref('')

const stressTask = computed(() => taskStore.tasks.find((t) => t.id === stressTaskId.value) ?? null)
const stressActive = computed(() => {
  const st = stressTask.value?.status
  return st === 'pending' || st === 'running' || st === 'paused'
})

function clampStressRows(n: number): number {
  const v = Math.trunc(n)
  if (!Number.isFinite(v)) return 64
  return Math.max(1, Math.min(MAX_CONCURRENCY, v))
}

async function openStress() {
  stressOpen.value = true
  stressError.value = ''
  if (stressTaskId.value) return // a reattached / running task: just show it
  // 音色下拉：列出全部克隆音色（任意一个都可以压测）；自动档排在最前。
  if (!cloneSpeakersLoaded.value) {
    try {
      const r = await listVoices()
      cloneSpeakers.value = r.speakers.filter((s) => s.type === 'clone')
      cloneSpeakersLoaded.value = true
    } catch {
      cloneSpeakers.value = [] // backend down — auto pick still works server-side
    }
  }
}

function closeStress() {
  if (stressActive.value && !window.confirm('压测任务仍在进行。关闭窗口后任务会继续运行（重新打开可看日志 / 取消）。确定关闭？')) return
  stressOpen.value = false
}

async function doStressTest() {
  if (stressActive.value) return
  const start = Math.trunc(Number(stressStartChars.value))
  const step = Math.trunc(Number(stressStepChars.value))
  if (!Number.isFinite(start) || start < 1 || start > 2500) {
    stressError.value = '起始每行字数须为 1..2500 的整数。'
    return
  }
  if (!Number.isFinite(step) || step < 1) {
    stressError.value = '每轮递增须为 ≥ 1 的整数。'
    return
  }
  const maxRoundsRaw = stressMaxRounds.value.trim()
  let maxRounds: number | undefined
  if (maxRoundsRaw !== '') {
    maxRounds = Math.trunc(Number(maxRoundsRaw))
    if (!Number.isFinite(maxRounds) || maxRounds < 1) {
      stressError.value = '轮数上限须为 ≥ 1 的整数（留空 = 不限）。'
      return
    }
  }
  stressError.value = ''
  stressResult.value = null
  try {
    const { task_id } = await runStressTest({
      rows: clampStressRows(stressRows.value),
      start_chars: start,
      step_chars: step,
      max_rounds: maxRounds,
      speaker: stressSpeaker.value || undefined,
    })
    stressTaskId.value = task_id
    await taskStore.refresh()
  } catch (e: any) {
    stressError.value = e?.message || '启动压测失败'
  }
}

function cancelStress() {
  if (stressTask.value) taskStore.control(stressTask.value.id, 'cancel')
}

watch(
  () => stressTask.value?.status,
  (st) => {
    const t = stressTask.value
    if (!st || !t) return
    if (st === 'succeeded') {
      const res = t.result as StressTestResult
      stressResult.value = res
      stressTaskId.value = null
      toast({
        title: res.failed_at_chars ? '压测停止 · 找到边界' : '压测完成',
        variant: res.failed_at_chars ? 'default' : 'success',
        description: res.failed_at_chars
          ? `第 ${res.results.length} 轮（${res.rows} 行 × ${res.failed_at_chars} 字/行）未达标 → 边界 · 共 ${res.results.length} 轮 · 结果：${res.result_path.split(/[\\/]/).pop()}`
          : `共 ${res.results.length} 轮 · ${res.stopped_reason}`,
      })
    } else if (st === 'failed') {
      stressError.value = t.error || '压测失败'
      stressTaskId.value = null
      toast({ title: '压测失败', variant: 'destructive', description: stressError.value })
    } else if (st === 'cancelled') {
      stressTaskId.value = null
    }
  },
)
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        音频合成
        <StatusPill
          :label="status?.implemented ? '可用' : '引擎未就绪'"
          :tone="status?.implemented ? 'positive' : 'neutral'"
          :aria-label="status?.implemented ? '引擎可用' : '引擎未就绪'"
        />
      </h1>
      <p class="mt-1 text-muted-foreground">
        选择解析结果，合成角色台词音频。
      </p>
    </div>

    <WorkspaceGateAlert />

    <Alert v-if="status && !status.implemented" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <!-- 待合成（多选） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />待合成</CardTitle>
          <CardDescription>
            选择待合成的解析文件，可多选。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <!-- list header: select-all (excludes 已合成 rows) + selection count + refresh -->
          <div class="flex items-center gap-3 px-2">
            <input
              type="checkbox"
              class="h-4 w-4 shrink-0 accent-primary"
              :checked="allPendingSelected"
              :indeterminate="somePendingSelected"
              :disabled="!workspaceSet || !pendingRows.length"
              @change="onSelectAllChange"
            />
            <span class="text-sm text-muted-foreground">全选未合成文件</span>
            <span class="ml-auto text-xs text-muted-foreground">
              已选 {{ selectedNames.length }} / {{ fileNames.length }} 个文件
            </span>
            <Button variant="outline" size="sm" :disabled="filesLoading || !workspaceSet" @click="refreshRows">
              <RefreshCw class="h-3.5 w-3.5" :class="filesLoading && 'animate-spin'" />刷新
            </Button>
          </div>

          <div class="max-h-80 space-y-0.5 overflow-y-auto pr-1">
            <!-- Column header shares the rows' grid tracks (ROW_GRID) and sticks INSIDE the
                 scroll container: header and rows shrink by the same amount when the scrollbar
                 appears, so the columns can never drift apart. -->
            <div :class="[ROW_GRID, 'sticky top-0 z-10 bg-card py-1 text-xs text-muted-foreground']">
              <span />
              <span>文件（03_parsed_json/）</span>
              <span class="text-right">已合成 / 总段落</span>
              <span class="text-right">角色</span>
              <span class="text-right">已就绪声音</span>
            </div>
            <label
              v-for="r in rows"
              :key="r.name"
              :class="[
                ROW_GRID,
                'cursor-pointer rounded py-1.5 hover:bg-accent/50',
                { 'bg-accent/60': selected[r.name] },
              ]"
            >
              <input
                type="checkbox"
                class="h-4 w-4 accent-primary"
                :checked="!!selected[r.name]"
                :disabled="!workspaceSet"
                @change="onRowChange(r.name, $event)"
              />
              <!-- 已合成 badge lives inside the filename cell — it never displaces the numeric columns -->
              <span class="flex min-w-0 items-center gap-2 text-sm">
                <Badge v-if="r.complete" variant="success" class="shrink-0">已合成</Badge>
                <Badge
                  v-if="r.stale_speakers?.length"
                  variant="warning"
                  class="shrink-0"
                  :title="`角色声音已变更，将重新合成：${r.stale_speakers.join('、')}`"
                >
                  待重合成
                </Badge>
                <span class="min-w-0 truncate" :title="r.name">{{ r.name }}</span>
              </span>
              <span class="text-right text-xs tabular-nums text-muted-foreground">
                {{ r.completed }} / {{ r.total }}
              </span>
              <span class="text-right text-xs tabular-nums text-muted-foreground">
                {{ r.speakers }}
              </span>
              <span
                class="text-right text-xs tabular-nums"
                :class="r.ready < r.speakers ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'"
              >
                {{ r.ready }}
              </span>
            </label>
            <p v-if="filesLoaded && !rows.length" class="px-2 py-3 text-sm text-muted-foreground">
              03_parsed_json/ 里没有解析 JSON——请先到「文本解析」生成。
            </p>
          </div>

          <Alert v-if="noSynthesizable" variant="default">
            没有可合成的段——请先到「文本解析」生成脚本。
          </Alert>
          <Alert v-if="warnNames.length" variant="warning">
            有 {{ warnNames.length }} 个角色尚未就绪声音（{{ warnNames.slice(0, 5).join('、') }}{{
              warnNames.length > 5 ? ' 等' : ''
            }}）——建议先到「角色配音」页一键生成，否则这些角色的段会失败。
          </Alert>
          <p v-if="filesLoaded && rows.length && !selectedNames.length" class="text-xs text-muted-foreground">
            勾选要合成的文件（「全选未合成文件」= 全部未完成文件，不含已合成的）。
          </p>
        </CardContent>
      </Card>

      <!-- 操作 + 实时日志 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />一键音频合成</CardTitle>
          <CardDescription>
            合成所选文件的音频段；已完成的段会自动跳过。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || stressActive || !workspaceSet || !selectedNames.length" @click="doRun">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Layers v-else class="h-4 w-4" />
              {{ busy ? '合成中…' : `开始音频合成${selectedNames.length ? `（${selectedNames.length} 个文件）` : ''}` }}
            </Button>
            <Button
              variant="outline"
              :disabled="busy || stressActive || !workspaceSet || !selectedNames.length"
              title="清除已生成音频并重新合成"
              @click="doRunAll"
            >
              <RotateCcw class="h-4 w-4" />重新全部合成
            </Button>
            <Button
              variant="outline"
              :disabled="!workspaceSet || busy || stressActive"
              title="测试批处理上限"
              @click="openStress"
            >
              <Loader2 v-if="stressActive" class="h-4 w-4 animate-spin" />
              <FlaskConical v-else class="h-4 w-4" />
              {{ stressActive ? '压测中…' : '压测' }}
            </Button>
            <label class="flex items-center gap-2 text-sm text-muted-foreground">
              批内段数
              <Input
                :modelValue="concurrency"
                type="number"
                min="1"
                :max="MAX_CONCURRENCY"
                step="1"
                class="h-8 w-20"
                :disabled="busy || autoConcurrency"
                @update:modelValue="onConcurrency"
              />
            </label>
            <label class="flex items-center gap-2 text-sm text-muted-foreground" title="按同批最长文本字数向上匹配已测安全档位">
              <input
                v-model="autoConcurrency"
                type="checkbox"
                class="h-4 w-4 rounded border-input accent-primary"
                :disabled="busy"
                @change="persistAutoConcurrency"
              />
              自动
            </label>
            <label
              class="flex items-center gap-2 text-sm text-muted-foreground"
              title="留空 = 用「设置」里的默认（默认 -1 = 随机）；填数字 = 本次运行可复现。仅本次运行生效，不写入设置。"
            >
              seed
              <Input
                :modelValue="seedInput"
                type="number"
                step="1"
                placeholder="默认随机"
                class="h-8 w-24"
                :disabled="busy"
                @update:modelValue="seedInput = String($event)"
              />
            </label>
          </div>

          <!-- 进度指标（按钮下方）：已合成/总段数 · 已合成/总字数 —— 纯 SSE 驱动（后端每段
               完成经 「segments」 事件推送全量累计值、按 1 秒节流，每批段结束后即时刷新），
               无定时器。任务结束（taskId 清空）后隐藏，由结果卡接管。 -->
          <div v-if="task && (task.seg_total ?? 0) > 0" class="flex flex-wrap gap-3">
            <div class="min-w-[10rem] flex-1 rounded-md border bg-muted/30 px-3 py-2">
              <div
                class="text-xs text-muted-foreground"
                title="累计：运行前已完成的段 + 本次运行完成的段 / 全部可合成段"
              >已合成 / 总段数</div>
              <div class="mt-0.5 text-lg font-semibold tabular-nums">
                {{ formatNumber(task.seg_done) }} / {{ formatNumber(task.seg_total) }}
              </div>
            </div>
            <div class="min-w-[12rem] flex-1 rounded-md border bg-muted/30 px-3 py-2">
              <div
                class="text-xs text-muted-foreground"
                title="累计已合成段的字数 / 全部字数（字数 = 去除空白后的 Unicode 码点数）"
              >已合成 / 总字数</div>
              <div class="mt-0.5 text-lg font-semibold tabular-nums">
                {{ formatNumber(task.seg_chars_done) }} / {{ formatNumber(task.seg_chars_total) }}
              </div>
            </div>
          </div>

          <LiveLogPanel :task="task" :max-height-class="'h-96'">
            <template #actions>
              <Button v-if="task" variant="outline" size="sm" @click="cancel">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <!-- 结果 -->
          <div v-if="result" class="space-y-3">
            <Alert variant="default" class="items-center">
              <template #icon>
                <CheckCircle2 class="h-4 w-4 shrink-0 text-emerald-500" />
              </template>
              <span class="flex-1">
                本次成功 {{ result.completed }} / 共 {{ result.total }} 段
                <span v-if="result.failed.length" class="text-amber-600 dark:text-amber-400">
                  ，失败 {{ result.failed.length }}
                </span>
                <span v-if="result.done_count != null" class="text-xs text-muted-foreground">
                  · 累计已合成 {{ result.done_count }} / 全部 {{ result.all_count }} 段
                </span>
              </span>
              <Button v-if="(result.done_count ?? result.completed) > 0" size="sm" @click="router.push('/merge')">
                前往音频合并<ArrowRight class="h-4 w-4" />
              </Button>
            </Alert>

            <div v-if="result.files?.length" class="space-y-1.5">
              <div class="text-xs font-medium text-muted-foreground">各文件结果（{{ result.files.length }}）</div>
              <div class="max-h-56 space-y-1 overflow-y-auto rounded-md border p-2 text-xs">
                <div v-for="f in result.files" :key="f.script" class="flex items-start gap-2">
                  <span class="min-w-0 flex-1 truncate font-medium" :title="f.script">{{ f.script }}</span>
                  <span v-if="f.error" class="text-destructive">— {{ f.error }}</span>
                  <span v-else class="flex shrink-0 items-center gap-1 text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 class="h-3.5 w-3.5" />
                    {{ f.done_count }} / {{ f.all_count }} 段
                    <span v-if="f.failed > 0" class="text-amber-600 dark:text-amber-400">
                      · 失败 {{ f.failed }}
                    </span>
                  </span>
                </div>
              </div>
            </div>

            <div v-if="result.failed.length" class="space-y-1.5">
              <div class="text-xs font-medium text-muted-foreground">失败的段（{{ result.failed.length }}）</div>
              <div class="max-h-56 space-y-1 overflow-y-auto rounded-md border p-2 text-xs">
                <div v-for="(f, i) in result.failed" :key="i" class="flex items-start gap-2">
                  <span v-if="f.script" class="shrink-0 text-muted-foreground">{{ f.script }} · </span>
                  <span class="shrink-0 text-muted-foreground">第 {{ f.index + 1 }} 段</span>
                  <span class="shrink-0 font-medium">{{ f.speaker || '(未知)' }}</span>
                  <span class="text-destructive">— {{ f.reason }}</span>
                </div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>

    <!-- 压测子窗口（临时测试入口）：机器自动生成的自然语句（不借助 LLM）+ 任意一个已有克隆
         音色，逐「每行字数」档各跑一次引擎子进程，探测批内跑满 / 崩溃边界。产物全在
         00_temp/ 临时目录、每档用完即删。 -->
    <div
      v-if="stressOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeStress"
    >
      <div class="max-h-[90vh] w-full max-w-2xl space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="flex items-center gap-2 text-lg font-semibold">
              <FlaskConical class="h-5 w-5" />测试批处理上限
            </h2>
            <p class="mt-1 text-xs text-muted-foreground">
              逐步增加每行字数，测试当前批处理设置的上限。
            </p>
          </div>
          <Button variant="ghost" size="icon" :disabled="stressActive" @click="closeStress">
            <X class="h-4 w-4" />
          </Button>
        </div>

        <div class="flex flex-wrap items-center gap-x-5 gap-y-3 rounded-md border p-3 text-sm">
          <label class="flex items-center gap-2 text-muted-foreground">
            批内行数
            <Input
              :modelValue="stressRows"
              type="number"
              min="1"
              :max="MAX_CONCURRENCY"
              step="1"
              class="h-8 w-20"
              :disabled="stressActive"
              @update:modelValue="stressRows = clampStressRows(Number($event))"
            />
          </label>
          <label class="flex items-center gap-2 text-muted-foreground">
            起始每行字数
            <Input
              :modelValue="stressStartChars"
              type="number"
              min="1"
              max="2500"
              step="1"
              class="h-8 w-20"
              :disabled="stressActive"
              @update:modelValue="stressStartChars = Math.trunc(Number($event))"
            />
          </label>
          <label class="flex items-center gap-2 text-muted-foreground" title="每轮 = 上一轮 + 该值，逐轮跑下去直到失败">
            每轮递增
            <Input
              :modelValue="stressStepChars"
              type="number"
              min="1"
              step="1"
              class="h-8 w-20"
              :disabled="stressActive"
              @update:modelValue="stressStepChars = Math.trunc(Number($event))"
            />
          </label>
          <label class="flex items-center gap-2 text-muted-foreground" title="留空 = 不限（跑到失败为止）">
            轮数上限
            <Input
              v-model="stressMaxRounds"
              type="number"
              min="1"
              step="1"
              placeholder="不限"
              class="h-8 w-20"
              :disabled="stressActive"
            />
          </label>
          <label class="flex items-center gap-2 text-muted-foreground">
            音色
            <select
              v-model="stressSpeaker"
              class="h-8 max-w-44 rounded-md border bg-background px-2 text-sm"
              :disabled="stressActive"
            >
              <option value="">自动（第一个克隆音色）</option>
              <option v-for="s in cloneSpeakers" :key="s.name" :value="s.name">{{ s.name }}</option>
            </select>
          </label>
        </div>

        <Alert v-if="stressError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ stressError }}
        </Alert>

        <div class="flex items-center gap-2">
          <Button :disabled="stressActive || !workspaceSet" @click="doStressTest">
            <Loader2 v-if="stressActive" class="h-4 w-4 animate-spin" />
            <FlaskConical v-else class="h-4 w-4" />
            {{ stressActive ? '压测中…' : '开始压测' }}
          </Button>
          <Button v-if="stressTask" variant="outline" size="sm" @click="cancelStress">
            <XCircle class="h-3.5 w-3.5" />取消
          </Button>
        </div>

        <LiveLogPanel :task="stressTask" :max-height-class="'h-64'" />

        <!-- 结果：每轮一行（字数 | 处理量 | 成功/行数 | 耗时 | 真实吞吐 | 判定） -->
        <div v-if="stressResult" class="space-y-2">
          <div class="text-xs text-muted-foreground">
            压测结果 · 音色：{{ stressResult.speaker }} · 批内行数 {{ stressResult.rows }}
            · 标准 {{ stressResult.min_throughput_chars_per_sec }} 字/秒
            <span v-if="stressResult.seed >= 0">· seed {{ stressResult.seed }}</span>
            <span class="block">停止：{{ stressResult.stopped_reason }}</span>
            <span v-if="stressResult.failed_at_chars" class="block text-amber-600 dark:text-amber-400">
              边界：每行 {{ stressResult.failed_at_chars }} 字（{{ stressResult.rows }} × {{ stressResult.failed_at_chars }}
              = {{ stressResult.rows * stressResult.failed_at_chars }} 字）未达标
            </span>
            <span class="block">结果文件：{{ stressResult.result_path }}</span>
          </div>
          <div class="overflow-hidden rounded-md border">
            <table class="w-full text-xs">
              <thead class="bg-accent/40 text-muted-foreground">
                <tr>
                  <th class="px-2 py-1.5 text-right font-medium">轮</th>
                  <th class="px-2 py-1.5 text-right font-medium">每行字数</th>
                  <th class="px-2 py-1.5 text-right font-medium">处理量（字）</th>
                  <th class="px-2 py-1.5 text-right font-medium">成功 / 行数</th>
                  <th class="px-2 py-1.5 text-right font-medium">耗时</th>
                  <th class="px-2 py-1.5 text-right font-medium">吞吐（字/秒）</th>
                  <th class="px-2 py-1.5 text-left font-medium">判定</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in stressResult.results" :key="r.round" class="border-t">
                  <td class="px-2 py-1.5 text-right tabular-nums">{{ r.round }}</td>
                  <td class="px-2 py-1.5 text-right font-medium tabular-nums">{{ r.chars_per_line }} 字</td>
                  <td class="px-2 py-1.5 text-right tabular-nums">{{ r.total_chars }}</td>
                  <td class="px-2 py-1.5 text-right tabular-nums">{{ r.ok }} / {{ r.rows }}</td>
                  <td class="px-2 py-1.5 text-right tabular-nums">{{ r.synth_seconds === null ? '—' : `${r.synth_seconds}s` }}</td>
                  <td class="px-2 py-1.5 text-right tabular-nums">{{ r.throughput_chars_per_sec === null ? '—' : r.throughput_chars_per_sec }}</td>
                  <td class="px-2 py-1.5">
                    <span v-if="r.passed" class="text-emerald-600 dark:text-emerald-400">通过</span>
                    <span v-else class="text-destructive" :title="r.reason">
                      失败<span v-if="r.reason">（{{ r.reason }}）</span>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
