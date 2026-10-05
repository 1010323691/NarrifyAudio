<script setup lang="ts">
import {
  computed,
  onActivated,
  onDeactivated,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { useWorkbenchScope, withinScope } from '@/composables/useWorkbenchScope'
import { useWorkbenchTaskControl } from '@/composables/useWorkbenchTaskControl'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { mergeStatusPackages, runMerge, ttsStatus } from '@/api/tts'
import { listDir } from '@/api/files'
import { previewUrl } from '@/utils/fileops'
import type { MergePackageStatus, MergeResult, TaskSnapshot, TTSStatus } from '@/types'

import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import ProductionWorkbench from '@/components/ProductionWorkbench.vue'
import { showConfirm } from '@/components/ui/dialog'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  useLabelDerivedTasks,
  labelKeyOf,
  LABEL_TASK_TERMINAL_STATUSES,
} from '@/composables/useLabelDerivedTasks'
import { Combine, Loader2, ArrowRight } from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const pipeline = usePipelineStateStore()
const taskStore = useTaskStore()
const taskControl = useWorkbenchTaskControl()
const captureScope = useWorkbenchScope()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)
let rowsRequest = 0
let refreshTimer: ReturnType<typeof setTimeout> | null = null
function scheduleRowsRefresh() {
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    if (captureScope()()) void refreshRows()
  }, 250)
}
function stopScheduledRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
}

// 行数据（磁盘口径，onMounted / onActivated / 手动刷新时拉取）：
// 包名列表 + 每包的合成进度 + 06_audio_merge/ 下的 MP3 存在性。
const pkgNames = ref<string[]>([])
const pkgStats = ref<Record<string, MergePackageStatus>>({})
const diskMp3 = ref<Record<string, string>>({}) // 包名 -> 06 下的产物文件名（.mp3）
// 「已合并」标记（内存口径，由 SSE 任务终态驱动）：包名 -> 产物文件名。
const mergedNames = ref<Record<string, string>>({})

const selected = reactive<Record<string, boolean>>({})
const submitting = ref(false)
const rowsLoading = ref(false)
const rowsError = ref('')
const error = ref('')

// ---------------------------------------------------------------------------
// 行任务派生：store 中 merge 任务按 label 尾部「：{包名}」归位（F5 刷新后 store 的
// refresh 已拉回全量任务 → 派生式重挂，无需本地 job 列表）。
// ---------------------------------------------------------------------------

const tasksByPkg = useLabelDerivedTasks('merge')

type RowVariant = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'

interface MergeRow {
  pkg: string
  task: TaskSnapshot | undefined
  failedTask: TaskSnapshot | undefined
  stat: MergePackageStatus | undefined
  merged: string | undefined
  label: string
  variant: RowVariant
  ready: boolean
  mergedState: boolean
}

// 行状态优先级：合并中（有在途任务）> 已合并（内存标记或 06 下有 .mp3）> 已就绪
// （total 全完成，total 取源解析 JSON 口径）> 已合成 C/T 段。WAV 兜底产物不算已合并。
const rows = computed<MergeRow[]>(() => {
  const { active, failed } = tasksByPkg.value
  return pkgNames.value.map((pkg) => {
    const task = active.get(pkg)
    const stat = pkgStats.value[pkg]
    // A previous merged MP3 is invalid as soon as synthesis is incomplete (for example
    // after a character voice change).  Keep it hidden until the current package is
    // synthesized again and explicitly merged again.
    const merged =
      task || !stat?.complete ? undefined : (mergedNames.value[pkg] ?? diskMp3.value[pkg])
    let label: string
    let variant: RowVariant
    let ready = false
    let mergedState = false
    if (task) {
      label = '合并中'
      variant = 'secondary'
    } else if (failed.get(pkg)) {
      label = '合并失败'
      variant = 'destructive'
      ready = !!stat?.complete && !merged
      mergedState = !!merged
    } else if (merged) {
      label = '已合并'
      variant = 'success'
      mergedState = true
    } else if (stat?.complete) {
      label = '已就绪'
      variant = 'default'
      ready = true
    } else if (stat && stat.total > 0) {
      label = `已合成 ${stat.completed}/${stat.total} 段`
      variant = 'secondary'
    } else {
      label = '未合成'
      variant = 'secondary'
    }
    return {
      pkg,
      task,
      failedTask: task ? undefined : failed.get(pkg),
      stat,
      merged,
      label,
      variant,
      ready,
      mergedState,
    }
  })
})

