<script setup lang="ts">
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { cancelParseBatch, generateScriptFiles } from '@/api/script'
import { listDir } from '@/api/files'
import { listProjectFiles } from '@/api/project'
import { listProjectDurableTasks } from '@/api/durableTasks'
import type { FileItem, ParseChecks, TaskSnapshot } from '@/types'

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
import LiveStreamPanel from '@/components/ui/LiveStreamPanel.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  FileText,
  ScanText,
  Loader2,
  X,
  XCircle,
  RefreshCw,
  ListChecks,
  Eraser,
  TriangleAlert,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()

// LLM / 生成参数 / Prompt 由管理员设置管理；解析内 6 项 LLM 检查开关在本页
// 「解析检查项」子窗口（「选择待解析文件」卡右上角按钮进入）：勾选状态随
// 「开始处理」的任务提交（固化进每个任务的配置快照），并记忆进项目配置作
// 下次页面初值（见下方 checks 状态）。

// ---- File selection (02_split_text) + per-file parse jobs ------------------------
// The user checks one or more split .txt files; each becomes an independent backend
// Task (its own LLM request, status, success and JSON output). The frontend never
// reads file contents — it sends only file names; the backend does the discovery,
// the concurrency-bounded LLM calls, and the per-file JSON writes.
interface ParseFile extends FileItem {
  /** True when 03_parsed_json/<file-stem>.json already exists (already parsed). */
  done: boolean
  /** Parsed, but that parse ran against OLDER file content (the split file was re-published
   *  with the same name). Excluded from 已完成 — needs re-parsing. */
  stale: boolean
}
const files = ref<ParseFile[]>([])
const filesLoading = ref(false)
const filesError = ref('')
const selected = reactive<Record<string, boolean>>({})

const selectedNames = computed(() => files.value.filter((f) => selected[f.name]).map((f) => f.name))
const doneCount = computed(() => files.value.filter((f) => f.done).length)
const staleCount = computed(() => files.value.filter((f) => f.stale).length)
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
 *  批次内文件直接在此显示状态 / 进度条 / 取消·重试。 */
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

// ---- 解析检查项（6 个开关）-----------------------------------------------------
// 勾选状态只在「开始处理」提交任务时生效：值随请求固化进每个任务的配置快照
// （提交值 = 任务最终执行值），跑中改本页不影响在跑任务；重跑（重新提交）才用新值。
// 初值从项目配置读取（上次「开始处理」时记忆的值），缺省全开（true = 现有行为）。
type CheckKey = keyof ParseChecks
const CHECK_DEFS: { key: CheckKey; label: string; hint: string }[] = [
  {
    key: 'check_chunk_alignment',
    label: 'chunk 忠实性校验',
    hint: '检出源文大段缺失（截断 / 模型自停）时恢复重跑：翻倍预算或对半切开。JSON 可解析性重试恒生效。',
  },
  {
    key: 'check_boundary_speakers',
    label: '角色匹配检查',
    hint: '重判 chunk 边界两侧条目的说话人归属（chunk 切割会切断跨段上下文）。',
  },
  {
    key: 'validate_instructs',
    label: 'instruct 检查',
    hint: '修复空 / 过长的声音指导（instruct）条目：先机械继承邻近有效值，剩余一批送 LLM。',
  },
  {
    key: 'revalidate_splits',
    label: '断句失败校验',
    hint: '对外层双引号内「…道：」标签条目重跑解析校验。',
  },
  {
    key: 'check_long_paragraphs',
    label: '超长段落检查',
    hint: '超字数条目先 LLM 语义重切；机械分段兜底（字数上界硬保证）恒生效，本开关只控制 LLM 语义重切。',
  },
  {
    key: 'spot_check_enabled',
    label: '归属抽样',
    hint: '按管理台设定的抽样率重判部分条目的说话人归属。',
  },
]
const checks = reactive<Record<CheckKey, boolean>>({
  check_chunk_alignment: true,
  check_boundary_speakers: true,
  validate_instructs: true,
  revalidate_splits: true,
  check_long_paragraphs: true,
  spot_check_enabled: true,
})
// 「解析检查项」子窗口开关（从「选择待解析文件」卡右上角按钮进入）。
const checksOpen = ref(false)

