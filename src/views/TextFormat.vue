<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { submitDurableTask } from '@/api/persistentTasks'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import { downloadFile, pickFile } from '@/utils/fileops'
import { formatNumber } from '@/utils/format'
import type {
  BookAnalyzeResult,
  BookChapter,
  BookSmartSplitResult,
  BookSplitResult,
  SmartConfidence,
  TextFormatResult,
  TextToggles,
} from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Alert from '@/components/ui/Alert.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import Table from '@/components/ui/Table.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableRow from '@/components/ui/TableRow.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableCell from '@/components/ui/TableCell.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import { FileText, ArrowRight, RefreshCw, Download, Scissors, AlertTriangle, Sparkles } from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

const file = ref<{ path: string; name: string; size?: number; file_id?: string; project_id?: string } | null>(null)
const toggles = reactive<TextToggles>({
  keep_single_space: false,
  sentence_break: true,
  dialogue_separate: true,
  detect_chapters: true,
  punct_ellipsis: true,
  punct_repeated: true,
  punct_lone_ascii: false,
  punct_quotes: false,
  punct_dash: false,
  live: true,
})

const busyFormat = ref(false)
const formatResult = ref<TextFormatResult | null>(null)
const busyAnalyze = ref(false)
const analysis = ref<BookAnalyzeResult | null>(null)
const busySplit = ref(false)
const splitResult = ref<BookSplitResult | null>(null)
const busySmart = ref(false)
const smartResult = ref<BookSmartSplitResult | null>(null)
// 章节序号警告（缺号/重号/乱序）被「不处理，继续」关闭（非阻断，仅为提示）。
const seqWarningDismissed = ref(false)
// 智能识别覆盖分析前，原始排版识别到的章节数（章节分析卡「已覆盖」注记用）。
const smartOriginalCount = ref<number | null>(null)
const error = ref('')

const zeroChapters = computed(() => !!analysis.value && analysis.value.chapter_count === 0)
// 零章节整本分册已完成（提示条从「选择」切换为「已按整本处理」注记）。
const wholeBookDone = computed(() => zeroChapters.value && !!splitResult.value)
const showSeqWarning = computed(
  () => !!analysis.value && !zeroChapters.value && analysis.value.sequence.hasIssues && !seqWarningDismissed.value,
)
// 分册 = 按智能识别结果（智能识别完成后才可分册）。
const splitEnabled = computed(
  () =>
    !!formatResult.value &&
    !!smartResult.value &&
    !!analysis.value &&
    analysis.value.chapter_count > 0 &&
    !busySplit.value &&
    projectSet.value,
)
// 智能识别：需要排版产物 + 至少一个章节；任何环节忙碌时禁用。
const smartEnabled = computed(
  () =>
    !!formatResult.value &&
    !!analysis.value &&
    !zeroChapters.value &&
    projectSet.value &&
    !busyFormat.value &&
    !busyAnalyze.value &&
    !busySplit.value &&
    !busySmart.value,
)
// 序号体检有问题时高亮「智能识别」按钮（提示而非自动执行）。
const seqHasIssues = computed(() => !!analysis.value && analysis.value.sequence.hasIssues)

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  if (settings.config) Object.assign(toggles, settings.config.text)
})

// 清掉排版之后的所有状态（分析 / 分册 / 选择标记）——重新排版或换文件后
// 一切结果必须重新推导，绝不让旧状态存活。
function resetDownstream() {
  formatResult.value = null
  analysis.value = null
  splitResult.value = null
  smartResult.value = null
  error.value = ''
  seqWarningDismissed.value = false
  smartOriginalCount.value = null
}

// 「重新上传原文」：整页回到选文件状态（排版开关保留——它们是持久配置）。
function resetPage() {
  file.value = null
  resetDownstream()
}

async function choose() {
  const picked = await pickFile([{ name: '文本文件', extensions: ['txt'] }])
  if (picked) {
    file.value = { path: picked.path, name: picked.name, size: picked.size, file_id: picked.file_id || picked.id, project_id: picked.project_id }
    resetDownstream()
  }
}