const selectedNames = computed(() => pkgNames.value.filter((p) => !!selected[p]))
const readyPkgs = computed(() => rows.value.filter((r) => r.ready).map((r) => r.pkg))
const mergedSelectedCount = computed(
  () => rows.value.filter((r) => r.mergedState && selected[r.pkg]).length,
)
const mergeActive = computed(() => taskStore.activeTasks('merge'))

// ---------------------------------------------------------------------------
// 行刷新（磁盘口径）：listDir(05) -> listDir(06, .mp3) -> mergeStatusPackages。
// 仅在「无在途任务」的包上用磁盘值更新内存合并标记（在途包的内存态不被磁盘旧态覆盖）。
// ---------------------------------------------------------------------------

async function refreshRows() {
  const isCurrent = captureScope()

  if (!projectSet.value) return
  const request = ++rowsRequest
  rowsLoading.value = true
  rowsError.value = ''
  try {
    const [chunks, mergedDir] = await withinScope(
      Promise.all([listDir('05_audio_chunk'), listDir('06_audio_merge')]),
      isCurrent,
    )
    const names = chunks.items.filter((i) => i.is_dir).map((i) => i.name)
    const response = names.length
      ? await withinScope(mergeStatusPackages(names), isCurrent)
      : { packages: [] }
    if (request !== rowsRequest) return
    const mp3s: Record<string, string> = {}
    for (const item of mergedDir.items)
      if (!item.is_dir && item.name.endsWith('.mp3'))
        mp3s[item.name.replace(/\.mp3$/, '')] = item.name
    pkgNames.value = names
    diskMp3.value = mp3s
    pkgStats.value = Object.fromEntries(response.packages.map((stat) => [stat.name, stat]))
    const { active } = tasksByPkg.value
    // Input completeness continues to gate every artifact, including old disk files.
    mergedNames.value = Object.fromEntries(
      names.filter((name) => !active.has(name) && mp3s[name]).map((name) => [name, mp3s[name]]),
    )
    for (const key of Object.keys(selected)) if (!names.includes(key)) delete selected[key]
  } catch (e: any) {
    if (!isCurrent()) return

    if (request === rowsRequest) rowsError.value = e?.message || '刷新失败'
  } finally {
    if (isCurrent()) {
      if (request === rowsRequest) rowsLoading.value = false
    }
  }
}

// ---------------------------------------------------------------------------
// 快捷选择只使用已核对的就绪输入；重合并通过独立选择范围包含已有产物。
// ---------------------------------------------------------------------------

function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}

function onSelectChange(pkg: string, e: Event) {
  if ((e.target as HTMLInputElement).checked) selected[pkg] = true
  else delete selected[pkg]
}

function selectReady() {
  selectFiltered(readyPkgs.value)
}

function selectAllIncludingDone() {
  selectFiltered(rows.value.filter((row) => row.stat?.complete && !row.task).map((row) => row.pkg))
}

function clearAll() {
  clearSelection()
}

// ---------------------------------------------------------------------------
// 提交 / 取消 / 重试
// ---------------------------------------------------------------------------

async function doRun() {
  const isCurrent = captureScope()

  if (
    submitting.value ||
    !projectSet.value ||
    rowsLoading.value ||
    rowsError.value ||
    !selectedNames.value.length ||
    !selectionReady.value
  )
    return
  submitting.value = true
  error.value = ''
  try {
    const names = [...selectedNames.value]
    if (
      mergedSelectedCount.value &&
      !(await withinScope(
        showConfirm(
          `将重新合并所选章节，其中 ${mergedSelectedCount.value} 章已有产物，新结果将覆盖旧音频。`,
          { title: '确认重新合并', destructive: true },
        ),
        isCurrent,
      ))
    )
      return
    if (!selectionReady.value) return
    await withinScope(runMerge(names), isCurrent)
    await withinScope(taskStore.refresh(), isCurrent)
    // 完成由下方的 SSE 驱动 watcher 处理（逐包终态 → 行状态流转）。
  } catch (e: any) {
    if (!isCurrent()) return

    const msg = e?.message || '启动失败'
    error.value = msg
    if (msg.includes('在途')) {
      toast({ title: '提交被拒绝', variant: 'destructive', description: msg })
    }
  } finally {
    if (isCurrent()) {
      submitting.value = false
    }
  }
}

