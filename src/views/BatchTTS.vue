<script setup lang="ts">
import {
  computed,
  onActivated,
  onDeactivated,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { useRouter } from 'vue-router'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { activeEntryLabel, latestEntryTasks } from '@/composables/useLabelDerivedTasks'
import { useWorkbenchRefresh } from '@/composables/useWorkbenchRefresh'
import { useWorkbenchTaskBatch } from '@/composables/useWorkbenchTaskBatch'
import { useWorkbenchScope, withinScope } from '@/composables/useWorkbenchScope'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { listDir } from '@/api/files'
import { batchStatusFiles, submitBatchReset, runBatch, ttsStatus, listVoices } from '@/api/tts'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import type { BatchFileStatus, BatchResult, FileItem, TTSStatus } from '@/types'

import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import WorkbenchActionBar from '@/components/WorkbenchActionBar.vue'
import ProductionWorkbench from '@/components/ProductionWorkbench.vue'
import { useSettingsStore } from '@/stores/settings'
import Progress from '@/components/ui/Progress.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Alert from '@/components/ui/Alert.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import { formatNumber } from '@/utils/format'
import { Layers, Loader2, RotateCcw, ArrowRight } from 'lucide-vue-next'

const router = useRouter()
const auth = useAuthStore()
const project = useProjectStore()
const pipeline = usePipelineStateStore()
const taskStore = useTaskStore()
const captureScope = useWorkbenchScope()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

const settings = useSettingsStore()
const status = ref<TTSStatus | null>(null)
const filesError = ref('')
let rowsRequest = 0
let statsRequest = 0

// ---------------------------------------------------------------------------
// 待合成 rows: the 03_parsed_json/ directory listing + each file's live stats
// (completed/total, speaker count, ready voices, the 已合成 flag) — one row per
// parsed JSON, so the numbers sit on the row instead of below a single pick.
// ---------------------------------------------------------------------------
const fileNames = ref<string[]>([])
const statuses = ref<BatchFileStatus[]>([])
const filesLoaded = ref(false)
const filesLoading = ref(false)
const voiceSummary = ref<{ total: number; ready: number } | null>(null)
let voiceSummaryRequest = 0

async function refreshVoiceSummary() {
  const isCurrent = captureScope()
  const request = ++voiceSummaryRequest
  if (!projectSet.value) {
    voiceSummary.value = null
    return
  }
  try {
    const response = await withinScope(listVoices('__all__'), isCurrent)
    if (request !== voiceSummaryRequest) return
    voiceSummary.value = {
      total: response.speakers.length,
      ready: response.speakers.filter((speaker) => speaker.status === 'ready').length,
    }
  } catch {
    if (isCurrent() && request === voiceSummaryRequest) voiceSummary.value = null
  }
}

// The same filter WorkspaceEntryPicker applied: plain .json files, excluding the two-checks
// shared product (<stem>_checked.json).
function matches(i: FileItem): boolean {
  if (i.is_dir) return false
  const low = i.name.toLowerCase()
  return low.endsWith('.json') && !low.endsWith('_checked.json')
}

/** A 待合成 row: the directory's file name + its live stats (zeros when stats are missing). */
interface FileRow {
  name: string
  display_name?: string
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
        name: n,
        total: 0,
        completed: 0,
        remaining: 0,
        complete: false,
        speakers: 0,
        ready: 0,
        missing: [],
      },
  )
})

/** Re-fetch ONLY the per-row stats (the poller's single request; the row list is untouched). */
async function refreshStatusesOnly() {
  void refreshVoiceSummary()
  const isCurrent = captureScope()

  if (!fileNames.value.length) return
  const request = ++statsRequest
  try {
    const response = await withinScope(batchStatusFiles([...fileNames.value]), isCurrent)
    if (request !== statsRequest) return
    statuses.value = response.files ?? []
    filesError.value = ''
  } catch (e: any) {
    if (!isCurrent()) return

    if (request === statsRequest) filesError.value = e?.message || '读取合成状态失败'
  }
}