// 排版 → 自动章节分析（对排版输出分析；排版失败则不分析）。
async function run(auto = false) {
  if (!file.value || busyFormat.value || busyAnalyze.value) return
  busyFormat.value = true
  resetDownstream()
  try {
    if (!file.value.file_id || !file.value.project_id) {
      throw new Error('上传文件未建立项目归属，请重新选择文件。')
    }
    const submitted = await submitDurableTask({
      project_id: file.value.project_id,
      task_type: 'text.format',
      payload: {
        input_file_id: file.value.file_id,
        config: { ...toggles },
        publish_module: '01_input',
        output_name: `${file.value.name.replace(/\.[^.]+$/, '')}_排版.txt`,
      },
      // 计费规则尚未固定，本阶段保留零估算；任务仍完整进入额度账本。
      estimated_units: 0,
      idempotency_key: `text-format:${file.value.file_id}:${crypto.randomUUID()}`,
    })
    const task = await waitForTask.wait(submitted.id)
    if (task.status !== 'succeeded' || !task.result) {
      throw new Error(task.error_message || '持久化排版任务失败')
    }
    const result = task.result
    formatResult.value = {
      source: file.value.path,
      encoding: 'UTF-8',
      output_path: result.path || result.name || '',
      stats: result.stats as TextFormatResult['stats'],
      preview: String(result.preview || ''),
      full_length: Number(result.full_length || 0),
      file_id: result.file_id,
      project_id: file.value.project_id,
    }
    if (!auto) toast({ title: '排版完成', variant: 'success', description: formatResult.value.output_path })
    await analyzeAfterFormat()
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '排版失败'
    if (!auto) toast({ title: '排版失败', variant: 'destructive', description: error.value })
  } finally {
    busyFormat.value = false
  }
}

async function analyzeAfterFormat() {
  if (!formatResult.value?.file_id || !formatResult.value.project_id) {
    error.value = '排版产物未建立项目归属，无法提交章节分析。'
    return
  }
  busyAnalyze.value = true
  try {
    const submitted = await submitDurableTask({
      project_id: formatResult.value.project_id,
      task_type: 'book.analyze',
      payload: {
        input_file_id: formatResult.value.file_id,
      },
      estimated_units: 0,
      idempotency_key: `book-analyze:${formatResult.value.file_id}:${crypto.randomUUID()}`,
    })
    const task = await waitForTask.wait(submitted.id)
    if (task.status !== 'succeeded' || !task.result?.analysis) {
      throw new Error(task.error_message || '持久化章节分析任务失败')
    }
    analysis.value = task.result.analysis as BookAnalyzeResult
    if (analysis.value.raw_chapter_count !== undefined) {
      smartOriginalCount.value = analysis.value.raw_chapter_count
    }
    // 零章节的 error 是「提示」而非失败——由整本/重传提示条承接，不进 error。
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '章节分析失败'
    toast({ title: '章节分析失败', variant: 'destructive', description: error.value })
  } finally {
    busyAnalyze.value = false
  }
}

// Live: reformat (and re-analyze) automatically when a toggle flips and a
// result already exists.
watch(
  () => Object.values(toggles).join('|'),
  () => {
    if (toggles.live && file.value && formatResult.value) run(true)
  },
)

function proceedAnyway() {
  seqWarningDismissed.value = true
}

function applyFinalAnalysis(chapters: BookChapter[], filenames: string[]) {
  if (!analysis.value) return
  analysis.value = {
    ...analysis.value,
    chapters,
    chapter_count: chapters.length,
    filenames,
    sequence: {
      count: chapters.length,
      parseable: chapters.length,
      unparseable: 0,
      first: chapters[0]?.num ?? null,
      last: chapters[chapters.length - 1]?.num ?? null,
      gaps: [],
      duplicates: [],
      disorder: [],
      hasIssues: false,
    },
    error: null,
  }
}