function cancelRow(task: TaskSnapshot) {
  void taskControl.control(task.id, 'cancel')
}

function retryRow(task: TaskSnapshot) {
  void taskControl.control(task.id, 'retry')
}

function cancelAll() {
  // 无专用 cancel-batch 端点：逐任务 cancel（PENDING 壳就地终结 + RUNNING 协作取消，必然收敛）。
  for (const t of mergeActive.value) void taskControl.control(t.id, 'cancel')
}

// ---------------------------------------------------------------------------
// SSE 驱动（getter 式 watch —— 日志数组每次追加都会触发 deep watch，绝不能用）：
// 终态 merge 任务按 label 尾部归位到行。succeeded + .mp3 → 置「已合并」并记录交接；
// succeeded + .wav（编码失败兜底）/ failed / cancelled → 清除标记（行回落，可重合并）。
// processed 集合防重复（快照重放 / 断线重连）；任务转回非终态（重试）时释放标记。
// ---------------------------------------------------------------------------

const processed = new Set<string>()
let watcherArmed = false

watch(
  () =>
    taskStore.projectTasks
      .filter((t) => t.module === 'merge')
      .map((t) => `${t.id}:${t.status}`)
      .join('|'),
  () => {
    // 首次触发 = 页面加载/重连后的快照回放：把当下已终态的任务全部预标记为「已处理」
    // （其磁盘产物由 refreshRows 的 06 目录扫描恢复），避免对陈旧结果重复弹 toast。
    if (!captureScope()()) return
    if (!watcherArmed) {
      watcherArmed = true
      for (const t of taskStore.projectTasks) {
        if (t.module === 'merge' && LABEL_TASK_TERMINAL_STATUSES.has(t.status)) processed.add(t.id)
      }
      return
    }
    scheduleRowsRefresh()
    for (const t of taskStore.projectTasks) {
      if (t.module !== 'merge') continue
      const pkg = labelKeyOf(t.label)
      if (!pkg) continue
      if (!LABEL_TASK_TERMINAL_STATUSES.has(t.status)) {
        processed.delete(t.id) // 重试转回运行态 → 允许再次处理其终态
        continue
      }
      if (processed.has(t.id)) continue
      processed.add(t.id)
      if (t.status === 'succeeded') {
        const file = (t.result?.file as string) || ''
        if (file.endsWith('.mp3')) {
          mergedNames.value[pkg] = file
          pipeline.recordMerge(t.result as MergeResult)
          toast({ title: '音频合并完成', variant: 'success', description: `已生成 ${file}` })
        } else {
          // 编码失败兜底：WAV 是唯一产物，行回落「已就绪」，重合并（-y 覆盖）自愈。
          delete mergedNames.value[pkg]
          toast({
            title: '音频合并完成（MP3 编码失败，已保留 WAV）',
            variant: 'destructive',
            description: pkg,
          })
        }
      } else {
        delete mergedNames.value[pkg]
      }
    }
  },
)

watch(
  () => taskStore.projectTasks,
  () => {
    if (captureScope()()) scheduleRowsRefresh()
  },
)

onMounted(async () => {
  const isCurrent = captureScope()

  try {
    if (!settings.loaded) await withinScope(settings.load(), isCurrent)
    try {
      status.value = await withinScope(ttsStatus(), isCurrent)
    } catch {
      if (!isCurrent()) return

      status.value = { implemented: false, message: '后端未连接' }
    }
    await withinScope(taskStore.refresh(), isCurrent)
    await withinScope(refreshRows(), isCurrent)
  } catch (e: any) {
    if (!isCurrent() || e?.name === 'AbortError') return
    error.value = e?.message || '工作台初始化失败，请刷新重试。'
  }
})

// keep-alive 缓存页：重新进入时刷新磁盘口径（零定时器 → 无需 onDeactivated 清理）。
onActivated(() => {
  if (settings.loaded) void refreshRows()
})

