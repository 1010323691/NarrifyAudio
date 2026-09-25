<script setup lang="ts">
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { mergeStatusPackages, runMerge, ttsStatus } from '@/api/tts'
import { listDir } from '@/api/files'
import { downloadFile, downloadUrl } from '@/utils/fileops'
import type { MergePackageStatus, MergeResult, TaskSnapshot, TTSStatus } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardFooter from '@/components/ui/CardFooter.vue'
import Badge from '@/components/ui/Badge.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  Combine,
  Download,
  Eraser,
  Loader2,
  ListChecks,
  RefreshCw,
  XCircle,
  ArrowRight,
  ArrowLeft,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const project = usePipelineStateStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)

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
// 在途（非终态）取 seq 升序首个；失败取 seq 降序最新（重试走同一任务 id，
// 故同一任务不会同时处于两种集合）。
// ---------------------------------------------------------------------------

const TERMINAL = new Set(['cancelled', 'succeeded', 'failed'])

function pkgOfLabel(label: string): string {
  // 与后端 _inflight_merge_packages 的 re.search(r"：(.+)$") 同一口径：取第一个「：」。
  const i = label.indexOf('：')
  return i >= 0 ? label.slice(i + 1) : ''
}

const tasksByPkg = computed(() => {
  const active = new Map<string, TaskSnapshot>()
  const failed = new Map<string, TaskSnapshot>()
  for (const t of taskStore.tasks) {
    if (t.module !== 'merge') continue
    const pkg = pkgOfLabel(t.label)
    if (!pkg) continue
    if (t.status === 'failed') {
      const cur = failed.get(pkg)
      if (!cur || t.seq > cur.seq) failed.set(pkg, t)
    } else if (!TERMINAL.has(t.status)) {
      const cur = active.get(pkg)
      if (!cur || t.seq < cur.seq) active.set(pkg, t)
    }
  }
  return { active, failed }
})

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
    const merged = task || !stat?.complete
      ? undefined
      : (mergedNames.value[pkg] ?? diskMp3.value[pkg])
    let label: string
    let variant: RowVariant
    let ready = false
    let mergedState = false
    if (task) {
      label = '合并中'
      variant = 'secondary'
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
    return { pkg, task, failedTask: task ? undefined : failed.get(pkg), stat, merged, label, variant, ready, mergedState }
  })
})

const selectedNames = computed(() => pkgNames.value.filter((p) => !!selected[p]))
const readyPkgs = computed(() => rows.value.filter((r) => r.ready).map((r) => r.pkg))
const mergedSelectedCount = computed(
  () => rows.value.filter((r) => r.mergedState && selected[r.pkg]).length,
)
const mergeActive = computed(() => taskStore.activeTasks('merge'))

const allReadySelected = computed(
  () => readyPkgs.value.length > 0 && readyPkgs.value.every((p) => !!selected[p]),
)
const allSelected = computed(
  () => pkgNames.value.length > 0 && pkgNames.value.every((p) => !!selected[p]),
)

// ---------------------------------------------------------------------------
// 行刷新（磁盘口径）：listDir(05) -> listDir(06, .mp3) -> mergeStatusPackages。
// 仅在「无在途任务」的包上用磁盘值更新内存合并标记（在途包的内存态不被磁盘旧态覆盖）。
// ---------------------------------------------------------------------------

async function refreshRows() {
  rowsLoading.value = true
  rowsError.value = ''
  try {
    const [chunks, mergedDir] = await Promise.all([listDir('05_audio_chunk'), listDir('06_audio_merge')])
    const names = chunks.items.filter((i) => i.is_dir).map((i) => i.name)
    pkgNames.value = names
    const mp3s: Record<string, string> = {}
    for (const i of mergedDir.items) {
      if (!i.is_dir && i.name.endsWith('.mp3')) mp3s[i.name.replace(/\.mp3$/, '')] = i.name
    }
    diskMp3.value = mp3s
    const { active } = tasksByPkg.value
    for (const pkg of names) {
      if (active.has(pkg)) continue // 在途包的内存标记保持（磁盘可能是上一轮的旧产物）
      if (mp3s[pkg]) mergedNames.value[pkg] = mp3s[pkg]
      else delete mergedNames.value[pkg]
    }
    if (names.length) {
      const res = await mergeStatusPackages(names)
      const stats: Record<string, MergePackageStatus> = {}
      for (const p of res.packages) stats[p.name] = p
      pkgStats.value = stats
    } else {
      pkgStats.value = {}
    }
    // 剔除已消失目录的选中项（绝不删用户数据——这里只是清 UI 勾选）。
    for (const k of Object.keys(selected)) {
      if (!names.includes(k)) delete selected[k]
    }
  } catch (e: any) {
    rowsError.value = e?.message || '刷新失败'
  } finally {
    rowsLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 工具栏：全选 = 清空后只勾「已就绪」行；全量全选 = 无视状态全勾；清空 = 全部取消。
// ---------------------------------------------------------------------------

function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}

function onSelectChange(pkg: string, e: Event) {
  if ((e.target as HTMLInputElement).checked) selected[pkg] = true
  else delete selected[pkg]
}

function selectReady() {
  const ready = readyPkgs.value
  if (!ready.length) return
  if (allReadySelected.value) {
    for (const p of ready) delete selected[p]
  } else {
    clearSelection()
    for (const p of ready) selected[p] = true
  }
}

function selectAllAll() {
  const all = pkgNames.value
  if (!all.length) return
  clearSelection()
  if (!allSelected.value) for (const p of all) selected[p] = true
}

function clearAll() {
  clearSelection()
}

// ---------------------------------------------------------------------------
// 提交 / 取消 / 重试
// ---------------------------------------------------------------------------

async function doRun() {
  if (submitting.value || !selectedNames.value.length) return
  submitting.value = true
  error.value = ''
  try {
    await runMerge(false, selectedNames.value)
    await taskStore.refresh()
    // 完成由下方的 SSE 驱动 watcher 处理（逐包终态 → 行状态流转）。
  } catch (e: any) {
    const msg = e?.message || '启动失败'
    error.value = msg
    if (msg.includes('在途')) {
      toast({ title: '提交被拒绝', variant: 'destructive', description: msg })
    }
  } finally {
    submitting.value = false
  }
}

function cancelRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'cancel')
}

