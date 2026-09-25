<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { listDir } from '@/api/files'
import { batchStatusFiles, submitBatchReset, runBatch, ttsStatus } from '@/api/tts'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import type { BatchFileStatus, BatchResult, FileItem, TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Badge from '@/components/ui/Badge.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import { formatNumber } from '@/utils/format'
import {
  Layers,
  Loader2,
  XCircle,
  CheckCircle2,
  RefreshCw,
  ListChecks,
  Eraser,
  RotateCcw,
  ArrowRight,
} from 'lucide-vue-next'

const router = useRouter()
const pipeline = usePipelineStateStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

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

// The same filter WorkspaceEntryPicker applied: plain .json files, excluding the two-checks
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
  if (!projectSet.value) return
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
// value pipeline.activeScript (角色配音 reads it) mirrors the FIRST selected file —
// written only on user interaction, so a multi-select is never collapsed by its
// own sync; an external change (角色配音 picking a file) collapses the selection
// to that file (the previous cross-page replace semantics).
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
const allPendingSelected = computed(
  () => pendingRows.value.length > 0 && pendingRows.value.every((r) => selected[r.name]),
)
const allFilesSelected = computed(
  () => fileNames.value.length > 0 && fileNames.value.every((name) => selected[name]),
)

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

onMounted(async () => {
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await taskStore.refresh()
  reattachTask()
  void refreshRows()
})

// Rows refresh whenever the page (re)appears, and the live poll resumes only if the
// tracked run is still going. onActivated also fires on first mount (initial load);
// on first mount, onMounted performs the initial file refresh.
onActivated(() => {
  void refreshRows()
  reattachTask()
  const st = task.value?.status
  if (task.value && (st === 'pending' || st === 'running')) startStatusPolling()
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
  try {
    // Default (resume): synthesize only the not-yet-done segments, skipping existing audio
    // (fully-done files contribute no segments to the pool).
    const { task_id } = await runBatch({ scripts: names })
    taskId.value = task_id
    await taskStore.refresh()
    startStatusPolling()
    // Completion is handled by the watcher on task.status.
  } catch (e: any) {
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
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  if (!await showConfirm('重新合成会删除所选文件已生成的音频和进度记录，并从头重新制作全部段落（模型会重新加载，耗时较长）。确定继续吗？', { title: '重新合成', destructive: true })) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    // Clear the completion state (the package folders) first …
    const reset = await submitBatchReset(names)
    const resetTask = await waitForTask.wait(reset.task_id)
    if (resetTask.status !== 'succeeded') {
      throw new Error(resetTask.error_message || '重置合成包失败')
    }
    // … then the identical one-click run: default resume, nothing done → everything re-done.
    const { task_id } = await runBatch({ scripts: names })
    taskId.value = task_id
    await taskStore.refresh()
    startStatusPolling()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
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
</script>

<template>
  <div class="space-y-4">
    <header class="page-header mb-5">
      <div>
        <p class="eyebrow">Pipeline · TTS</p>
        <h1 class="page-title flex items-center gap-3">
          音频合成
          <StatusPill
            :label="(status?.ready ?? status?.implemented) ? '可用' : '引擎未就绪'"
            :tone="(status?.ready ?? status?.implemented) ? 'positive' : 'neutral'"
            :aria-label="(status?.ready ?? status?.implemented) ? '引擎可用' : '引擎未就绪'"
          />
        </h1>
        <p class="page-description">选择解析结果，合成角色台词音频。</p>
      </div>
    </header>

    <ProjectGateAlert />

    <Alert v-if="status && !(status.ready ?? status.implemented)" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <!-- 待合成（多选） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Layers class="h-5 w-5" />选择文件并合成</CardTitle>
          <CardDescription>选择解析文件并确定合成范围。已完成的文件可通过“全量全选”重新合成。</CardDescription>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="max-h-80 space-y-0.5 overflow-y-auto pr-1">
            <!-- Column header shares the rows' grid tracks (ROW_GRID) and sticks INSIDE the
                 scroll container: header and rows shrink by the same amount when the scrollbar
                 appears, so the columns can never drift apart. -->
            <div :class="[ROW_GRID, 'sticky top-0 z-10 bg-card py-1 text-xs text-muted-foreground']">
              <span />
              <span>解析文件</span>
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
                :disabled="!projectSet"
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
              暂无可合成的解析结果，请先到「文本解析」完成解析。
            </p>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" :disabled="busy || !projectSet || !pendingRows.length" title="选择全部尚未合成的文件" @click="selectPending">
              <ListChecks class="h-3.5 w-3.5" />{{ allPendingSelected ? '已选' : '全选' }}
            </Button>
            <Button variant="outline" size="sm" :disabled="busy || !projectSet || !fileNames.length" title="选择所有文件包括已合成文件" @click="selectAllFiles">
              <ListChecks class="h-3.5 w-3.5" />{{ allFilesSelected ? '已全选' : '全量全选' }}
            </Button>
            <Button variant="outline" size="sm" :disabled="busy || !selectedNames.length" @click="clearSelection">
              <Eraser class="h-3.5 w-3.5" />清空
            </Button>
            <Button variant="outline" size="sm" :disabled="filesLoading || !projectSet" @click="refreshRows">
              <RefreshCw class="h-3.5 w-3.5" :class="filesLoading && 'animate-spin'" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">已选 {{ selectedNames.length }} / {{ fileNames.length }} 个文件</span>
          </div>

          <Alert v-if="noSynthesizable" variant="default">
            没有可合成的段——请先到「文本解析」生成脚本。
          </Alert>
          <Alert v-if="warnNames.length" variant="warning">
            有 {{ warnNames.length }} 个角色尚未就绪声音（{{ warnNames.slice(0, 5).join('、') }}{{
              warnNames.length > 5 ? ' 等' : ''
            }}）——建议先到「角色配音」页一键生成，否则这些角色的段会失败。
          </Alert>

        </CardContent>
        <CardContent class="space-y-4 border-t pt-6">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="busy || !projectSet || !selectedNames.length" @click="doRun">
              <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
              <Layers v-else class="h-4 w-4" />
              {{ busy ? '合成中…' : '音频合成' }}
            </Button>
            <Button
              variant="outline"
              :disabled="busy || !projectSet || !selectedNames.length"
              title="清除已生成音频并重新合成"
              @click="doRunAll"
            >
              <RotateCcw class="h-4 w-4" />重新合成
            </Button>

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

  </div>
</template>