function initChecks() {
  const g = settings.config?.generation
  for (const d of CHECK_DEFS) {
    const saved = g?.[d.key]
    checks[d.key] = saved === undefined ? true : saved
  }
}

// 解析日志区显隐（设置页「解析日志显示」，默认关）：开 = 显示「解析进度」Card
// （每文件实时日志 + 流式反馈）；关 = 整个 Card 隐藏。保存设置后立即生效
// （settings.config 是响应式的）。
const showParseLogs = computed(() => settings.config?.ui.show_parse_logs ?? false)

// 一批解析结束后自动刷新文件列表：把刚生成 JSON 的文件标记为「已完成」（既有勾选保留）。
watch(busy, (b, was) => {
  if (was && !b) loadFiles()
})

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
  initChecks()
  await loadFiles()
  await taskStore.refresh()
  reattachJobs()
})

let firstActivation = true
onActivated(() => {
  // keep-alive 首次进入也会触发 activated——跳过（onMounted 已加载）。
  if (firstActivation) {
    firstActivation = false
    return
  }
  // 从别的页面回来：排版分册可能已重新处理（同名覆盖），重新做文件时效校验。
  if (projectSet.value) void loadFiles()
})

async function loadFiles() {
  if (!projectSet.value) {
    files.value = []
    return
  }
  const pid = project.activeProjectId
  filesLoading.value = true
  filesError.value = ''
  try {
    const r = await listDir('02_split_text', true)
    // 已生成判断：03_parsed_json/ 下是否已有 <文件基名>.json（后端对缺失目录返回空列表）。
    // 时效校验（P0-03）：02 文件当前 sha（v1 文件表）× 最近一次成功解析记录的
    // source_sha256——同名文件被重新分册覆盖后，旧解析结果标「输入已变更」。
    // 任一数据源缺失（旧任务无 sha / 请求失败）退化为旧行为：只看 03 json 是否存在。
    const out = await listDir('03_parsed_json', true).catch(() => null)
    const [tracked, tasks] = pid
      ? await Promise.all([
          listProjectFiles(pid).catch(() => []),
          listProjectDurableTasks(pid).catch(() => []),
        ])
      : [[], []]
    const outNames = new Set((out?.items ?? []).map((i) => i.name))
    const fileIdByName = new Map<string, string>()
    const shaByFileId = new Map<string, string>()
    for (const t of tracked) {
      if (!t.name) continue
      fileIdByName.set(t.name, t.id)
      shaByFileId.set(t.id, t.sha256)
    }
    // 项目任务列表按创建时间倒序、script.parse 按源文件去重（entry identity），
    // 因此每个源文件的第一条成功解析即其最新一次。
    const parseShaByFileId = new Map<string, string>()
    for (const t of tasks) {
      if (t.task_type !== 'script.parse' || t.status !== 'succeeded' || !t.result) continue
      const fid = t.result.source_file_id
      const sha = t.result.source_sha256
      if (typeof fid === 'string' && typeof sha === 'string' && !parseShaByFileId.has(fid)) {
        parseShaByFileId.set(fid, sha)
      }
    }
    const txts: ParseFile[] = r.items
      .filter((i) => !i.is_dir && i.name.toLowerCase().endsWith('.txt'))
      .map((f) => {
        const stem = f.name.replace(/\.[^.]+$/, '')
        const hasJson = outNames.has(stem + '.json')
        const fileId = fileIdByName.get(f.name)
        const parseSha = fileId ? parseShaByFileId.get(fileId) : undefined
        const currentSha = fileId ? shaByFileId.get(fileId) : undefined
        const stale = hasJson && !!parseSha && !!currentSha && parseSha !== currentSha
        return { ...f, done: hasJson && !stale, stale }
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
    // 检查开关随任务提交（固化进每个任务的配置快照——提交值即该任务最终执行值）。
    const r = await generateScriptFiles(names, { ...checks })
    fileJobs.value = r.files.map((f) => ({ name: f.file, taskId: f.task_id }))
    await taskStore.refresh()
    // 记忆本次勾选进项目配置，作下次页面初值；fire-and-forget，失败不阻断
    // （与 AudioSplit 的 rememberParams 同款模式）。
    void settings.save({ generation: { ...checks } })
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
        <div class="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle class="flex items-center gap-2"><FileText class="h-5 w-5" />选择待解析文件</CardTitle>
            <CardDescription>
              选择要处理的分册文本，可多选；已完成的文件也可以重新解析。
            </CardDescription>
          </div>
          <Button variant="outline" size="sm" class="shrink-0" @click="checksOpen = true">
            <ListChecks class="h-3.5 w-3.5" />解析检查项
          </Button>
        </div>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="staleCount" variant="warning">
          <template #icon><TriangleAlert class="h-4 w-4 shrink-0" /></template>
          {{ staleCount }} 个分册文本已更新，对应的解析结果基于旧内容，请重新解析。
        </Alert>
        <Alert v-if="filesError" variant="destructive">
          <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
          {{ filesError }}
        </Alert>

        <div
          v-else-if="files.length"
          class="max-h-80 space-y-1 overflow-y-auto rounded-md border p-2"
        >
          <!-- 文件行 = 进度行：本批文件直接在此显示 状态（含排队位置）/ 进度条 /
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
              <Badge v-else-if="row.file.stale" variant="warning" class="shrink-0">输入已变更</Badge>
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
            <span v-if="staleCount"> · 输入已变更 {{ staleCount }} 个</span>
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
      </CardContent>
      <CardFooter>
        <span class="text-xs text-muted-foreground">
          解析结果会保存在当前项目中，可在后续制作步骤中继续使用。
        </span>
      </CardFooter>
    </Card>

    <!-- 解析检查项子窗口：6 个解析内 LLM 检查开关（自「选择待解析文件」卡右上角
         按钮进入）。勾选状态只随「开始处理」提交的任务生效（固化进每个任务的配置
         快照），跑中改动不影响在跑任务；重跑（重新提交）用新值。 -->
    <div
      v-if="checksOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="presentation"
      @click.self="checksOpen = false"
    >
      <div
        class="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl border bg-background p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="parse-checks-title"
      >
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 id="parse-checks-title" class="flex items-center gap-2 text-base font-semibold">
              <ListChecks class="h-5 w-5 text-primary" />解析检查项
            </h2>
            <p class="mt-1 text-xs leading-5 text-muted-foreground">
              解析过程中的 6 项 LLM 检查。按提交时勾选状态生效并随任务固化，之后修改本窗口或
              设置都不影响已提交任务；重跑（重新提交）使用最新勾选。
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            class="h-8 w-8 shrink-0 p-0"
            aria-label="关闭解析检查项窗口"
            @click="checksOpen = false"
          >
            <X class="h-4 w-4" />
          </Button>
        </div>
        <div class="mt-4 space-y-3">
          <div
            v-for="d in CHECK_DEFS"
            :key="d.key"
            class="flex items-center justify-between gap-4"
          >
            <div class="min-w-0">
              <p class="text-sm">{{ d.label }}</p>
              <p class="text-xs text-muted-foreground">{{ d.hint }}</p>
            </div>
            <Switch
              :model-value="checks[d.key]"
              :disabled="busy"
              @update:model-value="checks[d.key] = $event"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- 解析进度（每文件一行）：仅「解析日志显示」开启时渲染整个 Card
         （实时日志 + 流式反馈）。 -->
    <Card v-if="fileJobs.length && showParseLogs">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><ScanText class="h-5 w-5" />解析进度</CardTitle>
        <CardDescription>
          查看每个文件的解析状态和进度。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div v-for="row in jobRows" :key="row.taskId" class="space-y-2 rounded-md border p-3">
          <div class="flex items-center gap-3">
            <span class="min-w-0 flex-1 truncate text-sm font-medium" :title="row.name">{{ row.name }}</span>
            <Badge :variant="row.state.variant">{{ row.stateText }}</Badge>
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