function retryRow(task: TaskSnapshot) {
  taskStore.control(task.id, 'retry')
}

function cancelAll() {
  // 无专用 cancel-batch 端点：逐任务 cancel（PENDING 壳就地终结 + RUNNING 协作取消，必然收敛）。
  for (const t of mergeActive.value) taskStore.control(t.id, 'cancel')
}

function downloadRow(row: MergeRow) {
  if (row.merged) downloadFile('06_audio_merge', row.merged)
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
  () => taskStore.tasks.filter((t) => t.module === 'merge').map((t) => `${t.id}:${t.status}`).join('|'),
  () => {
    // 首次触发 = 页面加载/重连后的快照回放：把当下已终态的任务全部预标记为「已处理」
    // （其磁盘产物由 refreshRows 的 06 目录扫描恢复），避免对陈旧结果重复弹 toast。
    if (!watcherArmed) {
      watcherArmed = true
      for (const t of taskStore.tasks) {
        if (t.module === 'merge' && TERMINAL.has(t.status)) processed.add(t.id)
      }
      return
    }
    for (const t of taskStore.tasks) {
      if (t.module !== 'merge') continue
      const pkg = pkgOfLabel(t.label)
      if (!pkg) continue
      if (!TERMINAL.has(t.status)) {
        processed.delete(t.id) // 重试转回运行态 → 允许再次处理其终态
        continue
      }
      if (processed.has(t.id)) continue
      processed.add(t.id)
      if (t.status === 'succeeded') {
        const file = (t.result?.file as string) || ''
        if (file.endsWith('.mp3')) {
          mergedNames.value[pkg] = file
          project.recordMerge(t.result as MergeResult)
          toast({ title: '音频合并完成', variant: 'success', description: `已生成 ${file}` })
        } else {
          // 编码失败兜底：WAV 是唯一产物，行回落「已就绪」，重合并（-y 覆盖）自愈。
          delete mergedNames.value[pkg]
          toast({ title: '音频合并完成（MP3 编码失败，已保留 WAV）', variant: 'destructive', description: pkg })
        }
      } else {
        delete mergedNames.value[pkg]
      }
    }
  },
)

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await taskStore.refresh()
  await refreshRows()
})

// keep-alive 缓存页：重新进入时刷新磁盘口径（零定时器 → 无需 onDeactivated 清理）。
onActivated(() => {
  if (settings.loaded) void refreshRows()
})
</script>

