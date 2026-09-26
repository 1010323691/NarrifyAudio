<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { cancelParseBatch, generateScriptFiles } from '@/api/script'
import { listDir } from '@/api/files'
import type { FileItem, TaskSnapshot } from '@/types'

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
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import LiveStreamPanel from '@/components/ui/LiveStreamPanel.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  FileText,
  ScanText,
  Loader2,
  XCircle,
  RefreshCw,
  ListChecks,
  Eraser,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()

// LLM / 生成参数 / Prompt 的配置编辑在「设置」页（含解析内检查的三个开关）；本页
// 只从 settings.config 读取已保存的模型名，不再本地编辑 / 保存。

// ---- File selection (02_split_text) + per-file parse jobs ------------------------
// The user checks one or more split .txt files; each becomes an independent backend
// Task (its own LLM request, status, success and JSON output). The frontend never
// reads file contents — it sends only file names; the backend does the discovery,
// the concurrency-bounded LLM calls, and the per-file JSON writes.
interface ParseFile extends FileItem {
  /** True when 03_parsed_json/<file-stem>.json already exists (already parsed). */
  done: boolean
}
const files = ref<ParseFile[]>([])
const filesLoading = ref(false)
const filesError = ref('')
const selected = reactive<Record<string, boolean>>({})

const selectedNames = computed(() => files.value.filter((f) => selected[f.name]).map((f) => f.name))
const doneCount = computed(() => files.value.filter((f) => f.done).length)
const allSelected = computed(() => files.value.length > 0 && files.value.every((f) => selected[f.name]))
const allPendingSelected = computed(() => {
  const pending = files.value.filter((f) => !f.done)
  return pending.length > 0 && pending.every((f) => selected[f.name])
})
const selectedDoneCount = computed(() => files.value.filter((f) => f.done && selected[f.name]).length)

const error = ref('')
const fileJobs = ref<{ name: string; taskId: string }[]>([])

function jobTask(taskId: string): TaskSnapshot | undefined {
  return taskStore.projectTasks.find((t) => t.id === taskId)
}

type JobState = {
  label: string
  variant: 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
}
function jobState(task: TaskSnapshot | undefined): JobState {
  if (!task) return { label: '待处理', variant: 'secondary' }
  switch (task.status) {
    case 'succeeded':
      return { label: '已完成', variant: 'success' }
    case 'failed':
      return { label: '失败', variant: 'destructive' }
    case 'cancelled':
      return { label: '已取消', variant: 'outline' }
    case 'running':
      // Still waiting on the concurrency gate reads as "queued", not actively working.
      // （后端排队文案含「排队」二字，勿改文案否则此判定失效。）
      if (task.phase === 'parse' || task.phase === 'check') return { label: '解析中', variant: 'default' }
      if (/排队/.test(task.current || '')) return { label: '排队', variant: 'secondary' }
      return { label: '解析中', variant: 'default' }
    case 'pending':
      // PENDING 壳：批次协调者尚未投放（有序投放 + 有限预取）。
      return { label: '排队', variant: 'secondary' }
    case 'paused':
      return { label: '等待 LLM 恢复', variant: 'warning' }
    default:
      return { label: '排队', variant: 'secondary' }
  }
}

interface JobRow {
  name: string
  taskId: string
  task: TaskSnapshot | undefined
  state: JobState
  /** Badge 文案：排队时带等待位置（1 基序号 / 批内总数，= 投放顺序）。 */
  stateText: string
  /** 1 基投放位置（fileJobs 下标 + 1；F5 恢复后按 task.seq 升序重建 = 原勾选序）。 */
  queueIndex: number
  queueTotal: number
  progress: number
  active: boolean
  error: string
}
const jobRows = computed<JobRow[]>(() =>
  fileJobs.value.map((j, idx) => {
    const task = jobTask(j.taskId)
    const status = task?.status
    const state = jobState(task)
    return {
      name: j.name,
      taskId: j.taskId,
      task,
      state,
      stateText: state.label === '排队' ? `排队 ${idx + 1}/${fileJobs.value.length}` : state.label,
      queueIndex: idx + 1,
      queueTotal: fileJobs.value.length,
      progress: task?.progress ?? 0,
      active: status === 'pending' || status === 'running' || status === 'paused',
      error: task?.error || '',
    }
  }),
)

const jobRowByName = computed(() => new Map(jobRows.value.map((r) => [r.name, r])))