// 分册 = 按智能识别结果拆分：后端重跑确定性修复（同输入 → 同结果），写出与
// 智能识别完全相同的「第 NNN 章 标题.txt」文件（不产生第二套命名）。
async function split() {
  if (!splitEnabled.value || !formatResult.value) return
  busySplit.value = true
  error.value = ''
  try {
    const result = await runSplitTask({ smart: true })
    const r = toBookSplitResult(result)
    splitResult.value = r
    if (r.chapters?.length) applyFinalAnalysis(r.chapters, r.files.map((f) => f.name))
    toast({ title: '分册完成', variant: 'success', description: `已按智能识别结果生成 ${r.file_count} 个分册文件` })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '分册失败'
    toast({ title: '分册失败', variant: 'destructive', description: error.value })
  } finally {
    busySplit.value = false
  }
}

// 零章节「不处理，按整本继续」：直接写单个 `<base> 全书.txt`（唯一不经智能识别的分册路径）。
async function runWholeBook() {
  if (!formatResult.value || !analysis.value || !projectSet.value || busySplit.value) return
  busySplit.value = true
  error.value = ''
  try {
    const r = toBookSplitResult(await runSplitTask({ whole_book: true }))
    splitResult.value = r
    toast({ title: '整本分册完成', variant: 'success', description: `已生成整本文件 ${r.files[0]?.name ?? ''}` })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '整本分册失败'
    toast({ title: '整本分册失败', variant: 'destructive', description: error.value })
  } finally {
    busySplit.value = false
  }
}