<template>
  <div class="space-y-4">
    <div>
      <h1 class="flex items-center gap-3 text-2xl font-bold tracking-tight">
        <Combine class="h-6 w-6" />音频合并
        <StatusPill
          :label="(status?.ready ?? status?.implemented) ? '可用' : '引擎未就绪'"
          :tone="(status?.ready ?? status?.implemented) ? 'positive' : 'neutral'"
          :aria-label="(status?.ready ?? status?.implemented) ? '引擎可用' : '引擎未就绪'"
        />
      </h1>
      <p class="mt-1 text-muted-foreground">
        将已合成的音频段合并为完整有声书。
      </p>
    </div>

    <ProjectGateAlert />

    <Alert v-if="status && !(status.ready ?? status.implemented)" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Combine class="h-5 w-5" />批量合并</CardTitle>
          <CardDescription>
            选择要合并的音频包。全选只选择已就绪的项目。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <Alert v-if="rowsError" variant="destructive">
            <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
            {{ rowsError }}
          </Alert>

          <div
            v-else-if="pkgNames.length"
            class="max-h-96 space-y-1 overflow-y-auto rounded-md border p-2"
          >
            <!-- 行 = 进度行：复选 + 包名 + 状态 Badge + 按状态追加的控件
                 （行内按钮不包在 <label> 里，避免点按钮连带切换勾选）。 -->
            <div v-for="row in rows" :key="row.pkg" class="rounded px-2 py-1.5 hover:bg-accent/50">
              <div class="flex items-center gap-3">
                <label class="flex min-w-0 flex-1 cursor-pointer items-center gap-3">
                  <input
                    type="checkbox"
                    class="h-4 w-4 shrink-0 accent-primary"
                    :checked="!!selected[row.pkg]"
                    :disabled="submitting || !!row.task"
                    @change="onSelectChange(row.pkg, $event)"
                  />
                  <span class="min-w-0 truncate text-sm font-medium" :title="row.pkg">{{ row.pkg }}</span>
                </label>
                <Badge :variant="row.variant" class="shrink-0">{{ row.label }}</Badge>
                <template v-if="row.task">
                  <Progress
                    :value="row.task.progress"
                    class="h-1.5 w-24 shrink-0 sm:w-32"
                  />
                  <span class="w-10 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                    {{ Math.round(row.task.progress * 100) }}%
                  </span>
                  <Button variant="outline" size="sm" class="shrink-0" @click="cancelRow(row.task)">
                    <XCircle class="h-3.5 w-3.5" />取消
                  </Button>
                </template>
                <template v-else-if="row.merged">
                  <Button variant="outline" size="sm" class="shrink-0" @click="downloadRow(row)">
                    <Download class="h-3.5 w-3.5" />下载
                  </Button>
                  <MiniAudioPlayer :src="downloadUrl('06_audio_merge', row.merged)" />
                </template>
                <Button
                  v-else-if="row.failedTask"
                  variant="outline"
                  size="sm"
                  class="shrink-0"
                  @click="retryRow(row.failedTask)"
                >
                  <RefreshCw class="h-3.5 w-3.5" />重试
                </Button>
              </div>
              <p v-if="row.failedTask" class="mt-1 pl-7 text-xs text-destructive">
                {{ row.failedTask.error || '合并失败' }}
              </p>
              <!-- 日志面板仅在运行/暂停时渲染（防 N 个排队壳渲染 N 个空面板）。 -->
              <div v-if="row.task && (row.task.status === 'running' || row.task.status === 'paused')" class="mt-2">
                <LiveLogPanel :task="row.task" :max-height-class="'h-40'" />
              </div>
            </div>
          </div>
          <p v-else class="text-sm text-muted-foreground">
            暂无可合并的音频，请先到「音频合成」生成音频。
          </p>

          <div class="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" :disabled="submitting || !pkgNames.length" @click="selectReady">
              <ListChecks class="h-3.5 w-3.5" />{{ allReadySelected ? '已选' : '全选' }}
            </Button>
            <Button variant="outline" size="sm" :disabled="submitting || !pkgNames.length" @click="selectAllAll">
              <ListChecks class="h-3.5 w-3.5" />{{ allSelected ? '已全选' : '全量全选' }}
            </Button>
            <Button variant="outline" size="sm" :disabled="submitting || !pkgNames.length" @click="clearAll">
              <Eraser class="h-3.5 w-3.5" />清空
            </Button>
            <Button variant="outline" size="sm" :disabled="submitting || rowsLoading" @click="refreshRows">
              <RefreshCw class="h-3.5 w-3.5" :class="rowsLoading ? 'animate-spin' : ''" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">
              已选 {{ selectedNames.length }} / {{ pkgNames.length }} 个
              <span v-if="readyPkgs.length"> · 已就绪 {{ readyPkgs.length }} 个</span>
              <span v-if="mergedSelectedCount"> · 含已合并 {{ mergedSelectedCount }}</span>
            </span>
          </div>

          <div class="flex flex-wrap gap-2">
            <Button
              class="min-w-[10rem] flex-1"
              :disabled="!projectSet || submitting || !selectedNames.length"
              @click="doRun"
            >
              <Loader2 v-if="submitting" class="h-4 w-4 animate-spin" />
              <Combine v-else class="h-4 w-4" />
              {{ submitting ? '投放中…' : `合并为 MP3（${selectedNames.length} 个包）` }}
            </Button>
            <Button v-if="mergeActive.length" variant="destructive" @click="cancelAll">
              <XCircle class="h-4 w-4" />取消全部
            </Button>
          </div>
        </CardContent>
        <CardFooter class="justify-between">
          <Button variant="outline" size="sm" @click="router.push('/batch')">
            <ArrowLeft class="h-4 w-4" />返回音频合成
          </Button>
          <Button
            v-if="project.mergeResult && settings.config?.ui.show_audio_split"
            size="sm"
            @click="router.push('/audio')"
          >
            前往音频分集<ArrowRight class="h-4 w-4" />
          </Button>
        </CardFooter>
      </Card>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>
  </div>
</template>