/** 选择区渲染行 = 磁盘文件 + 本批进度行（若在该批中）。文件行即进度行：
 *  批次内文件直接在此显示状态 / 速度 / 进度条 / 取消·重试。 */
const fileRows = computed(() =>
  files.value.map((f) => ({ file: f, job: jobRowByName.value.get(f.name) })),
)

/** 行内进度条指示色：完成=emerald、失败=destructive、取消=muted、其余（排队/处理中）=primary。 */
function progressIndicator(row: JobRow): string {
  switch (row.state.label) {
    case '已完成': return 'bg-emerald-500'
    case '失败': return 'bg-destructive'
    case '已取消': return 'bg-muted-foreground/40'
    default: return 'bg-primary'
  }
}

// "In flight" while any job's task hasn't reached a terminal state (drives the button).
const busy = computed(() => jobRows.value.some((r) => r.active))

// 解析日志区显隐（设置页「解析日志显示」，默认关）：开 = 显示「解析进度」Card
// （每文件实时日志 + 流式反馈，指标在 Card 内）；关 = 整个 Card 隐藏、指标移到
// 「开始处理」按钮下方。保存设置后立即生效（settings.config 是响应式的）。
const showParseLogs = computed(() => settings.config?.ui.show_parse_logs ?? false)

// ---- LLM 性能指标：吞吐量 / 处理速度 --------------------------------------------
// 吞吐量 (字/s): 各运行中窗口「近 10 秒平均」生成速率之和。每个任务的 10 秒窗口速率
// (task.llm_cps_10s) 由后端按真实流式字符算出（近 10 秒生成字符 ÷ 对应秒数）并经 SSE 实时推送；
// 前端只把它们相加（同一时间窗口的速率可加：各运行窗口之和 = 总体近 10 秒平均）。用 computed
// 跟随 SSE 事件重算，无需定时器；无运行中窗口时自然为 0，段间 / 排队窗口随时间在后端衰减。
const totalTps = computed(() => {
  let sum = 0
  for (const r of jobRows.value) if (r.task?.status === 'running') sum += r.task.llm_cps_10s ?? 0
  return sum
})

// 一批解析结束后自动刷新文件列表：把刚生成 JSON 的文件标记为「已完成」（既有勾选保留）。
watch(busy, (b, was) => {
  if (was && !b) loadFiles()
})

// 处理速度 (字/s): Σ(已完成各段原始字符数) ÷ Σ(到各段为止的累计处理耗时)。
//   分子 / 分母都由后端在每段完成后一并上报（llm_chars / llm_secs）——耗时冻结于"该段完成"
//   的时刻而非实时时钟。因此本指标只在"某段刚完成"时更新一次，段与段之间保持恒定，
//   不会像用实时时钟做分母那样在等待下一段时持续走低。真实字符，非 token。
// A computed, so it recomputes exactly when a chunk-completion event lands (never on a
// timer) — per-segment updates with no decay in between.
const speedCps = computed(() => {
  let chars = 0
  let secs = 0
  for (const r of jobRows.value) {
    chars += r.task?.llm_chars ?? 0  // 各任务已完成各段的累计原始字符数
    secs += r.task?.llm_secs ?? 0    // 各任务到已完成各段为止的累计处理耗时
  }
  return secs > 0 ? chars / secs : 0
})

// 指标的统一数据源（日志区开 = 「解析进度」Card 顶部；关 = 「开始处理」按钮下方紧凑卡）。
const metrics = computed(() => [
  {
    label: '吞吐量（字/s）',
    value: String(Math.round(totalTps.value)),
    title: '近 10 秒平均：各运行中窗口「近 10 秒生成字符 ÷ 对应秒数」之和（后端按真实流式字符计算，平滑不抖动）',
  },
  {
    label: '处理速度（字/s）',
    value: String(Math.round(speedCps.value)),
    title: '累计平均：Σ已完成源字符 ÷ Σ累计处理耗时（自批次开始；每完成一段刷新、段间恒定）',
  },
])