const engineReady = computed(() => !!(status.value?.ready ?? status.value?.implemented))
const workRows = computed(() =>
  rows.value.map((row) => ({
    ...row,
    workKey: row.pkg,
    workName: row.pkg,
    workState: row.task
      ? 'running'
      : row.failedTask
        ? 'failed'
        : row.merged
          ? 'done'
          : row.stat?.complete
            ? 'ready'
            : 'blocked',
  })),
)
const selectionReady = computed(
  () =>
    selectedNames.value.length > 0 &&
    rows.value.filter((row) => selected[row.pkg]).every((row) => row.stat?.complete && !row.task),
)
function selectFiltered(names: string[]) {
  clearSelection()
  for (const name of names) selected[name] = true
}
function blockedReason(row: MergeRow) {
  return !row.stat
    ? '尚未读取输入状态，请刷新后检查。'
    : !row.stat.total
      ? '未检测到可合成段落，请检查文本解析。'
      : !row.stat.complete
        ? `仍有 ${row.stat.remaining} 段尚未合成；请先补齐合成音频。`
        : row.task
          ? '任务正在执行，请等待完成。'
          : ''
}

onDeactivated(() => {
  submitting.value = false
  stopScheduledRefresh()
})
onBeforeUnmount(stopScheduledRefresh)
</script>