async function refreshRows() {
  void refreshVoiceSummary()
  const isCurrent = captureScope()

  if (!projectSet.value) return
  const request = ++rowsRequest
  ++statsRequest
  filesLoading.value = true
  filesError.value = ''
  try {
    const directory = await withinScope(listDir('03_parsed_json'), isCurrent)
    if (request !== rowsRequest) return
    const names = directory.items.filter(matches).map((i) => i.name)
    const response = names.length
      ? await withinScope(batchStatusFiles(names), isCurrent)
      : { files: [] }
    if (request !== rowsRequest) return
    fileNames.value = names
    statuses.value = response.files ?? []
    for (const key of Object.keys(selected)) if (!names.includes(key)) delete selected[key]
    syncScript()
  } catch (e: any) {
    if (!isCurrent()) return

    if (request === rowsRequest) filesError.value = e?.message || '读取解析文件失败'
  } finally {
    if (isCurrent()) {
      if (request === rowsRequest) {
        filesLoading.value = false
        filesLoaded.value = true
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Selection: a local multi-select map (keyed by file name). The shared single
// value pipeline.activeScript mirrors the FIRST selected file — written only on
// user interaction, so a multi-select is never collapsed by its own sync; an
// external change collapses the selection to that file (the previous cross-page
// replace semantics). No page triggers such a change today: 角色配音's workbench
// no longer reads the shared value, and a store reset always rebuilds this
// keep-alive page instead — so the watch is a dormant cross-page safety net.
// ---------------------------------------------------------------------------
const selected = reactive<Record<string, boolean>>({})
let lastSynced = pipeline.activeScript
if (pipeline.activeScript) selected[pipeline.activeScript] = true

const selectedNames = computed(() => fileNames.value.filter((n) => selected[n]))

function syncScript() {
  const first = selectedNames.value[0] ?? ''
  pipeline.activeScript = first
  lastSynced = first
}

function onRowChange(name: string, ev: Event) {
  if ((ev.target as HTMLInputElement).checked) selected[name] = true
  else delete selected[name]
  syncScript()
}

watch(
  () => pipeline.activeScript,
  (v) => {
    if (v === lastSynced) return // our own sync — ignore
    for (const k of Object.keys(selected)) delete selected[k]
    if (v) selected[v] = true
    lastSynced = v
  },
)

// Select-all = every NOT-yet-complete file (a 已合成 row is never auto-checked);
// unchecking clears only the pending rows (a manually-checked complete row stays —
// it is still useful for 重新合成).
const pendingRows = computed(() => rows.value.filter((r) => !r.complete))

function selectPending() {
  for (const name of Object.keys(selected)) delete selected[name]
  for (const row of pendingRows.value) selected[row.name] = true
  syncScript()
}

function selectAllFiles() {
  for (const name of fileNames.value) selected[name] = true
  syncScript()
}

function clearSelection() {
  for (const name of Object.keys(selected)) delete selected[name]
  syncScript()
}

// ---------------------------------------------------------------------------
// Warnings
// ---------------------------------------------------------------------------
const noSynthesizable = computed(
  () => filesLoaded.value && rows.value.length > 0 && rows.value.every((r) => r.total === 0),
)

// ---------------------------------------------------------------------------
// Run state
// ---------------------------------------------------------------------------
const busy = ref(false)
const error = ref('')
const taskId = ref<string | null>(null)

const taskBatch = useWorkbenchTaskBatch(taskId)
const task = taskBatch.task
const tasksByFile = computed(() => latestEntryTasks([...taskStore.projectTasks, ...taskBatch.rows.value], 'tts-batch'))
const ACTIVE = new Set(['pending', 'queued', 'retrying', 'running', 'paused', 'cancelling'])

// 长度排序后按批内上限组批，模型在任务内仅加载一次。
// ---------------------------------------------------------------------------
// Live progress: while a run is in flight, re-fetch the per-row stats every 3 s
// (the manifests are written incrementally, so the numbers are real, never
// animated) and the 已合成 badge appears the moment a file finishes.
// ---------------------------------------------------------------------------
let statusTimer: ReturnType<typeof setInterval> | null = null
const statusRefresh = useWorkbenchRefresh(refreshStatusesOnly, 250)

function startStatusPolling() {
  if (statusTimer) return
  statusRefresh.schedule()
  statusTimer = setInterval(statusRefresh.schedule, 3000)
}

function stopStatusPolling(final = true) {
  if (statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
  statusRefresh.stop()
  if (final) statusRefresh.schedule()
}

watch([() => auth.user?.id, () => project.activeProjectId], () => {
  ++rowsRequest
  ++statsRequest
  ++voiceSummaryRequest
  stopStatusPolling(false)
  busy.value = false
  error.value = ''
  fileNames.value = []
  statuses.value = []
  voiceSummary.value = null
  filesLoaded.value = false
  filesLoading.value = false
  filesError.value = ''
  for (const name of Object.keys(selected)) delete selected[name]
}, { flush: 'sync' })

// ---------------------------------------------------------------------------
// Lifecycle (the page is keep-alive cached: onUnmounted does NOT fire on
// navigation, so the poller follows onActivated/onDeactivated)
// ---------------------------------------------------------------------------
// 刷新恢复：页面重载后本地 taskId 丢失，但后端合成任务仍在跑（store 的 refresh 已拉回全量
// 任务）。按 module 重新挂接全部在途章节任务——每文件行由 refreshRows()
//（03_parsed_json 目录列表）自行恢复，这里只恢复任务级状态（取消钮 / 完成 watcher）。
function reattachTask() {
  if (taskId.value) return
  const active = taskStore.activeTasks('tts-batch')
  const t = active[0]
  if (t) {
    taskBatch.track(active.map(row => row.id))
    busy.value = true
    startStatusPolling()
  }
}

watch(
  () => taskStore.projectTasks.filter(row => row.module === 'tts-batch').map(row => `${row.id}:${row.status}`).join('|'),
  () => {
    if (!captureScope()()) return
    reattachTask()
    if (filesLoaded.value) statusRefresh.schedule()
  },
)

onMounted(async () => {
  const isCurrent = captureScope()

  try {
    try {
      status.value = await withinScope(ttsStatus(), isCurrent)
    } catch {
      if (!isCurrent()) return

      status.value = { implemented: false, message: '后端未连接' }
    }
    await withinScope(taskStore.refresh(), isCurrent)
    reattachTask()
    void refreshRows()
  } catch (e: any) {
    if (!isCurrent() || e?.name === 'AbortError') return
    error.value = e?.message || '工作台初始化失败，请刷新重试。'
  }
})

// Rows refresh whenever the page (re)appears, and the live poll resumes only if the
// tracked run is still going. onActivated also fires on first mount (initial load);
// on first mount, onMounted performs the initial file refresh.
onActivated(() => {
  void refreshRows()
  if (task.value && ['failed', 'succeeded', 'cancelled'].includes(task.value.status))
    taskId.value = null
  if (!taskId.value) busy.value = false
  reattachTask()
  const st = task.value?.status
  if (task.value && (st === 'pending' || st === 'running')) startStatusPolling()
})
onDeactivated(() => stopStatusPolling(false)) // hidden: no timer, no requests
onUnmounted(() => stopStatusPolling(false)) // last resort (keep-alive usually prevents this)

async function doRun() {
  const isCurrent = captureScope()

  if (
    busy.value ||
    !projectSet.value ||
    filesLoading.value ||
    filesError.value ||
    !(status.value?.ready ?? status.value?.implemented)
  )
    return
  const names = selectedNames.value
  if (!names.length) return
  busy.value = true
  error.value = ''
  try {
    // Default (resume): synthesize only the not-yet-done segments, skipping existing audio
    // (fully-done files contribute no segments to the pool).
    const submitted = await withinScope(runBatch({ scripts: names }), isCurrent)
    taskBatch.track(submitted.task_ids)
    await withinScope(taskStore.refresh(), isCurrent)
    startStatusPolling()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

// 「重新合成」: step 1 queues deletion of the selected files' synthesis packages
// (05_audio_chunk/<包>/ — the finished mp3s + manifest); step 2 then sends the EXACT same
// request as 一键音频合成 — with nothing left on disk the ordinary resume run re-does every
// segment (loads the model again). There is no separate force-re-synthesis route on the
// backend. Gated behind a confirm since it is expensive and discards the finished audio.
async function doRunAll() {
  const isCurrent = captureScope()

  if (
    busy.value ||
    !projectSet.value ||
    filesLoading.value ||
    filesError.value ||
    !(status.value?.ready ?? status.value?.implemented)
  )
    return
  const names = selectedNames.value
  if (!names.length) return
  busy.value = true
  try {
    if (
      !(await withinScope(
        showConfirm(
          '重新合成会删除所选文件已生成的音频和进度记录，并从头重新制作全部段落（模型会重新加载，耗时较长）。确定继续吗？',
          { title: '重新合成', destructive: true },
        ),
        isCurrent,
      ))
    ) {
      busy.value = false
      return
    }
    error.value = ''
    // Clear the completion state (the package folders) first …
    const reset = await withinScope(submitBatchReset(names), isCurrent)
    const resetTask = await withinScope(waitForTask.wait(reset.task_id), isCurrent)
    if (resetTask.status !== 'succeeded') {
      throw new Error(resetTask.error_message || '重置合成包失败')
    }
    // … then the identical one-click run: default resume, nothing done → everything re-done.
    const submitted = await withinScope(runBatch({ scripts: names }), isCurrent)
    taskBatch.track(submitted.task_ids)
    await withinScope(taskStore.refresh(), isCurrent)
    startStatusPolling()
  } catch (e: any) {
    if (!isCurrent()) return

    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
    busy.value = false
  }
}

function cancel() {
  const isCurrent = captureScope()
  void taskBatch.cancel().catch(e => { if (isCurrent()) error.value = e?.message || '取消失败' })
}

watch(
  () => task.value?.status,
  (st) => {
    const t = task.value
    if (!st || !t) return
    if (st === 'succeeded') {
      const result = t.result as BatchResult | null
      taskId.value = null
      busy.value = false
      stopStatusPolling()
      toast({
        title: '音频合成完成',
        variant: result?.failed.length ? 'default' : 'success',
        description: `成功 ${result?.completed ?? 0} / ${result?.total ?? 0} 段`,
      })
    } else if (st === 'failed' || st === 'timeout') {
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

const workRows = computed(() => rows.value.map(row => {
  const entryTask = tasksByFile.value.get(row.name)
  const settled = entryTask?.status === 'running' && entryTask.progress === 100
    ? entryTask.current : ''
  const completed = row.complete || settled === '音频合成已完成'
  const partial = settled === '音频合成部分完成'
  const failed = entryTask?.status === 'failed' || entryTask?.status === 'timeout' || settled === '音频合成失败'
  const active = !!entryTask && ACTIVE.has(entryTask.status) && !failed && !partial && settled !== '音频合成已完成'
  return {
    ...row,
    workKey: row.name,
    workName: row.display_name || row.name.replace(/\.json$/i, ''),
    workState: active ? 'active' : failed ? 'failed' : partial ? 'pending' : completed ? 'done' : row.missing.length ? 'blocked' : row.stale_speakers?.length ? 'stale' : 'pending',
    statusLabel: active ? activeEntryLabel(entryTask!, '合成中') : failed ? '合成失败' : partial ? '部分完成' : completed ? '已完成' : row.missing.length ? '缺少声音' : row.stale_speakers?.length ? '待重合成' : '待合成',
    statusVariant: (failed ? 'destructive' : active || partial || row.missing.length ? 'warning' : completed ? 'success' : 'secondary') as 'destructive' | 'warning' | 'success' | 'secondary',
  }
}))
const scopeRows = computed(() => rows.value.filter((row) => selected[row.name]))
const selectedRemaining = computed(() =>
  scopeRows.value.reduce((sum, row) => sum + row.remaining, 0),
)
const selectedTotal = computed(() => scopeRows.value.reduce((sum, row) => sum + row.total, 0))
const engineReady = computed(() => !!(status.value?.ready ?? status.value?.implemented))
const failedTask = computed(
  () =>
    [...taskStore.projectTasks]
      .filter((t) => t.module === 'tts-batch' && t.status === 'failed')
      .sort((a, b) => b.seq - a.seq)[0],
)
const latestTask = computed(
  () =>
    task.value ??
    [...taskStore.projectTasks]
      .filter((t) => t.module === 'tts-batch')
      .sort((a, b) => b.seq - a.seq)[0] ??
    null,
)
function selectFiltered(names: string[]) {
  clearSelection()
  for (const name of names) selected[name] = true
  syncScript()
}
async function retryBatch() {
  const isCurrent = captureScope()

  if (!failedTask.value || busy.value) return
  const id = failedTask.value.id
  busy.value = true
  try {
    await withinScope(taskStore.control(id, 'retry'), isCurrent)
    taskId.value = id
    await withinScope(taskStore.refresh(), isCurrent)
    if (task.value?.status === 'failed') busy.value = false
    else startStatusPolling()
  } catch (e: any) {
    if (!isCurrent()) return
    busy.value = false
    error.value = e?.message || '重试失败'
  }
}
</script>

<template>
  <div class="viewport-workbench">
    <header class="page-header">
      <p class="eyebrow">Pipeline · TTS</p>
      <h1 class="page-title flex items-center gap-3">
        音频合成
      </h1>
      <p class="page-description">核对角色声音与合成范围，逐段制作并检查结果。</p>
    </header>
    <div class="workbench-controls" tabindex="0" role="region" aria-label="制作条件与流程">
      <ProjectGateAlert />
      <Alert v-if="status && !engineReady" variant="destructive">{{ status.message }}</Alert>
      <WorkbenchContextBar>
        <template #icon><Layers /></template>
        <template #title>01 条件核对 → 02 选择范围 → 03 合成与检查</template>
        <template #description>
          <p>增量补齐未完成或声音变更的段落；重新合成会清除所选音频。</p>
        </template>
        <template #metrics>
          <div class="workbench-context-metric" title="当前项目的角色总数（跨文件去重）">
            <strong>{{ voiceSummary?.total ?? '—' }}</strong>
            角色总数
          </div>
          <div class="workbench-context-metric" title="当前项目已有可用声音的角色数">
            <strong class="!text-emerald-600 dark:!text-emerald-400">{{ voiceSummary?.ready ?? '—' }}</strong>
            声音已就绪
          </div>
        </template>
        <template #actions><Button variant="outline" size="sm" @click="router.push('/voices')">核对角色声音</Button></template>
      </WorkbenchContextBar>
    </div>
    <ProductionWorkbench
      :rows="workRows"
      :selected="selected"
      :loading="filesLoading"
      :load-error="filesError"
      :disabled="busy || !projectSet"
      :filters="[
        { key: 'pending', label: '待合成' },
        { key: 'active', label: '处理中' },
        { key: 'failed', label: '合成失败' },
        { key: 'blocked', label: '声音未就绪' },
        { key: 'stale', label: '声音已变更' },
        { key: 'done', label: '已完成' },
      ]"
      :columns="[
        { label: '已合成 / 总段', width: '100px' },
        { label: '声音', width: '65px' },
        { label: '状态', width: '90px' },
      ]"
      label="合成"
      empty-text="暂无解析结果，请先完成文本解析。"
      @refresh="refreshRows"
      @select="onRowChange"
      @select-filtered="selectFiltered"
    >
      <template #selection
        ><Button
          variant="ghost"
          size="sm"
          :disabled="filesLoading || !!filesError || busy || !pendingRows.length"
          @click="selectPending"
          >选择待合成</Button
        ><Button variant="ghost" size="sm" :disabled="filesLoading || !!filesError || busy || !rows.length" @click="selectAllFiles"
          >选择全部文件</Button
        ><Button
          variant="ghost"
          size="sm"
          :disabled="filesLoading || !!filesError || busy || !selectedNames.length"
          @click="clearSelection"
          >清空</Button
        ></template
      >
      <template #empty
        ><RouterLink to="/script" class="mt-3 inline-block text-primary underline"
          >前往文本解析</RouterLink
        ></template
      >
      <template #cells="{ row }"
        ><td class="tabular-nums">
          <span>{{ row.completed }} / {{ row.total }}</span
          ><Progress :value="row.total ? row.completed / row.total : 0" class="mt-1 h-1" />
        </td>
        <td class="tabular-nums" :class="{ 'text-destructive': row.ready < row.speakers }">
          {{ row.ready }} / {{ row.speakers }}
        </td>
        <td>
          <WorkbenchStatus :variant="row.statusVariant">{{ row.statusLabel }}</WorkbenchStatus>
        </td></template
      >
      <template #detail="{ row }">
        <dl class="production-facts">
          <dt>已合成 / 总段落</dt>
          <dd>{{ row.completed }} / {{ row.total }}</dd>
          <dt>剩余段落</dt>
          <dd>{{ row.remaining }}</dd>
          <dt>就绪声音 / 角色</dt>
          <dd>{{ row.ready }} / {{ row.speakers }}</dd>
        </dl>
        <div class="production-section">
          <h3>合成条件</h3>
          <p v-if="!row.total" class="text-muted-foreground">没有可合成段落，请检查解析内容。</p>
          <p v-else-if="row.missing.length" class="break-words text-destructive">
            声音未就绪：{{ row.missing.join('、') }}。这些角色的段落会失败，请先制作角色声音。
          </p>
          <p v-else class="text-muted-foreground">角色声音已就绪，可进行增量合成。</p>
          <p v-if="row.stale_speakers?.length" class="mt-2 break-words">
            声音已变更：{{ row.stale_speakers.join('、') }}。增量合成会重做相关段落。
          </p>
        </div>
        <div class="production-section">
          <h3>执行参数</h3>
          <dl class="production-facts">
            <dt>批内并发上限</dt>
            <dd>
              {{
                settings.config?.tts.batch_auto
                  ? '自动'
                  : (settings.config?.tts.batch_concurrency ?? '未读取')
              }}
            </dd>
          </dl>
          <p class="mt-2 text-muted-foreground">
            沿用当前合成配置。任务按长度组批，模型在任务内加载一次。参数由管理员统一维护。
          </p>
        </div>
        <div class="production-section">
          <h3>关联批次任务</h3>
          <p class="text-muted-foreground">合成进度为批次总进度，不代表本文件单独的进度。</p>
          <template v-if="latestTask"
            ><p class="mt-2">
              {{ latestTask.label }} · {{ latestTask.status }} ·
              {{ Math.round(latestTask.progress * 100) }}%
            </p>
            <p v-if="latestTask.error" class="mt-2 break-words text-destructive">
              {{ latestTask.error }}
            </p>
            <p v-if="latestTask.seg_total" class="mt-2 tabular-nums">
              {{ formatNumber(latestTask.seg_done) }} / {{ formatNumber(latestTask.seg_total) }} 段
              · {{ formatNumber(latestTask.seg_chars_done) }} /
              {{ formatNumber(latestTask.seg_chars_total) }} 字
            </p></template
          >
          <p v-else class="mt-2 text-muted-foreground">暂无任务</p>
          <RouterLink to="/tasks" class="mt-2 inline-block text-primary underline"
            >查看任务记录</RouterLink
          >
        </div>
        <Button v-if="row.complete" variant="outline" size="sm" @click="router.push('/merge')"
          >检查与合并结果<ArrowRight class="h-3.5 w-3.5"
        /></Button>
      </template>
    </ProductionWorkbench>
    <WorkbenchActionBar>
      <template #summary>
        <strong>已选 {{ selectedNames.length }} 个文件</strong>
        <p class="mt-1 text-muted-foreground">
          增量处理 {{ selectedRemaining }} 段 · 全量重做 {{ selectedTotal }} 段
        </p>
      </template>
      <Button
        :disabled="busy || filesLoading || !projectSet || !engineReady || !!filesError || !selectedRemaining"
        @click="doRun"
        ><Loader2 v-if="busy" class="h-4 w-4 animate-spin" /><Layers v-else class="h-4 w-4" />{{
          busy ? '合成中…' : '增量合成'
        }}</Button
      ><Button
        variant="outline"
        :disabled="busy || filesLoading || !projectSet || !engineReady || !!filesError || !selectedTotal"
        title="清除所选音频并重新合成全部段落"
        @click="doRunAll"
        ><RotateCcw class="h-4 w-4" />重新合成所选</Button
      ><Button v-if="task" variant="destructive" @click="cancel">取消合成</Button
      ><Button
        v-else-if="failedTask"
        variant="outline"
        :disabled="busy || !projectSet || !engineReady"
        @click="retryBatch"
        >重试失败批次</Button
      >
    </WorkbenchActionBar>
    <div
      v-if="noSynthesizable || error"
      class="workbench-feedback"
      tabindex="0"
      role="region"
      aria-label="制作反馈"
    >
      <Alert v-if="noSynthesizable">没有可合成段落，请先完成文本解析。</Alert>
      <Alert v-if="error" variant="destructive">{{ error }}</Alert>
    </div>
  </div>
</template>