// 刷新恢复：页面重载后 fileJobs 为空，但后端任务仍在跑（store 的 refresh 已拉回全量任务）。
// 按 module + label 重新挂接非终态解析任务——label 形如「文本解析（{文件名}）」（全角括号，
// 后端 api/script.py 的 label 格式，勿改）。恢复顺序按 task.seq（后端单调创建序）升序重建 =
// 原勾选/投放顺序，排队位置 N/总 因此与刷新前一致。批次注册表在进程内存、重启即失，恢复不依赖它。
function reattachJobs() {
  const pending: { name: string; taskId: string; seq: number }[] = []
  for (const t of taskStore.activeTasks('script')) {
    if (fileJobs.value.some((j) => j.taskId === t.id)) continue
    const m = t.label.match(/文本解析（(.+)）$/)
    if (m) pending.push({ name: m[1], taskId: t.id, seq: t.seq })
  }
  pending.sort((a, b) => a.seq - b.seq)
  for (const p of pending) fileJobs.value.push({ name: p.name, taskId: p.taskId })
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  await loadFiles()
  await taskStore.refresh()
  reattachJobs()
})

async function loadFiles() {
  if (!projectSet.value) {
    files.value = []
    return
  }
  filesLoading.value = true
  filesError.value = ''
  try {
    const r = await listDir('02_split_text', true)
    // 已生成判断：03_parsed_json/ 下是否已有 <文件基名>.json（后端对缺失目录返回空列表）。
    const out = await listDir('03_parsed_json', true).catch(() => null)
    const outNames = new Set((out?.items ?? []).map((i) => i.name))
    const txts: ParseFile[] = r.items
      .filter((i) => !i.is_dir && i.name.toLowerCase().endsWith('.txt'))
      .map((f) => {
        const stem = f.name.replace(/\.[^.]+$/, '')
        return {
          ...f,
          done: outNames.has(stem + '.json'),
        }
      })
    files.value = txts
    // 默认全不选：只保留仍存在于磁盘的既有勾选（已完成文件同样可勾选，用于重新解析 / 跑其他流程），
    // 剔除已消失的文件。
    const names = new Set(txts.map((t) => t.name))
    for (const k of Object.keys(selected)) if (!names.has(k)) delete selected[k]
  } catch (e: any) {
    filesError.value = e?.message || '读取文件列表失败'
  } finally {
    filesLoading.value = false
  }
}

function selectAllPending() {
  // 【全选】= 只勾未完成的条目（先清空，避免残留已完成的勾选；幂等）。
  clearAll()
  files.value.forEach((f) => {
    if (!f.done) selected[f.name] = true
  })
}
function selectAllIncludingDone() {
  // 【全量全选】= 无视状态全勾（含已完成）；执行时按实际勾选原样提交，不做状态二次过滤。
  files.value.forEach((f) => {
    selected[f.name] = true
  })
}
function clearAll() {
  files.value.forEach((f) => {
    delete selected[f.name]
  })
}
function onFileChange(name: string, ev: Event) {
  if ((ev.target as HTMLInputElement).checked) selected[name] = true
  else delete selected[name]
}

async function startParse() {
  if (busy.value) return
  const names = selectedNames.value
  if (!names.length) return
  if (!(settings.config?.llm.model_name || '').trim()) {
    error.value = '请先填写 LLM 模型名称（模型不能为空）。'
    return
  }
  error.value = ''
  try {
    const r = await generateScriptFiles(names)
    fileJobs.value = r.files.map((f) => ({ name: f.file, taskId: f.task_id }))
    await taskStore.refresh()
  } catch (e: any) {
    error.value = e?.message || '启动解析失败'
  }
}

function cancelJob(task: TaskSnapshot | undefined) {
  if (task) taskStore.control(task.id, 'cancel')
}

function retryJob(task: TaskSnapshot | undefined) {
  if (task) taskStore.control(task.id, 'retry')
}

/** 【取消全部】：一次取消本批所有排队 + 在跑任务，并停止批次继续投放后续文件。
 *  走专用端点（单个任务 cancel 表达不了「停止投放」，否则协调者会继续把剩余文件投出去）；
 *  端点失败时回退为逐任务 cancel（投放停止缺失但取消本身可达）。 */
async function cancelAll() {
  const ids = jobRows.value.filter((r) => r.active).map((r) => r.taskId)
  if (!ids.length) return
  try {
    await cancelParseBatch(ids)
  } catch {
    for (const id of ids) taskStore.control(id, 'cancel')
  }
  await taskStore.refresh()
}

</script>