<template>
  <div class="viewport-workbench">
    <header class="page-header">
      <p class="eyebrow">Pipeline · Merge</p>
      <h1 class="page-title flex items-center gap-3">
        音频合并
      </h1>
      <p class="page-description">核对章节输入完整性，合并音频段并试听章节结果。</p>
    </header>
    <div class="workbench-controls" tabindex="0" role="region" aria-label="制作条件与流程">
      <ProjectGateAlert />
      <Alert v-if="status && !engineReady" variant="destructive">{{ status.message }}</Alert>
      <WorkbenchContextBar>
        <template #icon><Combine /></template>
        <template #title>01 核对完整性 → 02 批量合并 → 03 试听与检查</template>
        <template #description><p>仅完整合成的章节可提交；输入未完成时，已有旧产物会隐藏。</p></template>
        <template #actions><Button variant="outline" size="sm" @click="router.push('/batch')">补齐合成音频</Button></template>
      </WorkbenchContextBar>
    </div>
    <ProductionWorkbench
      :rows="workRows"
      :selected="selected"
      :loading="rowsLoading"
      :load-error="rowsError"
      :disabled="submitting || !projectSet"
      :row-disabled="(row) => !!row.task || !row.stat?.complete"
      :filters="[
        { key: 'ready', label: '可合并' },
        { key: 'blocked', label: '输入未就绪' },
        { key: 'running', label: '执行中' },
        { key: 'failed', label: '失败' },
        { key: 'done', label: '已有产物' },
      ]"
      :columns="[
        { label: '输入段数', width: '100px' },
        { label: '状态', width: '105px' },
        { label: '操作', width: '72px', align: 'center' },
      ]"
      label="合并"
      empty-text="暂无音频包，请先完成音频合成。"
      @refresh="refreshRows"
      @select="onSelectChange"
      @select-filtered="selectFiltered"
    >
      <template #selection
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || !readyPkgs.length"
          @click="selectReady"
          >选择首次合并</Button
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || !rows.length"
          @click="selectAllIncludingDone"
          >选择全部就绪（含重合并）</Button
        ><Button
          variant="ghost"
          size="sm"
          :disabled="submitting || !selectedNames.length"
          @click="clearAll"
          >清空</Button
        ></template
      >
      <template #empty
        ><RouterLink to="/batch" class="mt-3 inline-block text-primary underline"
          >前往音频合成</RouterLink
        ></template
      >
      <template #cells="{ row }"
        ><td class="tabular-nums">
          <span>{{ row.stat ? `${row.stat.completed} / ${row.stat.total}` : '未读取' }}</span
          ><Progress
            v-if="row.stat"
            :value="row.stat.total ? row.stat.completed / row.stat.total : 0"
            class="mt-1 h-1"
          />
        </td>
        <td>
          <WorkbenchStatus :variant="row.variant">{{
            row.task
              ? `${Math.round(row.task.progress * 100)}% · 合并中`
              : row.stat?.complete || row.failedTask
                ? row.label
                : '输入未就绪'
          }}</WorkbenchStatus>
        </td>
        <td class="text-center">
          <Button v-if="row.task" variant="ghost" size="sm" @click="cancelRow(row.task)"
            >取消</Button
          ><Button
            v-else-if="row.failedTask"
            variant="ghost"
            size="sm"
            :disabled="
              !row.stat?.complete || submitting || !!taskControl.pending[row.failedTask.id]
            "
            @click="retryRow(row.failedTask)"
            >重试</Button
          ><Button
            v-else-if="!row.stat?.complete"
            variant="ghost"
            size="sm"
            class="h-7 gap-1 rounded-md px-1.5 font-medium text-primary hover:bg-primary/10 hover:text-primary focus-visible:ring-offset-0"
            :aria-label="`补齐 ${row.pkg} 的合成音频`"
            @click="router.push('/batch')"
            >补齐<ArrowRight class="h-3 w-3" aria-hidden="true" /></Button
          >
        </td></template
      >
      <template #detail="{ row }">
        <WorkbenchStatus :variant="row.variant">{{ row.label }}</WorkbenchStatus>
        <dl class="production-facts">
          <dt>输入完整性</dt>
          <dd><WorkbenchStatus :variant="row.stat?.complete ? 'success' : 'secondary'"
            >{{ row.stat?.complete ? '完整' : '未就绪' }}</WorkbenchStatus></dd>
          <dt>已合成 / 总段落</dt>
          <dd>{{ row.stat ? `${row.stat.completed} / ${row.stat.total}` : '未读取' }}</dd>
          <dt>剩余段落</dt>
          <dd>{{ row.stat?.remaining ?? '未读取' }}</dd>
        </dl>
        <div v-if="blockedReason(row)" class="production-section">
          <h3>下一步</h3>
          <p class="text-muted-foreground">{{ blockedReason(row) }}</p>
          <RouterLink
            v-if="!row.stat?.complete"
            to="/batch"
            class="mt-2 inline-block text-primary underline"
            >返回合成补齐输入</RouterLink
          >
        </div>
        <div v-if="row.task || row.failedTask" class="production-section">
          <h3>关联任务</h3>
          <p>{{ (row.task || row.failedTask)?.label }}</p>
          <p v-if="row.task" class="mt-2 tabular-nums">
            {{ Math.round(row.task.progress * 100) }}% ·
            {{ row.task.status === 'pending' ? '排队等待' : '执行中' }}
          </p>
          <p v-if="row.failedTask" class="mt-2 break-words text-destructive">
            {{ row.failedTask.error || '合并失败，请检查输入后重试。' }}
          </p>
          <LiveLogPanel
            :task="row.task || row.failedTask || null"
            :max-height-class="'h-40'"
            class="mt-3"
          />
        </div>
        <div class="production-section">
          <h3>合并产物</h3>
          <template v-if="row.merged"
            ><p class="mb-3 break-all text-muted-foreground">{{ row.merged }}</p>
            <div class="rounded-lg border p-3">
              <MiniAudioPlayer :key="row.merged" :src="previewUrl('06_audio_merge', row.merged)" preload-metadata />
            </div>
            </template
          >
          <p v-else class="text-muted-foreground">
            {{
              diskMp3[row.pkg]
                ? '已有旧文件，但当前输入或任务状态不满足有效产物条件。'
                : '尚无可用 MP3。合并完成后可在此试听与检查。'
            }}
          </p>
        </div>
      </template>
    </ProductionWorkbench>
    <div class="production-actionbar">
      <div class="mr-auto text-xs">
        <strong>已选 {{ selectedNames.length }} 章 · 重新合并 {{ mergedSelectedCount }} 章</strong>
        <p class="mt-1 text-muted-foreground">
          {{
            selectedNames.length && !selectionReady
              ? '所选章节状态已变化，请重新选择就绪章节。'
              : '首次合并创建 MP3；重新合并会覆盖所选章节已有结果。'
          }}
        </p>
      </div>
      <Button
        :disabled="!projectSet || !engineReady || submitting || rowsLoading || !!rowsError || !selectionReady"
        @click="doRun"
        ><Loader2 v-if="submitting" class="h-4 w-4 animate-spin" /><Combine
          v-else
          class="h-4 w-4"
        />{{ submitting ? '提交中…' : `合并所选（${selectedNames.length} 章）` }}</Button
      ><Button v-if="mergeActive.length" variant="destructive" @click="cancelAll"
        >取消全部合并</Button
      ><Button
        v-if="pipeline.mergeResult && settings.config?.ui.show_audio_split"
        variant="outline"
        @click="router.push('/audio')"
        >前往音频分集<ArrowRight class="h-4 w-4"
      /></Button>
    </div>
    <div v-if="error" class="workbench-feedback" tabindex="0" role="region" aria-label="制作反馈与报告">
      <Alert v-if="error" variant="destructive">{{ error }}</Alert>
    </div>
  </div>
</template>