// 智能识别：机械修复章节结构（重编号 1..N、拆分异常长章、删除重复章），
// 输出「第 NNN 章 标题.txt」+ 修复报告。与分册同落 02_split_text/。
async function runSmart() {
  if (!smartEnabled.value || !formatResult.value || !analysis.value) return
  busySmart.value = true
  error.value = ''
  try {
    const result = await runSplitTask({ smart: true })
    const r = toBookSmartSplitResult(result)
    smartResult.value = r
    // 用修复结果覆盖原始排版识别：此后「章节分析」卡展示修复后的 1..N 结构
    //（缺号/重号/乱序警告随之消失），「开始分册」也按这份结果拆分文件。
    smartOriginalCount.value = analysis.value.raw_chapter_count ?? analysis.value.chapter_count
    applyFinalAnalysis(r.chapters.map((c, i) => ({
        seq: i + 1,
        num: c.final_num,
        numStr: String(c.final_num),
        title: c.title,
        chars: c.chars,
      })), r.files.map((f) => f.name))
    seqWarningDismissed.value = false
    const removedN = r.report.removed.length
    toast({
      title: r.status === 'clean' ? '智能识别完成：未检测到异常' : '智能识别完成',
      variant: 'success',
      description: removedN
        ? `生成 ${r.file_count} 个文件，删除 ${removedN} 个重复章节（详见报告）`
        : `生成 ${r.file_count} 个文件`,
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '智能识别失败'
    toast({ title: '智能识别失败', variant: 'destructive', description: error.value })
  } finally {
    busySmart.value = false
  }
}

async function formatAndRecognize() {
  await run()
  if (!formatResult.value || !analysis.value || zeroChapters.value) return
  await runSmart()
}

async function runSplitTask(payload: { smart?: boolean; whole_book?: boolean }) {
  if (!formatResult.value?.file_id || !formatResult.value.project_id) {
    throw new Error('排版产物未建立项目归属，无法提交分册任务。')
  }
  const submitted = await submitDurableTask({
    project_id: formatResult.value.project_id,
    task_type: 'book.split',
    payload: {
      input_file_id: formatResult.value.file_id,
      ...payload,
    },
    estimated_units: 0,
    idempotency_key: `book-split:${formatResult.value.file_id}:${payload.smart ? 'smart' : 'whole'}:${crypto.randomUUID()}`,
  })
  const task = await waitForTask.wait(submitted.id)
  if (task.status !== 'succeeded' || !task.result) {
    throw new Error(task.error_message || '持久化分册任务失败')
  }
  return task.result
}

function toBookSplitResult(result: Record<string, unknown>): BookSplitResult {
  const files = Array.isArray(result.files) ? result.files : []
  return {
    output_dir: String(result.output_dir || ''),
    file_count: Number(result.file_count || files.length),
    files: files.map((item) => {
      const file = item as Record<string, unknown>
      return { name: String(file.name || ''), path: String(file.path || ''), chars: Number(file.chars || 0) }
    }),
    chapters: Array.isArray(result.chapters) ? result.chapters as BookChapter[] : [],
  }
}

function toBookSmartSplitResult(result: Record<string, unknown>): BookSmartSplitResult {
  const files = Array.isArray(result.files) ? result.files : []
  return {
    status: result.status === 'clean' ? 'clean' : 'ok',
    output_dir: String(result.output_dir || ''),
    file_count: Number(result.file_count || files.length),
    files: files.map((item) => {
      const file = item as Record<string, unknown>
      return { name: String(file.name || ''), path: String(file.path || ''), chars: Number(file.chars || 0) }
    }),
    chapters: Array.isArray(result.chapters) ? result.chapters as BookSmartSplitResult['chapters'] : [],
    report: (result.report || { actions: [], warnings: [], removed: [] }) as BookSmartSplitResult['report'],
    baseline_chars: typeof result.baseline_chars === 'number' ? result.baseline_chars : null,
    original_count: Number(result.original_count || 0),
    expected_format: String(result.expected_format || ''),
  }
}

function goNext() {
  if (splitResult.value?.files.length || smartResult.value?.files.length) router.push('/script')
  else toast({ title: '请先完成分册或智能识别', variant: 'destructive' })
}

// 修复动作 / 置信度的展示文案。
const ACTION_LABELS: Record<string, string> = {
  range_split: '范围标题补齐',
  inferred_split: '推断拆分',
  duplicate_kept: '保留重复章',
  duplicate_truncated: '截除重复章',
  renumbered: '重编号',
  gap_absorbed: '吸收跳号',
  kept: '保留',
}
const CONFIDENCE_LABELS: Record<SmartConfidence, string> = { high: '高', medium: '中', low: '低' }
function actionLabels(actions: string[]) {
  return actions.map((a) => ACTION_LABELS[a] ?? a).join('、')
}
function confidenceClass(c: SmartConfidence) {
  return c === 'high'
    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300'
    : c === 'medium'
      ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
      : 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
}

function download(p: string) {
  downloadFile('01_input', p)
}
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">排版与分册</h1>
      <p class="text-muted-foreground mt-1">
        整理原文、识别章节并拆分为分册文本。
      </p>
    </div>

    <ProjectGateAlert />

    <!-- 选择文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <FileText class="h-5 w-5" />选择文件
        </CardTitle>
      </CardHeader>
      <CardContent class="flex items-center gap-3">
        <Button @click="choose" :disabled="busyFormat || busyAnalyze || busySplit || busySmart">选择 TXT 文件</Button>
        <template v-if="file">
          <span class="text-sm font-medium">{{ file.name }}</span>
        </template>
        <span v-else class="text-sm text-muted-foreground">尚未选择文件</span>
      </CardContent>
    </Card>

    <!-- 操作 -->
    <div class="flex flex-wrap items-center gap-x-5 gap-y-3">
      <Button
        @click="formatAndRecognize"
        :disabled="busyFormat || busyAnalyze || busySmart || busySplit || !file || !projectSet"
        :class="seqHasIssues && !smartResult ? 'ring-2 ring-amber-400/80' : ''"
        :title="seqHasIssues ? '章节号存在问题（缺号/重号/乱序），点击按物理顺序修复' : ''"
      >
        <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': busyFormat || busyAnalyze || busySmart }" />
        {{ busyFormat ? '排版中…' : busyAnalyze ? '章节分析中…' : busySmart ? '智能识别中…' : '排版识别' }}
      </Button>
      <Button @click="split" :disabled="!splitEnabled">
        <Scissors class="h-4 w-4" />
        {{ busySplit ? '分册中…' : '分册（智能识别结果）' }}
      </Button>
      <Button
        variant="outline"
        @click="goNext"
        :disabled="!(splitResult || smartResult)"
      >
        前往下一步<ArrowRight class="h-4 w-4" />
      </Button>
    </div>

    <Alert v-if="error" variant="destructive">{{ error }}</Alert>

    <!-- 零章节：整本继续 / 重新上传 二选一（整本分册完成后切换为注记） -->
    <Alert v-else-if="zeroChapters && !wholeBookDone" variant="warning">
      <AlertTriangle class="h-4 w-4 shrink-0" />
      <div class="space-y-2">
        <p>{{ analysis?.error }}</p>
        <div class="flex gap-2">
          <Button size="sm" :disabled="busySplit || !formatResult || !projectSet" @click="runWholeBook">
            不处理，按整本继续
          </Button>
          <Button size="sm" variant="outline" @click="resetPage">重新上传原文</Button>
        </div>
      </div>
    </Alert>
    <Alert v-else-if="wholeBookDone" variant="info">
      已按整本处理：全部文本已写为单个文件 <code class="text-xs">{{ splitResult?.files[0]?.name }}</code>。
    </Alert>

    <!-- 排版结果 -->
    <Card v-if="formatResult">
      <CardHeader>
        <CardTitle>排版结果</CardTitle>
        <p class="text-xs text-muted-foreground">编码 {{ formatResult.encoding }} · 输出 {{ formatResult.output_path }}</p>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="flex flex-wrap gap-6">
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.chars) }}</div>
            <div class="text-xs text-muted-foreground">字数</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.paras) }}</div>
            <div class="text-xs text-muted-foreground">段落</div>
          </div>
          <div>
            <div class="text-2xl font-bold">{{ formatNumber(formatResult.stats.chapters) }}</div>
            <div class="text-xs text-muted-foreground">章节标题</div>
          </div>
        </div>
        <div>
          <div class="mb-1 text-xs text-muted-foreground">预览（前 2000 字）</div>
          <ScrollArea class="h-64 rounded-md border">
            <pre class="whitespace-pre-wrap p-3 text-sm">{{ formatResult.preview }}</pre>
          </ScrollArea>
        </div>
      </CardContent>
      <CardFooter>
        <Button variant="outline" size="sm" @click="download(formatResult.output_path)">
          <Download class="h-4 w-4" />下载
        </Button>
      </CardFooter>
    </Card>

    <!-- 章节分析 -->
    <Card v-if="analysis && !zeroChapters">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Scissors class="h-5 w-5" />章节分析</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>总字数 <b>{{ formatNumber(analysis.total_chars) }}</b></span>
          <span>章节 <b>{{ analysis.chapter_count }}</b></span>
          <span>编码 <b>{{ analysis.encoding }}</b></span>
        </div>
        <p v-if="analysis.repair_status && analysis.raw_chapter_count !== undefined && analysis.raw_chapter_count !== analysis.chapter_count" class="pt-1 text-xs text-emerald-600 dark:text-emerald-400">
          已按智能识别修复结果覆盖（原 {{ smartOriginalCount }} 章 → {{ analysis.chapter_count }} 章），分册将按此结果拆分
        </p>
      </CardHeader>
      <CardContent class="space-y-5">
        <!-- 章节序号警告（非阻断）：列出具体缺号/重号/乱序 + 预期格式 + 两个选择 -->
        <Alert v-if="showSeqWarning" variant="warning">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <div class="space-y-1">
            <p class="font-medium">检测到章节号问题（不影响分册，可继续）</p>
            <ul class="list-disc pl-5 space-y-0.5">
              <li v-for="(g, gi) in analysis.sequence.gaps" :key="'g' + gi">
                缺号：第{{ g.after }}章之后缺少 {{ g.missing.map((n) => `第${n}章`).join('、') }}
              </li>
              <li v-for="(d, di) in analysis.sequence.duplicates" :key="'d' + di">
                重号：第{{ d.num }}章重复出现（第{{ d.seq }}个章节）
              </li>
              <li v-for="(d, di) in analysis.sequence.disorder" :key="'o' + di">
                乱序：第{{ d.num }}章出现在第{{ d.prevNum }}章之后
              </li>
            </ul>
            <p class="text-muted-foreground">
              系统识别的章节格式：{{ analysis.expected_format }}。每章切一个文件，分册编号为顺序号，缺号不影响分册。
            </p>
            <div class="flex gap-2 pt-1">
              <Button size="sm" @click="proceedAnyway">不处理，继续</Button>
              <Button size="sm" variant="outline" @click="resetPage">重新上传原文</Button>
            </div>
          </div>
        </Alert>

        <!-- 章节列表（含分册文件名预览） -->
        <div v-if="analysis.chapters.length">
          <div class="mb-2 text-sm font-medium">章节列表（{{ analysis.chapter_count }}）</div>
          <ScrollArea class="h-72 rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead class="w-14">#</TableHead>
                  <TableHead class="w-24">编号</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead class="w-24 text-right">字数</TableHead>
                  <TableHead>分册文件</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow v-for="c in analysis.chapters" :key="c.seq">
                  <TableCell class="text-muted-foreground">{{ c.seq }}</TableCell>
                  <TableCell>第{{ c.numStr }}章</TableCell>
                  <TableCell class="max-w-[240px] truncate" :title="c.title">{{ c.title || '—' }}</TableCell>
                  <TableCell class="text-right">{{ formatNumber(c.chars) }}</TableCell>
                  <TableCell class="max-w-[320px] truncate" :title="analysis.filenames[c.seq - 1]">
                    {{ analysis.filenames[c.seq - 1] }}
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      </CardContent>
    </Card>

    <!-- 零章节：将输出的单个文件 -->
    <Card v-else-if="analysis && zeroChapters">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Scissors class="h-5 w-5" />章节分析</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>总字数 <b>{{ formatNumber(analysis.total_chars) }}</b></span>
          <span>章节 <b>0</b></span>
          <span>编码 <b>{{ analysis.encoding }}</b></span>
        </div>
      </CardHeader>
      <CardContent>
        <p class="text-sm text-muted-foreground">
          未检测到任何章节（系统识别的格式：{{ analysis.expected_format }}）。可「不处理，按整本继续」或重新上传原文。
        </p>
      </CardContent>
    </Card>

    <!-- 分册结果 -->
    <Card v-if="splitResult">
      <CardHeader>
        <CardTitle>分册结果</CardTitle>
        <p class="text-xs text-muted-foreground">
          共 {{ splitResult.file_count }} 个文件 · 输出目录 {{ splitResult.output_dir }}
        </p>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>文件名</TableHead>
              <TableHead class="w-24 text-right">字数</TableHead>
              <TableHead class="w-40"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow v-for="f in splitResult.files" :key="f.path">
              <TableCell class="max-w-[420px] truncate">{{ f.name }}</TableCell>
              <TableCell class="text-right">{{ formatNumber(f.chars) }}</TableCell>
              <TableCell class="text-right">
                <div class="flex items-center justify-end gap-1">
                  <Button variant="ghost" size="sm" @click="downloadFile('02_split_text', f.path)">
                    <Download class="h-3.5 w-3.5" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
      <CardFooter>
        <Button size="sm" @click="goNext">前往下一步（文本解析）<ArrowRight class="h-4 w-4" /></Button>
      </CardFooter>
    </Card>

    <!-- 智能识别结果 -->
    <Card v-if="smartResult">
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Sparkles class="h-5 w-5" />智能识别</CardTitle>
        <div class="flex flex-wrap gap-4 pt-1 text-sm">
          <span>状态 <b>{{ smartResult.status === 'clean' ? '未检测到异常' : '已修复' }}</b></span>
          <span>原章节 <b>{{ smartResult.original_count }}</b></span>
          <span>最终章节 <b>{{ smartResult.chapters.length }}</b></span>
          <span v-if="smartResult.baseline_chars != null">
            章节长度基线 <b>{{ formatNumber(Math.round(smartResult.baseline_chars)) }}</b> 字
          </span>
        </div>
      </CardHeader>
      <CardContent class="space-y-5">
        <!-- 警告（推断拆分 / 无法处理的异常长章 / 重复章后的跳号 等） -->
        <div v-if="smartResult.report.warnings.length" class="space-y-2">
          <Alert v-for="(w, wi) in smartResult.report.warnings" :key="wi" variant="warning">
            <AlertTriangle class="h-4 w-4 shrink-0" />
            {{ w.detail }}
          </Alert>
        </div>

        <!-- 被删除的重复章 -->
        <Alert v-if="smartResult.report.removed.length" variant="info">
          <div class="space-y-1">
            <p class="font-medium">已删除的重复章节（{{ smartResult.report.removed.length }}）</p>
            <ul class="list-disc pl-5 space-y-0.5">
              <li v-for="(r, ri) in smartResult.report.removed" :key="ri">
                第{{ r.numStr }}章 {{ r.title || '' }}
                <span class="text-muted-foreground">
                  （{{ r.kind === 'truncated' ? '正文与前一章完全相同，已截除重复部分' : '重复章节，未写出' }}）
                </span>
              </li>
            </ul>
          </div>
        </Alert>

        <!-- 修复记录 -->
        <div>
          <div class="mb-2 text-sm font-medium">修复记录（原号 → 新号）</div>
          <ScrollArea class="h-64 rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead class="w-14">#</TableHead>
                  <TableHead class="w-24">原号</TableHead>
                  <TableHead class="w-24">新号</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead>操作</TableHead>
                  <TableHead class="w-20">置信</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow v-for="a in smartResult.report.actions" :key="a.seq">
                  <TableCell class="text-muted-foreground">{{ a.seq }}</TableCell>
                  <TableCell>第{{ a.orig_numStr }}章</TableCell>
                  <TableCell>第{{ String(a.final_num).padStart(3, '0') }}章</TableCell>
                  <TableCell class="max-w-[200px] truncate" :title="a.orig_title">{{ a.orig_title || '—' }}</TableCell>
                  <TableCell class="max-w-[260px] truncate" :title="actionLabels(a.actions)">{{ actionLabels(a.actions) }}</TableCell>
                  <TableCell>
                    <span
                      class="inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium"
                      :class="confidenceClass(a.confidence)"
                    >{{ CONFIDENCE_LABELS[a.confidence] }}</span>
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </ScrollArea>
        </div>

        <!-- 输出文件 -->
        <div>
          <div class="mb-2 text-sm font-medium">输出文件（{{ smartResult.file_count }}）</div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>文件名</TableHead>
                <TableHead class="w-24 text-right">字数</TableHead>
                <TableHead class="w-40"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="f in smartResult.files" :key="f.path">
                <TableCell class="max-w-[420px] truncate">{{ f.name }}</TableCell>
                <TableCell class="text-right">{{ formatNumber(f.chars) }}</TableCell>
                <TableCell class="text-right">
                  <div class="flex items-center justify-end gap-1">
                    <Button variant="ghost" size="sm" @click="downloadFile('02_split_text', f.path)">
                      <Download class="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>

        <p class="text-xs text-muted-foreground">
          低置信拆分或删除的章节请对照上方报告核对。
        </p>
      </CardContent>
      <CardFooter>
        <Button size="sm" @click="goNext">前往下一步（文本解析）<ArrowRight class="h-4 w-4" /></Button>
      </CardFooter>
    </Card>
  </div>
</template>