<template>
  <div class="space-y-4">
    <header class="page-header mb-5">
      <div>
        <p class="eyebrow">Pipeline · LLM</p>
        <h1 class="page-title flex items-center gap-3"><ScanText class="h-6 w-6" />文本解析</h1>
        <p class="page-description">选择分册文本，生成角色和台词数据。</p>
      </div>
    </header>

    <ProjectGateAlert />

    <!-- 选择待解析文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><FileText class="h-5 w-5" />选择待解析文件</CardTitle>
        <CardDescription>
          选择要处理的分册文本，可多选；已完成的文件也可以重新解析。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="filesError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ filesError }}
        </Alert>

        <div
          v-else-if="files.length"
          class="max-h-80 space-y-1 overflow-y-auto rounded-md border p-2"
        >
          <!-- 文件行 = 进度行：本批文件直接在此显示 状态（含排队位置）/ 速度 / 进度条 /
               取消·重试（行内按钮不包在 <label> 里，避免点按钮连带切换勾选）。 -->
          <div
            v-for="row in fileRows"
            :key="row.file.name"
            class="rounded px-2 py-1.5 hover:bg-accent/50"
          >
            <div class="flex items-center gap-3">
              <label class="flex min-w-0 flex-1 cursor-pointer items-center gap-3">
                <input
                  type="checkbox"
                  class="h-4 w-4 shrink-0 accent-primary"
                  :checked="!!selected[row.file.name]"
                  :disabled="busy"
                  @change="onFileChange(row.file.name, $event)"
                />
                <span class="min-w-0 truncate text-sm" :title="row.file.name">{{ row.file.name }}</span>
              </label>
              <template v-if="row.job">
                <Badge :variant="row.job.state.variant" class="shrink-0">{{ row.job.stateText }}</Badge>
                <span
                  class="shrink-0 text-xs tabular-nums"
                  :class="row.job.state.label === '解析中' ? 'text-primary' : 'text-muted-foreground'"
                  title="近 10 秒平均速度"
                >{{ row.job.state.label === '解析中' ? `${Math.round(row.job.task?.llm_cps_10s ?? 0)} 字/s` : '—' }}</span>
                <Progress
                  :value="row.job.progress"
                  :indicator-class="progressIndicator(row.job)"
                  class="h-1.5 w-24 shrink-0 sm:w-32"
                />
                <span class="w-10 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                  {{ Math.round(row.job.progress * 100) }}%
                </span>
                <Button
                  v-if="row.job.active"
                  variant="outline"
                  size="sm"
                  class="shrink-0"
                  @click="cancelJob(row.job.task)"
                >
                  <XCircle class="h-3.5 w-3.5" />取消
                </Button>
                <Button
                  v-else-if="row.job.state.label === '失败'"
                  variant="outline"
                  size="sm"
                  class="shrink-0"
                  @click="retryJob(row.job.task)"
                >
                  <RefreshCw class="h-3.5 w-3.5" />重试
                </Button>
              </template>
              <Badge v-else-if="row.file.done" variant="success" class="shrink-0">已完成</Badge>
            </div>
            <p v-if="row.job && row.job.state.label === '失败'" class="mt-1 pl-7 text-xs text-destructive">
              {{ row.job.error || '解析失败' }}
            </p>
          </div>
        </div>
        <p v-else class="text-sm text-muted-foreground">
          暂无待解析的章节文本，请先到「排版与分册」生成章节。
        </p>

        <div class="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" :disabled="busy || !files.length" @click="selectAllPending">
            <ListChecks class="h-3.5 w-3.5" />{{ allPendingSelected ? '已选' : '全选' }}
          </Button>
          <Button variant="outline" size="sm" :disabled="busy || !files.length" @click="selectAllIncludingDone">
            <ListChecks class="h-3.5 w-3.5" />{{ allSelected ? '已全选' : '全量全选' }}
          </Button>
          <Button variant="outline" size="sm" :disabled="busy || !files.length" @click="clearAll">
            <Eraser class="h-3.5 w-3.5" />清空
          </Button>
          <Button variant="outline" size="sm" :disabled="busy || filesLoading" @click="loadFiles">
            <RefreshCw class="h-3.5 w-3.5" :class="filesLoading ? 'animate-spin' : ''" />刷新
          </Button>
          <span class="ml-auto text-xs text-muted-foreground">
            已选 {{ selectedNames.length }} / {{ files.length }} 个
            <span v-if="doneCount"> · 已完成 {{ doneCount }} 个</span>
            <span v-if="selectedDoneCount"> · 含已完成 {{ selectedDoneCount }}</span>
            · 后台 Worker 按部署容量处理，等待中的任务会排队
          </span>
        </div>

        <div class="flex flex-wrap gap-2">
          <Button class="min-w-[10rem] flex-1" :disabled="!selectedNames.length || !projectSet || busy" @click="startParse">
            <Loader2 v-if="busy" class="h-4 w-4 animate-spin" />
            <ScanText v-else class="h-4 w-4" />
            {{ busy ? '处理中…' : '开始处理' }}
          </Button>
          <Button v-if="busy" variant="destructive" @click="cancelAll">
            <XCircle class="h-4 w-4" />取消全部
          </Button>
        </div>

        <!-- 性能指标（日志区关闭时显示在按钮下方；开启时在「解析进度」Card 内） -->
        <div v-if="!showParseLogs && fileJobs.length" class="flex flex-wrap gap-3">
          <div
            v-for="m in metrics"
            :key="m.label"
            class="min-w-[7rem] flex-1 rounded-md border bg-muted/30 px-3 py-2"
          >
            <div class="text-xs text-muted-foreground" :title="m.title">{{ m.label }}</div>
            <div class="mt-0.5 text-lg font-semibold tabular-nums">{{ m.value }}</div>
          </div>
        </div>
      </CardContent>
      <CardFooter>
        <span class="text-xs text-muted-foreground">
          解析结果会保存在当前项目中，可在后续制作步骤中继续使用。
        </span>
      </CardFooter>
    </Card>

    <!-- 解析进度（每文件一行）：仅「解析日志显示」开启时渲染整个 Card
         （实时日志 + 流式反馈 + 指标）；关闭时由上方按钮下的紧凑指标卡替代。 -->
    <Card v-if="fileJobs.length && showParseLogs">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><ScanText class="h-5 w-5" />解析进度</CardTitle>
        <CardDescription>
          查看每个文件的解析状态和进度。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <!-- 性能指标：吞吐量（各运行中窗口近 10 秒字/s 之和）/ 处理速度（累计已处理字÷累计处理耗时） -->
        <div class="grid gap-3 sm:grid-cols-3">
          <div
            v-for="m in metrics"
            :key="m.label"
            class="rounded-md border bg-muted/30 px-3 py-2"
          >
            <div class="text-xs text-muted-foreground" :title="m.title">{{ m.label }}</div>
            <div class="mt-0.5 text-lg font-semibold tabular-nums">{{ m.value }}</div>
          </div>
        </div>

        <div v-for="row in jobRows" :key="row.taskId" class="space-y-2 rounded-md border p-3">
          <div class="flex items-center gap-3">
            <span class="min-w-0 flex-1 truncate text-sm font-medium" :title="row.name">{{ row.name }}</span>
            <Badge :variant="row.state.variant">{{ row.stateText }}</Badge>
            <span
              class="shrink-0 text-xs tabular-nums"
              :class="row.state.label === '解析中' ? 'text-primary' : 'text-muted-foreground'"
              title="近 10 秒平均速度"
            >{{ row.state.label === '解析中' ? `${Math.round(row.task?.llm_cps_10s ?? 0)} 字/s` : '—' }}</span>
            <span class="shrink-0 w-10 text-right text-xs text-muted-foreground">
              {{ Math.round(row.progress * 100) }}%
            </span>
            <Button v-if="row.active" variant="outline" size="sm" @click="cancelJob(row.task)">
              <XCircle class="h-3.5 w-3.5" />取消
            </Button>
            <Button
              v-else-if="row.state.label === '失败'"
              variant="outline"
              size="sm"
              @click="retryJob(row.task)"
            >
              <RefreshCw class="h-3.5 w-3.5" />重试
            </Button>
          </div>

          <Progress :value="row.progress" :indicator-class="progressIndicator(row)" />

          <div class="flex items-stretch gap-3">
            <!-- 左：解析进度日志（现有窗口，行为不变；任务结束后收起，把整行让给右侧） -->
            <div :class="row.active ? 'min-w-0 flex-1' : 'hidden'">
              <LiveLogPanel
                v-if="row.active"
                :task="row.task ?? null"
                :show-progress="false"
                :max-height-class="'h-40'"
              />
            </div>
            <!-- 右：流式反馈（LLM 原始输出，实时追加；完成后保留以便回看） -->
            <div class="min-w-0 flex-1">
              <LiveStreamPanel
                :task="row.task ?? null"
                :max-height-class="'h-40'"
              />
            </div>
          </div>

          <p v-if="row.state.label === '失败'" class="text-xs text-destructive">
            {{ row.error || '解析失败' }}
          </p>
        </div>
      </CardContent>
    </Card>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
